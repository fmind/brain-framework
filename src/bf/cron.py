"""Zero-dependency 5-field POSIX cron & @macro evaluator using Python stdlib `datetime`/`zoneinfo`."""

from __future__ import annotations

import calendar
import hashlib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cache
from typing import Protocol
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduleSpec:
    raw: str
    every: int | None = None
    cron: str = ""
    backfill: int | None = None
    overlap: int | None = None


class _ScheduledLoop(Protocol):
    @property
    def enabled(self) -> bool: ...
    @property
    def daemon(self) -> bool: ...
    @property
    def schedules(self) -> tuple[ScheduleSpec, ...]: ...
    @property
    def backfill(self) -> int: ...
    @property
    def overlap(self) -> int: ...


_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_wild: bool
    dow_wild: bool


def _parse_val(tok: str, lo: int, hi: int, names: dict[str, int] | None, *, dow: bool = False) -> int:
    low = tok.lower()
    if names and low in names:
        return names[low]
    if not low.isdigit():
        raise ValueError(f"invalid cron token {tok!r}")
    val = int(low)
    if dow and val == 7:
        return 0
    if val < lo or val > hi:
        raise ValueError(f"cron value {val} out of range {lo}..{hi}")
    return val


def _parse_field(
    field: str, lo: int, hi: int, names: dict[str, int] | None = None, *, dow: bool = False
) -> tuple[frozenset[int], bool]:
    raw = field.strip()
    is_wild = raw == "*"
    out: set[int] = set()
    for part in raw.split(","):
        if not part:
            raise ValueError(f"empty list item in {field!r}")
        base, sep, step_s = part.partition("/")
        step = 1
        if sep and (not step_s.isdigit() or (step := int(step_s)) < 1):
            raise ValueError(f"invalid cron step {step_s!r}")
        if base == "*":
            out.update(range(lo, hi + 1, step))
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            if "-" in b_s:
                raise ValueError(f"invalid cron range {base!r}")
            if dow:
                a = _parse_val(a_s, 0, 7, names, dow=False)
                b = _parse_val(b_s, 0, 7, names, dow=False)
                if b == 0 and b_s.lower() == "sun" and a > 0:
                    b = 7
                if a > b:
                    raise ValueError(f"invalid dow range {base!r}")
                out.update((v % 7) for v in range(a, b + 1, step))
            else:
                a = _parse_val(a_s, lo, hi, names, dow=False)
                b = _parse_val(b_s, lo, hi, names, dow=False)
                if a > b:
                    raise ValueError(f"invalid cron range {base!r}")
                out.update(range(a, b + 1, step))
        else:
            if sep:
                start = _parse_val(base, lo, hi, names, dow=dow)
                out.update(range(start, hi + 1, step))
            else:
                out.add(_parse_val(base, lo, hi, names, dow=dow))
    return frozenset(out), is_wild


@cache
def parse_cron(expr: str) -> CronSpec:
    """Parse a 5-field POSIX cron expression or `@macro` into a compiled `CronSpec`."""
    norm = " ".join(expr.strip().split())
    if norm.startswith("@"):
        if (expanded := _MACROS.get(norm.lower())) is None:
            raise ValueError(f"unknown cron macro {norm!r}")
        norm = expanded
    parts = norm.split(" ")
    if len(parts) != 5:
        raise ValueError("cron requires 5 whitespace-separated fields or @macro")
    mins, _ = _parse_field(parts[0], 0, 59)
    hrs, _ = _parse_field(parts[1], 0, 23)
    doms, dom_wild = _parse_field(parts[2], 1, 31)
    months, _ = _parse_field(parts[3], 1, 12, _MONTHS)
    dows, dow_wild = _parse_field(parts[4], 0, 6, _DOWS, dow=True)
    # Verify at least one calendar day is reachable in a 4-year leap cycle.
    valid_day = any(d <= calendar.monthrange(yr, mo)[1] for yr in (2024, 2025) for mo in months for d in doms)
    if not valid_day:
        raise ValueError(f"cron expression {expr!r} matches no valid calendar date")
    return CronSpec(mins, hrs, doms, months, dows, dom_wild, dow_wild)


def is_valid_cron(expr: str) -> bool:
    try:
        parse_cron(expr)
    except ValueError:
        return False
    return True


def _day_matches(spec: CronSpec, year: int, month: int, day: int) -> bool:
    # Python weekday(): Mon=0..Sun=6 -> POSIX cron: Sun=0, Mon=1..Sat=6
    cron_dow = (calendar.weekday(year, month, day) + 1) % 7
    dom_ok = day in spec.doms
    dow_ok = cron_dow in spec.dows
    if spec.dom_wild and spec.dow_wild:
        return True
    if spec.dom_wild:
        return dow_ok
    if spec.dow_wild:
        return dom_ok
    return dom_ok or dow_ok


