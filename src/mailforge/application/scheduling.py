"""UTC-backed, time-zone-aware scheduling primitives."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailforge.domain.outbound import CatchUpPolicy, ScheduleMode, ScheduleSpec


class ScheduleError(ValueError):
    """Raised for invalid or non-resolvable local schedule times."""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ScheduleError(f"unknown IANA timezone: {name}") from error


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


def resolve_local_time(value: str, timezone: str, *, fold: int | None = None) -> datetime:
    naive = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if naive.tzinfo is not None:
        return naive.astimezone(UTC)
    zone = _zone(timezone)
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=candidate_fold)
        round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and aware.astimezone(UTC) not in candidates:
            candidates.append(aware.astimezone(UTC))
    if not candidates:
        raise ScheduleError("scheduled local time does not exist in its timezone")
    if len(candidates) > 1:
        if fold not in (0, 1):
            raise ScheduleError("ambiguous local time requires fold=0 or fold=1")
        return candidates[fold]
    return candidates[0]


def _local_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise ScheduleError(f"invalid sending-window time: {value}") from error


class ScheduleEngine:
    def validate(self, schedule: ScheduleSpec) -> None:
        _zone(schedule.timezone)
        if not schedule.weekdays or any(day not in range(7) for day in schedule.weekdays):
            raise ScheduleError("weekdays must contain values from 0 through 6")
        if schedule.mode in {ScheduleMode.AT, ScheduleMode.RECURRING}:
            if not schedule.scheduled_at:
                raise ScheduleError("scheduled_at is required for this schedule mode")
            resolve_local_time(schedule.scheduled_at, schedule.timezone, fold=schedule.fold)
        if schedule.mode is ScheduleMode.RECURRING and schedule.recurrence not in {
            "daily",
            "weekly",
        }:
            raise ScheduleError("recurrence must be daily or weekly")
        if schedule.mode is ScheduleMode.WINDOW:
            if not schedule.window_start or not schedule.window_end:
                raise ScheduleError("window_start and window_end are required")
            _local_time(schedule.window_start)
            _local_time(schedule.window_end)
        if schedule.pacing_seconds < 0 or schedule.cooldown_seconds < 0:
            raise ScheduleError("pacing and cooldown cannot be negative")
        if schedule.catch_up_policy is CatchUpPolicy.SKIP and schedule.recurrence:
            return

    def due(self, schedule: ScheduleSpec, now: datetime) -> bool:
        self.validate(schedule)
        current = _utc(now)
        if schedule.mode is ScheduleMode.IMMEDIATE:
            return True
        if schedule.mode is ScheduleMode.AT:
            return bool(schedule.scheduled_at) and current >= resolve_local_time(
                schedule.scheduled_at, schedule.timezone, fold=schedule.fold
            )
        if schedule.mode is ScheduleMode.RECURRING:
            occurrence = self._recurring_occurrence(schedule, current)
            if schedule.catch_up_policy is CatchUpPolicy.SKIP:
                zone = _zone(schedule.timezone)
                return (
                    current.astimezone(zone).date() == occurrence.astimezone(zone).date()
                    and current >= occurrence
                )
            return current >= occurrence
        local = current.astimezone(_zone(schedule.timezone))
        if local.weekday() not in schedule.weekdays:
            return False
        start = _local_time(schedule.window_start or "00:00")
        end = _local_time(schedule.window_end or "23:59:59")
        if start <= end:
            return start <= local.timetz().replace(tzinfo=None) <= end
        return (
            local.timetz().replace(tzinfo=None) >= start
            or local.timetz().replace(tzinfo=None) <= end
        )

    def next_eligible(self, schedule: ScheduleSpec, now: datetime) -> datetime | None:
        self.validate(schedule)
        current = _utc(now)
        if schedule.mode is ScheduleMode.RECURRING:
            occurrence = self._recurring_occurrence(schedule, current)
            if current < occurrence:
                return occurrence
            if schedule.catch_up_policy is CatchUpPolicy.NEXT_WINDOW and self.due(
                schedule, current
            ):
                return current
            return self._recurring_occurrence(schedule, current, next_only=True)
        if self.due(schedule, current):
            return current
        if schedule.mode is ScheduleMode.AT and schedule.scheduled_at:
            scheduled = resolve_local_time(
                schedule.scheduled_at, schedule.timezone, fold=schedule.fold
            )
            if scheduled > current:
                return scheduled
        local = current.astimezone(_zone(schedule.timezone))
        for offset in range(0, 8):
            day = (local + timedelta(days=offset)).date()
            if day.weekday() not in schedule.weekdays:
                continue
            start = _local_time(schedule.window_start or "00:00")
            candidate = datetime.combine(day, start, _zone(schedule.timezone)).astimezone(UTC)
            if candidate > current:
                return candidate
        return None

    def _recurring_occurrence(
        self, schedule: ScheduleSpec, now: datetime, *, next_only: bool = False
    ) -> datetime:
        if not schedule.scheduled_at or schedule.recurrence not in {"daily", "weekly"}:
            raise ScheduleError("recurring schedule is missing a supported recurrence")
        zone = _zone(schedule.timezone)
        initial = resolve_local_time(schedule.scheduled_at, schedule.timezone, fold=schedule.fold)
        initial_local = initial.astimezone(zone)
        current_local = _utc(now).astimezone(zone)
        if current_local < initial_local:
            return initial
        days = (current_local.date() - initial_local.date()).days
        if schedule.recurrence == "weekly":
            days -= days % 7
        if next_only:
            days = days + 1 if schedule.recurrence == "daily" else days - days % 7 + 7
        candidate_date = initial_local.date() + timedelta(days=days)
        local_value = datetime.combine(candidate_date, initial_local.timetz().replace(tzinfo=None))
        return resolve_local_time(local_value.isoformat(), schedule.timezone, fold=schedule.fold)
