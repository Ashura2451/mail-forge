"""Deterministic sender/provider policy selection for synthetic dispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from mailforge.domain.email import validate_email
from mailforge.domain.outbound import PolicyDecision, SenderPolicy


class PolicyConfigurationError(ValueError):
    """Raised when a sender policy is invalid or incomplete."""


def _policy(row) -> SenderPolicy:
    configuration = json.loads(row["configuration_json"])
    return SenderPolicy(
        ref=row["ref"],
        provider=row["provider"],
        email=row["email"],
        display_name=row["display_name"],
        reply_to=row["reply_to"],
        max_per_minute=row["max_per_minute"],
        max_per_hour=row["max_per_hour"],
        max_per_day=row["max_per_day"],
        per_domain_per_minute=row["per_domain_per_minute"],
        per_domain_per_hour=int(configuration.get("per_domain_per_hour", 0)),
        max_concurrency=row["max_concurrency"],
        cooldown_seconds=row["cooldown_seconds"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
    )


class PolicyEngine:
    def __init__(self, store, *, now_fn=None):
        self.store = store
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def validate(self, policy: SenderPolicy) -> None:
        if not validate_email(policy.email).valid:
            raise PolicyConfigurationError(f"invalid sender address for policy {policy.ref}")
        if policy.reply_to and not validate_email(policy.reply_to).valid:
            raise PolicyConfigurationError(f"invalid reply-to address for policy {policy.ref}")
        if (
            any(
                value < 0
                for value in (
                    policy.max_per_minute,
                    policy.max_per_hour,
                    policy.max_per_day,
                    policy.per_domain_per_minute,
                    policy.per_domain_per_hour,
                    policy.cooldown_seconds,
                )
            )
            or policy.max_concurrency < 1
        ):
            raise PolicyConfigurationError(f"invalid limits for policy {policy.ref}")

    def policies(self) -> list[SenderPolicy]:
        policies = [_policy(row) for row in self.store.list_sender_policies()]
        for policy in policies:
            self.validate(policy)
        return sorted(policies, key=lambda item: (item.priority, item.ref))

    def _configuration(self, policy_ref: str) -> dict[str, object]:
        row = next(
            (item for item in self.store.list_sender_policies() if item["ref"] == policy_ref),
            None,
        )
        return json.loads(row["configuration_json"]) if row else {}

    def _adaptive_decision(
        self, policy: SenderPolicy, domain: str, configuration: dict[str, object], now: datetime
    ) -> PolicyDecision | None:
        adaptive = configuration.get("adaptive_domain_throttle", {})
        if not isinstance(adaptive, dict) or not adaptive.get("enabled", False):
            return None
        threshold = float(adaptive.get("bounce_rate_threshold", 0.2))
        minimum = int(adaptive.get("min_samples", 5))
        cooldown = max(1, int(adaptive.get("cooldown_minutes", 60)))
        since = (now - timedelta(minutes=cooldown)).isoformat()
        stats = self.store.sender_policy_outcome_counts(policy.ref, domain=domain, since=since)
        if stats["total"] < minimum or stats["failures"] / stats["total"] < threshold:
            return None
        return PolicyDecision(
            False,
            policy.ref,
            "adaptive domain cooldown",
            (now + timedelta(minutes=cooldown)).isoformat(),
        )

    def _reputation_decision(
        self, policy: SenderPolicy, configuration: dict[str, object], now: datetime
    ) -> PolicyDecision | None:
        reputation = configuration.get("smtp_reputation", {})
        if not isinstance(reputation, dict) or not reputation.get("enabled", False):
            return None
        threshold = float(reputation.get("failure_rate_threshold", 0.3))
        minimum = int(reputation.get("min_samples", 10))
        cooldown = max(1, int(reputation.get("cooldown_minutes", 60)))
        since = (now - timedelta(minutes=cooldown)).isoformat()
        stats = self.store.sender_policy_outcome_counts(policy.ref, since=since)
        if stats["total"] < minimum or stats["failures"] / stats["total"] < threshold:
            return None
        return PolicyDecision(
            False,
            policy.ref,
            "SMTP reputation cooldown",
            (now + timedelta(minutes=cooldown)).isoformat(),
        )

    def _warmup_limit(
        self, policy: SenderPolicy, configuration: dict[str, object], now: datetime
    ) -> tuple[int, str] | None:
        warmup = configuration.get("warmup", {})
        if not isinstance(warmup, dict) or not warmup.get("enabled", False):
            return None
        ramp_days = max(1, int(warmup.get("ramp_days", 7)))
        started = str(warmup.get("started_at", now.date().isoformat()))
        try:
            days = max(0, (now.date() - datetime.fromisoformat(started).date()).days)
        except ValueError:
            days = ramp_days
        multiplier = min(1.0, (days + 1) / ramp_days)
        return max(1, int(policy.max_per_day * multiplier)), now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

    def reserve(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        recipient: str,
        preferred_ref: str | None = None,
    ) -> PolicyDecision:
        domain = recipient.rsplit("@", 1)[-1].lower()
        now = self.now_fn().astimezone(UTC)
        retry_at = None
        policies = self.policies()
        if preferred_ref is not None:
            policies = [policy for policy in policies if policy.ref == preferred_ref]
        for policy in policies:
            if not policy.enabled:
                continue
            configuration = self._configuration(policy.ref)
            adaptive = self._adaptive_decision(policy, domain, configuration, now)
            if adaptive is not None:
                retry_at = retry_at or adaptive.retry_at
                continue
            reputation = self._reputation_decision(policy, configuration, now)
            if reputation is not None:
                retry_at = retry_at or reputation.retry_at
                continue
            warmup = self._warmup_limit(policy, configuration, now)
            if warmup is not None:
                limit, since = warmup
                if (
                    self.store.sender_policy_outcome_counts(policy.ref, since=since)["total"]
                    >= limit
                ):
                    retry_at = retry_at or (now + timedelta(days=1)).isoformat()
                    continue
            else:
                limit = None
            decision = self.store.try_reserve_policy_slot(
                attempt_id=attempt_id,
                intent_id=intent_id,
                policy=policy,
                domain=domain,
                now=now,
                warmup_max_per_day=limit,
            )
            if decision.allowed:
                return PolicyDecision(True, policy.ref, "policy_selected")
            retry_at = retry_at or decision.retry_at
        reason = (
            f"sender policy unavailable: {preferred_ref}"
            if preferred_ref
            else "no sender policy capacity"
        )
        return PolicyDecision(False, reason=reason, retry_at=retry_at)

    @staticmethod
    def sender_domain(policy: SenderPolicy) -> str:
        return parseaddr(policy.email)[1].rsplit("@", 1)[-1].lower()