def _to_utc_instants(year: int, month: int, day: int, hour: int, minute: int, tz: tzinfo) -> tuple[datetime, ...]:
    """Resolve local wall-clock `(y, m, d, H, M)` in `tz` to 1 or 2 UTC instants (handling DST spring-forward and fall-back)."""
    if not isinstance(tz, ZoneInfo):
        return (datetime(year, month, day, hour, minute, tzinfo=UTC),)
    utc0 = datetime(year, month, day, hour, minute, tzinfo=tz, fold=0).astimezone(UTC)
    utc1 = datetime(year, month, day, hour, minute, tzinfo=tz, fold=1).astimezone(UTC)
    if utc1 != utc0:
        back0 = utc0.astimezone(tz)
        back1 = utc1.astimezone(tz)
        if (back0.year, back0.month, back0.day, back0.hour, back0.minute) == (year, month, day, hour, minute) and (
            back1.year,
            back1.month,
            back1.day,
            back1.hour,
            back1.minute,
        ) == (year, month, day, hour, minute):
            return (utc0, utc1) if utc0 < utc1 else (utc1, utc0)
    # Normal wall time or spring-forward gap (fires at the transition instant utc0, matching Vixie cron).
    return (utc0,)


def next_cron(expr: str, when: datetime) -> datetime:
    """Return the earliest UTC fire time strictly after `when` matching `expr` in `when.tzinfo`."""
    spec = parse_cron(expr)
    tz = when.tzinfo if isinstance(when.tzinfo, ZoneInfo) else UTC
    utc_ref = when.astimezone(UTC)
    # Seed slightly behind `utc_ref` in ZoneInfo timezones so DST spring-forward gaps (e.g. 02:30 -> 03:30)
    # are never skipped when local wall-clock jumps forward; `inst > utc_ref` filters out past instants.
    seed = (
        (utc_ref - timedelta(hours=2))
        if isinstance(tz, ZoneInfo)
        else (utc_ref.replace(second=0, microsecond=0) + timedelta(minutes=1))
    )
    cur = seed.astimezone(tz)
    end_year = cur.year + 8
    y, mo, d, h = cur.year, cur.month, cur.day, cur.hour
    min_hr = min(spec.hours)
    sorted_hrs, sorted_mins = sorted(spec.hours), sorted(spec.minutes)
    sorted_months = sorted(spec.months)

    while y <= end_year:
        if mo not in spec.months:
            nxt_mo = next((m for m in sorted_months if m > mo), None)
            if nxt_mo is None:
                y, mo, d, h = y + 1, sorted_months[0], 1, min_hr
            else:
                mo, d, h = nxt_mo, 1, min_hr
            continue
        days_in_mo = calendar.monthrange(y, mo)[1]
        if d > days_in_mo:
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
            d, h = 1, min_hr
            continue
        if not _day_matches(spec, y, mo, d):
            d, h = d + 1, min_hr
            continue
        if h not in spec.hours:
            nxt_h = next((hr for hr in sorted_hrs if hr > h), None)
            if nxt_h is None:
                d, h = d + 1, min_hr
            else:
                h = nxt_h
            continue
        # Check all matching minutes in hour `h` across both DST folds so repeated fall-back hours never skip `fold=1`.
        best: datetime | None = None
        for mn in sorted_mins:
            for inst in _to_utc_instants(y, mo, d, h, mn, tz):
                if inst > utc_ref and (best is None or inst < best):
                    best = inst
        if best is not None:
            return best.astimezone(tz)
        nxt_h = next((hr for hr in sorted_hrs if hr > h), None)
        if nxt_h is None:
            d, h = d + 1, min_hr
        else:
            h = nxt_h
    raise ValueError(f"no next fire time for cron expression {expr!r}")


