import asyncio
import json
from pathlib import Path

import aiodns
import pytest

from mailforge.application.ingest import collect_unique_addresses, iter_jsonl_email_field
from mailforge.application.sorting import Checkpoint, EmailSorter, InputReader, SortConfig
from mailforge.application.transform import (
    count_legacy_list,
    format_email_file,
    remove_duplicate_lines,
    write_legacy_list,
)
from mailforge.domain.email import classify_address, extract_addresses
from mailforge.domain.sorting import DNSMode, DNSResult
from mailforge.infrastructure.disposable import DisposableDomainStore
from mailforge.infrastructure.dns import DNSCache, DNSService
from mailforge.infrastructure.providers import ProviderCatalog, ProviderConfigError
from mailforge.interfaces.cli import build_parser


def test_unified_sort_campaign_preserves_outputs_and_summary(tmp_path):
    config_dir = tmp_path / "config with spaces"
    results_dir = tmp_path / "results with spaces"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "gmail": {"output_file": "gmail.txt", "aliases": ["gmail.com"]},
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "disposable.json").write_text('["mailinator.com"]', encoding="utf-8")
    source = tmp_path / "input.jsonl"
    source.write_text(
        "\n".join(
            [
                '{"email": "Alice@GMAIL.COM"}',
                '{"email": "alice@gmail.com"}',
                '{"email": "Support+case@mailinator.com"}',
                '{"email": "δοκιμή@παράδειγμα.δοκιμή"}',
                '{"email": "bad..local@example.com"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sorter = EmailSorter(
        SortConfig(
            results_dir=results_dir,
            config_dir=config_dir,
            log_dir=tmp_path / "logs",
            checkpoint_path=tmp_path / "checkpoint.json",
            checkpoint_interval=1,
            dns_mode=DNSMode.OFFLINE,
        )
    )

    result = sorter.sort_file(source, resume=False)

    assert result.committed
    assert result.summary_path == results_dir / "summary.json"
    assert (results_dir / "gmail.txt").read_text(encoding="utf-8") == "alice@gmail.com\n"
    assert "support+case@mailinator.com\n" in (results_dir / "custom_domains.txt").read_text(
        encoding="utf-8"
    )
    summary = json.loads((results_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["total_extracted"] == 5
    assert summary["counts"]["duplicates_removed"] == 1
    assert summary["counts"]["unique_valid"] == 3
    assert summary["counts"]["disposable"] == 1
    assert not (results_dir / "_temp").exists()
    assert list((tmp_path / "logs").glob("mailforge_*.log"))


def test_dns_sync_async_cache_and_failure_paths(tmp_path):
    class Answer:
        exchange = "aspmx.l.google.com."
        preference = 10

    class SyncResolver:
        calls = 0

        def resolve(self, domain, record_type):
            self.calls += 1
            assert (domain, record_type) == ("example.com", "MX")
            return [Answer()]

    resolver = SyncResolver()
    service = DNSService(
        mode=DNSMode.SYNC,
        cache=DNSCache(max_size=2),
        sync_resolver=resolver,
    )
    first = service.resolve_one_sync("example.com")
    second = service.resolve_one_sync("example.com")
    assert first.result is DNSResult.OK
    assert first.provider_id == "google_workspace"
    assert first is second
    assert resolver.calls == 1

    class AsyncResolver:
        async def query(self, domain, record_type):
            assert (domain, record_type) == ("async.example", "MX")
            return [Answer()]

    async_service = DNSService(
        mode=DNSMode.ASYNC,
        cache=DNSCache(max_size=2),
        async_resolver=AsyncResolver(),
    )
    result = asyncio.run(async_service.resolve_one_async("async.example"))
    assert result.result is DNSResult.OK

    class BrokenResolver:
        def resolve(self, _domain, _record_type):
            raise TimeoutError("synthetic timeout")

    failure = DNSService(mode=DNSMode.SYNC, sync_resolver=BrokenResolver())
    assert failure.resolve_one_sync("slow.example").result is DNSResult.TIMEOUT
    assert failure.preflight_check() is False

    cache_path = tmp_path / "dns-cache.json"
    service.cache.save(cache_path)
    restored = DNSCache(max_size=2)
    assert restored.load(cache_path)
    assert restored.get("example.com") == first


def test_dns_numeric_errors_and_circuit_recovery():
    assert (
        DNSService._exception_result(aiodns.error.DNSError(aiodns.error.ARES_ENOTFOUND, "missing"))
        is DNSResult.NXDOMAIN
    )
    assert (
        DNSService._exception_result(aiodns.error.DNSError(aiodns.error.ARES_ENODATA, "empty"))
        is DNSResult.NO_MX
    )

    class BrokenResolver:
        def resolve(self, _domain, _record_type):
            raise TimeoutError("synthetic timeout")

    class HealthyResolver:
        def resolve(self, _domain, _record_type):
            return []

    service = DNSService(
        mode=DNSMode.SYNC,
        sync_resolver=BrokenResolver(),
        retry_count=1,
        circuit_threshold=1,
        circuit_timeout=60,
    )
    assert service.resolve_one_sync("first.example").result is DNSResult.TIMEOUT
    assert service.resolve_one_sync("second.example").result is DNSResult.CIRCUIT_BROKEN
    service.circuit_timeout = 0
    assert (
        service.resolve_one_sync("third.example", resolver=HealthyResolver()).result
        is DNSResult.NO_MX
    )
    assert (
        service.resolve_one_sync("first.example", resolver=HealthyResolver()).result
        is DNSResult.NO_MX
    )


def test_json_object_lines_and_mixed_unicode_extraction(tmp_path):
    source = tmp_path / "records.json"
    source.write_text(
        '{"email": "one@example.com"}\n{"email": "δοκιμή@παράδειγμα.δοκιμή"}\n',
        encoding="utf-8",
    )
    assert collect_unique_addresses(source) == [
        "one@example.com",
        "δοκιμή@παράδειγμα.δοκιμή",
    ]
    assert extract_addresses("one@example.com and δοκιμή@παράδειγμα.δοκιμή") == [
        "one@example.com",
        "δοκιμή@παράδειγμα.δοκιμή",
    ]
    assert extract_addresses("üser@example.com") == ["üser@example.com"]
    assert extract_addresses("Contact one@example.com.") == ["one@example.com"]
    assert classify_address("user@sub.mailinator.com", {"mailinator.com"})["is_disposable"]


def test_disposable_refresh_is_atomic_and_validates_source(tmp_path):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'["TempMail.COM", "example.test"]'

    path = tmp_path / "config" / "disposable.json"
    count = DisposableDomainStore(path).refresh(
        "https://synthetic.invalid", opener=lambda *_args, **_kwargs: Response()
    )
    assert count == 2
    assert json.loads(path.read_text(encoding="utf-8")) == ["example.test", "tempmail.com"]
    assert DisposableDomainStore(path).load() == frozenset({"example.test", "tempmail.com"})
    provenance = DisposableDomainStore(path).load_provenance()
    assert provenance is not None
    assert provenance["source_url"] == "https://synthetic.invalid"
    assert provenance["domain_count"] == 2


def test_disposable_refresh_redacts_url_and_rolls_back_sidecar_failure(tmp_path, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'["new.example"]'

    path = tmp_path / "disposable.json"
    path.write_text('["old.example"]\n', encoding="utf-8")
    provenance_path = path.with_suffix(path.suffix + ".meta.json")
    provenance_path.write_text('{"source_url": "old"}\n', encoding="utf-8")
    store = DisposableDomainStore(path)
    original_replace = Path.replace
    failed = False

    def fail_once(self, target):
        nonlocal failed
        if target == provenance_path and not failed:
            failed = True
            raise OSError("synthetic sidecar failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_once)

    with pytest.raises(OSError, match="sidecar failure"):
        store.refresh(
            "https://example.invalid/list.json?access_token=secret",
            opener=lambda *_args, **_kwargs: Response(),
        )

    assert store.load() == frozenset({"old.example"})
    assert provenance_path.read_text(encoding="utf-8") == '{"source_url": "old"}\n'

    monkeypatch.setattr(Path, "replace", original_replace)
    store.refresh(
        "https://example.invalid/list.json?access_token=secret",
        opener=lambda *_args, **_kwargs: Response(),
    )
    assert store.load_provenance()["source_url"] == "https://example.invalid/list.json"
    assert "mailinator.com" in DisposableDomainStore(tmp_path / "missing.json").load()


def test_legacy_transformations_are_owned_by_modern_service(tmp_path):
    source = tmp_path / "emails.txt"
    formatted = tmp_path / "formatted.txt"
    listed = tmp_path / "listed.txt"
    source.write_text("a@example.com\n\na@example.com\n", encoding="utf-8")
    format_email_file(source, formatted)
    assert remove_duplicate_lines(formatted) == 1
    write_legacy_list(formatted, listed)
    assert count_legacy_list(listed) == 1


def test_jsonl_field_extractor_preserves_legacy_e_field_contract(tmp_path):
    source = tmp_path / "records.jsonl"
    output = tmp_path / "emails.txt"
    source.write_text(
        '{"e": "Alice@Example.com"}\n{"e": "alice@example.com"}\n'
        '{"e": "bad"}\n{"other": "ignored@example.com"}\n',
        encoding="utf-8",
    )
    values = list(iter_jsonl_email_field(source))
    output.write_text("\n".join(values) + "\n", encoding="utf-8")
    assert len(values) == 1
    assert output.read_text(encoding="utf-8") == "alice@example.com\n"


def test_legacy_jsonl_extractor_allows_explicit_size_limit(tmp_path):
    source = tmp_path / "records.jsonl"
    source.write_text('{"e": "alice@example.com"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        list(iter_jsonl_email_field(source, max_bytes=1))


def test_provider_configuration_rejects_path_escape(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps({"bad": {"output_file": "../outside.txt", "aliases": []}}),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError):
        ProviderCatalog(config)


def test_interrupted_campaign_resumes_idempotently(tmp_path, monkeypatch):
    source = tmp_path / "input.txt"
    source.write_text(
        "one@example.com\ntwo@example.com\nthree@example.com\nfour@example.com\n",
        encoding="utf-8",
    )
    config = SortConfig(
        results_dir=tmp_path / "results",
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        checkpoint_path=tmp_path / "checkpoint.json",
        checkpoint_interval=1,
    )
    original = InputReader.iter_emails

    def interrupted(path, **kwargs):
        for index, value in enumerate(original(path, **kwargs)):
            if index == 2:
                raise KeyboardInterrupt
            yield value

    monkeypatch.setattr(InputReader, "iter_emails", staticmethod(interrupted))
    with pytest.raises(KeyboardInterrupt):
        EmailSorter(config).sort_file(source, resume=False)
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["completed"] is False
    assert checkpoint["position"] == 2
    assert checkpoint["seen_domains"] == ["example.com"]
    assert checkpoint["pending_domains"] == ["example.com"]

    monkeypatch.setattr(InputReader, "iter_emails", staticmethod(original))
    result = EmailSorter(config).sort_file(source, resume=True)
    assert result.committed
    values = (config.results_dir / "custom_domains.txt").read_text(encoding="utf-8").splitlines()
    assert values == [
        "one@example.com",
        "two@example.com",
        "three@example.com",
        "four@example.com",
    ]


def test_fresh_retry_preserves_checkpoint_when_results_are_not_empty(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\n", encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    (results / "existing.txt").write_text("keep\n", encoding="utf-8")
    checkpoint_path = tmp_path / "checkpoint.json"
    Checkpoint(checkpoint_path).save(str(source), 1, 1)
    before = checkpoint_path.read_text(encoding="utf-8")
    config = SortConfig(results_dir=results, checkpoint_path=checkpoint_path)
    with pytest.raises(RuntimeError):
        EmailSorter(config).sort_file(source, resume=False)
    assert checkpoint_path.read_text(encoding="utf-8") == before


def test_legacy_checkpoint_without_output_boundary_is_rejected_safely(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\n", encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    output = results / "custom_domains.txt"
    output.write_text("one@example.com\n", encoding="utf-8")
    checkpoint_path = tmp_path / "checkpoint.json"
    Checkpoint(checkpoint_path).save(str(source), 1, 1)
    with pytest.raises(RuntimeError, match="output-boundary metadata"):
        EmailSorter(SortConfig(results_dir=results, checkpoint_path=checkpoint_path)).sort_file(
            source, resume=True
        )
    assert output.read_text(encoding="utf-8") == "one@example.com\n"


def test_resume_truncates_uncheckpointed_output_after_write_failure(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("one@example.com\ntwo@example.com\n", encoding="utf-8")
    config = SortConfig(
        results_dir=tmp_path / "results",
        checkpoint_path=tmp_path / "checkpoint.json",
        checkpoint_interval=1,
    )
    sorter = EmailSorter(config)
    original_flush = sorter._flush_outputs
    calls = 0

    def fail_after_flush(handles, master):
        nonlocal calls
        original_flush(handles, master)
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic flush failure")

    sorter._flush_outputs = fail_after_flush
    with pytest.raises(RuntimeError):
        sorter.sort_file(source, resume=False)
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["position"] == 1

    result = EmailSorter(config).sort_file(source, resume=True)
    assert result.committed
    assert (config.results_dir / "custom_domains.txt").read_text(encoding="utf-8").splitlines() == [
        "one@example.com",
        "two@example.com",
    ]
    assert len((config.results_dir / "master.csv").read_text(encoding="utf-8").splitlines()) == 3


def test_cli_controls_and_cleanup_are_available(tmp_path):
    parser = build_parser()
    args = parser.parse_args(["sort", "input.csv", "--dns-mode", "sync", "--resume", "--debug"])
    assert args.command == "sort"
    assert args.dns_mode == "sync"
    assert parser.parse_args(["sort", "input.csv", "--fresh"]).resume is False
    results = tmp_path / "results"
    results.mkdir()
    (results / "summary.json").write_text("{}", encoding="utf-8")
    sorter = EmailSorter(SortConfig(results_dir=results, log_dir=tmp_path / "logs"))
    assert sorter.view_summary() == {}
    assert sorter.delete_results(confirm=True)
    assert not results.exists()
