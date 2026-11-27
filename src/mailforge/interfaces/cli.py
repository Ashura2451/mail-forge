"""Small local CLI for sorting, comparing, sending, and recovery."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from mailforge.application.campaigns import run_synthetic_demo
from mailforge.application.ingest import collect_unique_addresses, iter_jsonl_email_field
from mailforge.application.legacy_migration import rehearse_legacy_migration
from mailforge.application.local_application import (
    DataDirectory,
    LocalApplication,
    create_backup,
    restore_backup,
    validate_backup,
)
from mailforge.application.mailer_compat import (
    load_legacy_configuration,
    migrate_legacy_state,
)
from mailforge.application.sorting import EmailSorter, SortConfig
from mailforge.application.transform import (
    count_legacy_list,
    format_email_file,
    remove_duplicate_lines,
    write_legacy_list,
)
from mailforge.domain.sorting import DNSMode
from mailforge.infrastructure.disposable import DisposableDomainStore
from mailforge.interfaces.process_lock import DatabaseProcessLock, ProcessLockError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailforge", description="Local-first email utility")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="validate and deduplicate a local input file")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--output", type=Path)
    ingest.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)

    sort = commands.add_parser("sort", help="stream, classify, and export addresses")
    sort.add_argument("input", type=Path, nargs="+")
    sort.add_argument("--results-dir", type=Path)
    sort.add_argument("--config-dir", type=Path)
    sort.add_argument("--checkpoint", type=Path)
    sort.add_argument("--dns-mode", choices=[mode.value for mode in DNSMode])
    sort.add_argument("--max-bytes", type=int)
    resume = sort.add_mutually_exclusive_group()
    resume.add_argument("--resume", dest="resume", action="store_true")
    resume.add_argument("--fresh", dest="resume", action="store_false")
    sort.set_defaults(resume=None)
    sort.add_argument("--debug", action="store_true")
    sort.add_argument("--profile", choices=["full", "validation", "dedupe", "classify", "report"])
    sort.add_argument("--dry-run", action="store_true")
    sort.add_argument("--progress-every", type=int)

    summary = commands.add_parser("summary", help="print the last sorting summary")
    summary.add_argument("--results-dir", type=Path)
    clean = commands.add_parser("clean", help="remove sorting results")
    clean.add_argument("--results-dir", type=Path)
    clean.add_argument("--yes", action="store_true")

    extract = commands.add_parser("extract-jsonl", help="extract validated JSONL email values")
    extract.add_argument("input", type=Path)
    extract.add_argument("output", type=Path)
    extract.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)

    transform = commands.add_parser("transform", help="run an email-file transformation")
    transform.add_argument("operation", choices=["format", "dedupe", "list", "count"])
    transform.add_argument("input", type=Path)
    transform.add_argument("output", type=Path, nargs="?")

    disposable = commands.add_parser("refresh-disposable", help="refresh disposable-domain data")
    disposable.add_argument("--path", type=Path, default=Path("config/disposable.json"))
    disposable.add_argument("--url", required=True)

    campaign = commands.add_parser("campaign", help="manage retained local send compatibility")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    demo = campaign_commands.add_parser("demo")
    demo.add_argument("--database", type=Path, required=True)
    backup = campaign_commands.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    restore = campaign_commands.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    legacy = campaign_commands.add_parser("translate-legacy")
    legacy.add_argument("--config", type=Path, required=True)
    legacy.add_argument("--database", type=Path)
    legacy.add_argument("--output", type=Path)
    legacy_import = campaign_commands.add_parser("import-legacy")
    legacy_import.add_argument("--database", type=Path, required=True)
    legacy_import.add_argument("--config", type=Path, required=True)
    legacy_import.add_argument("--campaign", required=True)
    legacy_import.add_argument("--targets", type=Path)
    legacy_rehearse = campaign_commands.add_parser("rehearse-legacy-state")
    legacy_rehearse.add_argument("--database", type=Path, required=True)
    legacy_rehearse.add_argument("--config", type=Path, required=True)
    legacy_rehearse.add_argument("--campaign", required=True)

    app = commands.add_parser("app", help="manage the local application")
    app_commands = app.add_subparsers(dest="app_command", required=True)
    for name in ("init", "status", "start", "stop", "restart", "open"):
        item = app_commands.add_parser(name)
        item.add_argument("--data-dir", type=Path, required=True)
        item.add_argument("--port", type=int, default=8765)
        item.add_argument("--no-open", action="store_true")
    app_backup = app_commands.add_parser("backup")
    app_backup.add_argument("--data-dir", type=Path, required=True)
    app_backup.add_argument("--destination", type=Path)
    app_validate = app_commands.add_parser("validate-backup")
    app_validate.add_argument("backup", type=Path)
    app_restore = app_commands.add_parser("restore")
    app_restore.add_argument("--backup", type=Path, required=True)
    app_restore.add_argument("--destination", type=Path, required=True)
    app_restore.add_argument("--replace", action="store_true")

    serve = commands.add_parser("serve", help="serve the local browser application")
    serve.add_argument("--database", type=Path)
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--frontend-dir", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    return parser


def _sort(args: argparse.Namespace) -> int:
    overrides = {}
    for source, target in (("results_dir", "results_dir"), ("config_dir", "config_dir"), ("checkpoint", "checkpoint_path"), ("max_bytes", "max_bytes"), ("profile", "profile"), ("progress_every", "progress_every")):
        value = getattr(args, source, None)
        if value is not None:
            overrides[target] = value
    if args.dns_mode:
        overrides["dns_mode"] = DNSMode(args.dns_mode)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
    result = EmailSorter(SortConfig.from_environment(**overrides)).sort_file(
        args.input[0] if len(args.input) == 1 else args.input,
        resume=args.resume,
        dry_run=args.dry_run,
    )
    print(json.dumps({"committed": result.committed, "summary": str(result.summary_path)}))
    return 0 if result.committed else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        payload = {"count": len(collect_unique_addresses(args.input, max_bytes=args.max_bytes))}
        text = json.dumps(payload, indent=2) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    if args.command == "sort":
        return _sort(args)
    if args.command == "summary":
        summary = EmailSorter(SortConfig.from_environment(results_dir=args.results_dir) if args.results_dir else SortConfig.from_environment()).view_summary()
        if summary is None:
            print("No summary found")
            return 1
        print(json.dumps(summary, indent=2))
        return 0
    if args.command == "clean":
        config = SortConfig.from_environment(results_dir=args.results_dir) if args.results_dir else SortConfig.from_environment()
        removed = EmailSorter(config).delete_results(confirm=args.yes)
        return 0 if removed or not config.results_dir.exists() else 1
    if args.command == "extract-jsonl":
        values = list(iter_jsonl_email_field(args.input, max_bytes=args.max_bytes))
        args.output.write_text("".join(f"{email}\n" for email in values), encoding="utf-8")
        print(f"Wrote {len(values)} validated email(s) to {args.output}")
        return 0
    if args.command == "transform":
        if args.operation == "format":
            if args.output is None: raise SystemExit("transform format requires an output path")
            format_email_file(args.input, args.output)
        elif args.operation == "dedupe":
            print(f"Removed {remove_duplicate_lines(args.input)} duplicates")
        elif args.operation == "list":
            if args.output is None: raise SystemExit("transform list requires an output path")
            write_legacy_list(args.input, args.output)
        else:
            print(count_legacy_list(args.input))
        return 0
    if args.command == "refresh-disposable":
        print(f"Saved {DisposableDomainStore(args.path).refresh(args.url)} disposable domains to {args.path}")
        return 0
    if args.command == "campaign":
        if args.campaign_command == "demo":
            print(json.dumps(run_synthetic_demo(args.database), indent=2, default=str))
            return 0
        from mailforge.infrastructure.campaign_store import CampaignStore
        if args.campaign_command == "backup":
            if not args.database.is_file():
                raise FileNotFoundError(args.database)
            print(json.dumps({"backup": str(CampaignStore(args.database).backup_to(args.destination))}))
            return 0
        if args.campaign_command == "restore":
            print(json.dumps({"restored": str(CampaignStore.restore_from(args.source, args.destination))}))
            return 0
        if args.campaign_command == "translate-legacy":
            print(json.dumps(load_legacy_configuration(args.config), indent=2, default=str))
            return 0
        if args.campaign_command == "import-legacy":
            print(json.dumps(migrate_legacy_state(args.database, args.config, args.campaign, args.targets), indent=2, default=str))
            return 0
        print(json.dumps(rehearse_legacy_migration(args.database, args.config, args.campaign), indent=2, default=str))
        return 0
    if args.command == "app":
        data = DataDirectory.from_path(args.data_dir if hasattr(args, "data_dir") else Path("state"))
        if args.app_command == "init":
            data.initialize(); print(json.dumps({"initialized": True, "data_directory_id": data.identity})); return 0
        if args.app_command == "backup":
            print(json.dumps({"backup": str(create_backup(data, args.destination))})); return 0
        if args.app_command == "validate-backup":
            print(json.dumps(validate_backup(args.backup), indent=2)); return 0
        if args.app_command == "restore":
            print(json.dumps({"restored": str(restore_backup(args.backup, args.destination, replace=args.replace))})); return 0
        application = LocalApplication(data, port=args.port)
        if args.app_command == "status": print(json.dumps(application.status(), indent=2))
        elif args.app_command == "start": print(json.dumps(application.start(open_browser=not args.no_open), indent=2))
        elif args.app_command == "stop": print(json.dumps(application.stop(), indent=2))
        elif args.app_command == "restart": application.stop(); print(json.dumps(application.start(open_browser=not args.no_open), indent=2))
        else: print(json.dumps(application.open(), indent=2))
        return 0
    if args.command == "serve":
        import uvicorn
        from mailforge.interfaces.web import create_app
        database = args.database or ((args.data_dir / "mailforge.sqlite3") if args.data_dir else Path("state/mailforge.sqlite3"))
        frontend = args.frontend_dir or Path(__file__).resolve().parents[1] / "web_assets"
        if args.host not in {"127.0.0.1", "localhost", "::1"}: raise SystemExit("mailforge serve only binds to a loopback host")
        try:
            with DatabaseProcessLock(database):
                uvicorn.run(create_app(database, frontend_dir=frontend, data_directory=args.data_dir or database.parent), host=args.host, port=args.port, log_level="info")
        except ProcessLockError as error:
            raise SystemExit(str(error)) from error
        return 0
    raise SystemExit(f"unsupported command: {args.command}")