def prev_cron(expr: str, when: datetime) -> datetime:
    """Return the latest fire time at or before `when` matching `expr` in `when.tzinfo`."""
    spec = parse_cron(expr)
    tz = when.tzinfo if isinstance(when.tzinfo, ZoneInfo) else UTC
    utc_ref = when.astimezone(UTC)
    cur = utc_ref.replace(second=0, microsecond=0).astimezone(tz)
    min_year = cur.year - 8
    y, mo, d, h = cur.year, cur.month, cur.day, cur.hour
    max_hr = max(spec.hours)
    rev_hrs, rev_mins = sorted(spec.hours, reverse=True), sorted(spec.minutes, reverse=True)
    rev_months = sorted(spec.months, reverse=True)

    while y >= min_year:
        if mo not in spec.months:
            prv_mo = next((m for m in rev_months if m < mo), None)
            if prv_mo is None:
                y, mo = y - 1, rev_months[0]
            else:
                mo = prv_mo
            d, h = calendar.monthrange(y, mo)[1], max_hr
            continue
        if d < 1:
            mo -= 1
            if mo < 1:
                y, mo = y - 1, 12
            d, h = calendar.monthrange(y, mo)[1], max_hr
            continue
        if not _day_matches(spec, y, mo, d):
            d, h = d - 1, max_hr
            continue
        if h not in spec.hours:
            prv_h = next((hr for hr in rev_hrs if hr < h), None)
            if prv_h is None:
                d, h = d - 1, max_hr
            else:
                h = prv_h
            continue
        best: datetime | None = None
        for mn in rev_mins:
            for inst in reversed(_to_utc_instants(y, mo, d, h, mn, tz)):
                if inst <= utc_ref and (best is None or inst > best):
                    best = inst
        if best is not None:
            return best.astimezone(tz)
        prv_h = next((hr for hr in rev_hrs if hr < h), None)
        if prv_h is None:
            d, h = d - 1, max_hr
        else:
            h = prv_h
    raise ValueError(f"no previous fire time for cron expression {expr!r}")


def parse_dt(raw: str) -> datetime | None:
    """Parse an ISO timestamp with explicit timezone into a `datetime`, else `None`."""
    with suppress(ValueError):
        return dt if raw and (dt := datetime.fromisoformat(raw)).tzinfo is not None else None
    return None


def resolve_tz(name: str) -> tzinfo:
    """Return `datetime.UTC` for `'UTC'` (works without system tzdata) or `ZoneInfo(name)` for IANA zones."""
    return UTC if name == "UTC" else ZoneInfo(name)


_MIN_BACKOFF_CAP = 3600
_MAX_BACKOFF_CAP = 21_600


def backoff_cap(loop: _ScheduledLoop) -> int:
    """Cap consecutive-failure backoff between 1h and 6h (`2 * min(every)` when > 30m) to avoid offline retry storms."""
    base = min((s.every for s in loop.schedules if s.every is not None), default=0)
    return min(max(base * 2, _MIN_BACKOFF_CAP), _MAX_BACKOFF_CAP)


