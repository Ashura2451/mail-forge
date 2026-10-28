"""Synthetic-only rehearsal for mapping legacy MAILER files into the core."""

from __future__ import annotations

from pathlib import Path

from mailforge.application.mailer_compat import migrate_legacy_state
from mailforge.infrastructure.campaign_store import CampaignStore


def rehearse_legacy_migration(
    store: CampaignStore,
    *,
    list_id: str,
    campaign_id: str,
    sent_path: str | Path,
    failed_path: str | Path,
    hard_bounces_path: str | Path,
) -> dict[str, int]:
    """Compatibility wrapper around the canonical state rehearsal."""

    report = migrate_legacy_state(
        store,
        list_id=list_id,
        campaign_id=campaign_id,
        sent_path=sent_path,
        failed_path=failed_path,
        hard_bounces_path=hard_bounces_path,
    )
    return {
        key: report.counts[key] for key in ("sent_ambiguous", "failed_deferred", "hard_bounces")
    }
