"""Standalone list comparison over the canonical address parser.

Comparison is intentionally read-only.  It accepts files, saved lists, and
selected successful submission history, but never imports contacts or changes
campaign state.  All address identity decisions stay in ``domain.email``.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from mailforge.application.ingest import InputLimitError, iter_raw_values
from mailforge.domain.email import extract_addresses, normalize_email, validate_email
from mailforge.infrastructure.campaign_store import CampaignStore

MAX_COMPARISON_BYTES = 1_073_741_824
MAX_RESULT_RECORDS = 250_000


class ListComparisonError(ValueError):
    """A user-correctable comparison request error."""

    def __init__(self, message: str, code: str = "list_comparison_invalid"):
        super().__init__(message)
        self.code = code


class ListComparisonService:
    """Compare address sets without creating contacts or requiring SMTP setup."""

    def __init__(self, store: CampaignStore):
        self.store = store

    def compare_files(
        self,
        main_path: str | Path,
        reference_paths: Sequence[str | Path] = (),
        *,
        operation: str = "subtract",
        history_campaign_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        main = self._read_file(main_path)
        references = [self._read_file(path) for path in reference_paths]
        history = self._successful_history(history_campaign_ids)
        if history:
            references.append(self._from_emails(history, "sending history"))
        return self._compare(main, references, operation=operation)

    def compare_saved_lists(
        self,
        main_list_id: str,
        reference_list_ids: Sequence[str] = (),
        *,
        operation: str = "subtract",
        history_campaign_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        main = self._read_saved_list(main_list_id)
        references = [self._read_saved_list(list_id) for list_id in reference_list_ids]
        history = self._successful_history(history_campaign_ids)
        if history:
            references.append(self._from_emails(history, "sending history"))
        return self._compare(main, references, operation=operation)

    def history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.name, c.status, c.created_at, c.completed_at,
                       COUNT(
                           DISTINCT CASE WHEN a.outcome = 'accepted' THEN i.normalized_email END
                       ) AS accepted,
                       COUNT(DISTINCT i.normalized_email) AS prepared
                FROM campaigns c
                LEFT JOIN delivery_intents i ON i.campaign_id = c.id
                LEFT JOIN delivery_attempts a ON a.intent_id = i.id
                GROUP BY c.id
                ORDER BY COALESCE(c.completed_at, c.updated_at) DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, result: dict[str, Any]) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["email", "classification", "source"])
        for item in result.get("results", []):
            writer.writerow([item["email"], item["classification"], item["source"]])
        return output.getvalue()

    def _read_file(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.is_file():
            raise ListComparisonError("comparison input was not retained", "input_missing")
        if path.stat().st_size > MAX_COMPARISON_BYTES:
            raise ListComparisonError(
                "comparison input exceeds the local processing limit", "input_limit"
            )
        emails: list[str] = []
        invalid = 0
        raw_values = 0
        try:
            for raw in iter_raw_values(path, max_bytes=MAX_COMPARISON_BYTES):
                raw_values += 1
                candidates = extract_addresses(raw)
                if not candidates:
                    invalid += 1
                    continue
                for candidate in candidates:
                    normalized = normalize_email(candidate)
                    if validate_email(normalized).valid:
                        emails.append(normalized)
                    else:
                        invalid += 1
        except (OSError, InputLimitError) as error:
            raise ListComparisonError(str(error), "input_invalid") from error
        return self._from_emails(emails, path.name, invalid=invalid, raw_values=raw_values)

    def _read_saved_list(self, list_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            exists = connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone()
        if exists is None:
            raise ListComparisonError("saved list was not found", "list_not_found")
        rows = self.store.list_contacts(list_id)
        return self._from_emails((row["normalized_email"] for row in rows), f"saved list:{list_id}")

    @staticmethod
    def _from_emails(
        emails: Iterable[str], source: str, *, invalid: int = 0, raw_values: int | None = None
    ) -> dict[str, Any]:
        records = list(emails)
        unique = set(records)
        counts = Counter(records)
        return {
            "source": source,
            "emails": unique,
            "email_counts": dict(counts),
            "input_count": raw_values if raw_values is not None else len(records),
            "valid_count": len(records),
            "unique_count": len(unique),
            "duplicate_count": max(0, len(records) - len(unique)),
            "invalid_count": invalid,
        }

    def _successful_history(self, campaign_ids: Sequence[str]) -> set[str]:
        if not campaign_ids:
            return set()
        placeholders = ",".join("?" for _ in campaign_ids)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT i.normalized_email
                FROM delivery_intents i
                JOIN delivery_attempts a ON a.intent_id = i.id
                WHERE i.campaign_id IN ({placeholders}) AND a.outcome = 'accepted'
                """,
                tuple(campaign_ids),
            ).fetchall()
        return {row["normalized_email"] for row in rows}

    def _compare(
        self, main: dict[str, Any], references: Sequence[dict[str, Any]], *, operation: str
    ) -> dict[str, Any]:
        operation = {
            "difference": "subtract",
            "overlap": "intersection",
            "merge": "union",
            "unique": "unique_main",
        }.get(operation, operation)
        valid_operations = {"subtract", "intersection", "union", "unique_main", "duplicates"}
        if operation not in valid_operations:
            raise ListComparisonError("unsupported comparison operation", "operation_invalid")
        main_set = set(main["emails"])
        reference_sets = [set(item["emails"]) for item in references]
        reference_union = set().union(*reference_sets) if reference_sets else set()
        if operation == "subtract":
            selected = main_set - reference_union
            classification = "only in main list"
        elif operation == "intersection":
            selected = main_set & reference_union
            classification = "in both selections"
        elif operation == "union":
            selected = main_set | reference_union
            classification = "merged unique address"
        elif operation == "unique_main":
            selected = main_set - reference_union
            classification = "unique to main list"
        else:
            selected = {email for email, count in main["email_counts"].items() if count > 1}
            classification = "duplicate in main list"

        ordered = sorted(selected)
        if len(ordered) > MAX_RESULT_RECORDS:
            raise ListComparisonError(
                "comparison result is too large to return in one response; "
                "export the source lists in smaller batches",
                "result_limit",
            )
        reference_names = ", ".join(item["source"] for item in references) or "none"
        results = [
            {"email": email, "classification": classification, "source": main["source"]}
            for email in ordered
        ]
        return {
            "operation": operation,
            "main": self._public_set(main),
            "references": [self._public_set(item) for item in references],
            "reference_label": reference_names,
            "counts": {
                "result": len(results),
                "main_unique": len(main_set),
                "reference_unique": len(reference_union),
                "main_duplicates": main["duplicate_count"],
                "main_invalid": main["invalid_count"],
            },
            "results": results,
            "non_destructive": True,
            "history_is_successful_submission_only": bool(
                any(item["source"] == "sending history" for item in references)
            ),
        }

    @staticmethod
    def _public_set(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key != "emails"}