def backoff_remaining(doc: Mapping[str, object], cap: int, now: datetime) -> float:
    raw_sf = doc.get("schedule_failures")
    ev_f = int(ev_v) if type(ev_v := doc.get("event_failures")) is int else 0
    dl_n = int(dl_v) if type(dl_v := doc.get("dead_lettered")) is int else 0
    consec = int(val) if type(val := doc.get("consecutive_failures")) is int else 0
    fails = raw_sf if type(raw_sf) is int and (raw_sf > 0 or ev_f > 0 or dl_n > 0) else consec
    incomp = int(iv) if type(iv := doc.get("incomplete_runs")) is int else 0
    attempts = max(fails, incomp)
    last_end = str(doc.get("last_schedule_end") or doc.get("last_end") or "")
    last_dt = parse_dt(last_end) if attempts > 0 else None
    if last_dt is None:
        return 0.0
    raw_wait = min(60 * (1 << min(attempts - 1, 10)), cap)
    seed = f"{doc.get('name', '')}:{attempts}:{last_end}".encode()
    jitter = 0.85 + 0.15 * (int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") / 0xFFFFFFFF)
    wait = raw_wait * jitter
    return max(0.0, wait - max(0.0, (now - last_dt).total_seconds()))


def event_backoff_remaining(doc: Mapping[str, object], now: datetime) -> float:
    """Short exponential retry backoff (`10s..60s`) for failing event triggers, isolated from schedule failures."""
    ev_fails = int(val) if type(val := doc.get("event_failures")) is int else 0
    last_end = str(doc.get("last_event_end") or doc.get("last_end") or "")
    last_dt = parse_dt(last_end) if ev_fails > 0 else None
    if last_dt is None:
        return 0.0
    raw_wait = min(10 * (1 << min(ev_fails - 1, 6)), 60)
    seed = f"{doc.get('name', '')}:ev:{ev_fails}:{last_end}".encode()
    jitter = 0.85 + 0.15 * (int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") / 0xFFFFFFFF)
    wait = raw_wait * jitter
    return max(0.0, wait - max(0.0, (now - last_dt).total_seconds()))


def sched_anchor(doc: Mapping[str, object], sched: ScheduleSpec | None = None) -> str:
    if sched is not None and isinstance(sl := doc.get("schedule_last"), Mapping) and sl:
        return str(sl.get(sched.raw) or "")
    return str(doc.get("last_scheduled") or "")


def schedule_window_start(
    loop: _ScheduledLoop,
    state_doc: Mapping[str, object],
    instant: datetime,
    *,
    trigger: str,
    schedules: Sequence[ScheduleSpec] = (),
) -> datetime:
    """Compute `BF_START` across matched schedules (picking the widest/earliest window) or manual/event fallback."""
    cap = instant - timedelta(seconds=1)
    if trigger == "schedule" and schedules:
        starts: list[datetime] = []
        for sched in schedules:
            bf_s = loop.backfill if sched.backfill is None else sched.backfill
            ov_s = loop.overlap if sched.overlap is None else sched.overlap
            last_dt = parse_dt(sched_anchor(state_doc, sched))
            starts.append(min((last_dt or instant) - timedelta(seconds=ov_s if last_dt else bf_s), cap))
        return min(starts)
    last_dt = parse_dt(str(state_doc.get("last_success") or ""))
    return min((last_dt or instant) - timedelta(seconds=loop.overlap if last_dt else loop.backfill), cap)


def due_schedules(
    loop: _ScheduledLoop, state_doc: Mapping[str, object], now: datetime, *, tz_name: str = "UTC"
) -> tuple[ScheduleSpec, ...]:
    """Return all currently due `ScheduleSpec`s for `loop` (empty when backed off or none due)."""
    if not loop.schedules or backoff_remaining(state_doc, backoff_cap(loop), now) > 0:
        return ()
    tz, local_now, due = None, None, []
    for sched in loop.schedules:
        last_dt = parse_dt(sched_anchor(state_doc, sched))
        if sched.every is not None:
            if last_dt is None or (now - last_dt).total_seconds() >= sched.every:
                due.append(sched)
            continue
        if tz is None or local_now is None:
            tz = resolve_tz(tz_name)
            local_now = now.astimezone(tz)
        with suppress(ValueError, KeyError, TypeError):
            if last_dt is None or last_dt.astimezone(UTC) < prev_cron(sched.cron, local_now).astimezone(UTC):
                due.append(sched)
    return tuple(due)


def next_schedule_delay(
    loop: _ScheduledLoop, state_doc: Mapping[str, object], now: datetime, *, tz_name: str = "UTC"
) -> float | None:
    """Return seconds until `loop`'s earliest next schedule or backoff expiry, or `None` if unscheduled."""
    if not loop.schedules:
        return None
    if (backoff_rem := backoff_remaining(state_doc, backoff_cap(loop), now)) > 0:
        return backoff_rem
    utc_now = now.astimezone(UTC)
    tz: tzinfo | None = None
    local_now: datetime | None = None
    soonest: float | None = None
    for sched in loop.schedules:
        if (last_dt := parse_dt(sched_anchor(state_doc, sched))) is None:
            return 0.5
        if sched.every is not None:
            rem = max(0.5, sched.every - (now - last_dt).total_seconds())
            soonest = rem if soonest is None else min(soonest, rem)
        else:
            if tz is None or local_now is None:
                tz = resolve_tz(tz_name)
                local_now = utc_now.astimezone(tz)
            with suppress(ValueError, KeyError, TypeError):
                if last_dt.astimezone(UTC) < prev_cron(sched.cron, local_now).astimezone(UTC):
                    return 0.5
                rem = max(0.5, (next_cron(sched.cron, local_now).astimezone(UTC) - utc_now).total_seconds())
                soonest = rem if soonest is None else min(soonest, rem)
    return soonest


def is_schedule_overdue(
    loop: _ScheduledLoop, state_doc: Mapping[str, object], now: datetime, *, tz_name: str = "UTC"
) -> bool:
    """Return True when an enabled scheduled loop missed >= max(3*every, every+1h) or >= 2h past a missed cron fire."""
    if not loop.enabled or loop.daemon or not loop.schedules:
        return False
    last_ok = parse_dt(str(state_doc.get("last_success") or ""))
    utc_now = now.astimezone(UTC)
    tz: tzinfo | None = None
    for sched in loop.schedules:
        anchor = parse_dt(sched_anchor(state_doc, sched)) or last_ok
        if anchor is None:
            continue
        anchor_utc = anchor.astimezone(UTC)
        if sched.every is not None:
            grace = max(sched.every * 3, sched.every + 3600)
            if (utc_now - anchor_utc).total_seconds() >= grace:
                return True
        else:
            if tz is None:
                tz = resolve_tz(tz_name)
            with suppress(ValueError, KeyError, TypeError):
                first_missed = next_cron(sched.cron, anchor_utc.astimezone(tz)).astimezone(UTC)
                if first_missed <= utc_now and (utc_now - first_missed).total_seconds() >= 7200:
                    return True
    return False
