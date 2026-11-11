"""Cron schedules in words, and when they next run (5.73).

`describe("30 2 * * 1")` is "Every Monday at 02:30"; `next_run` finds the
next minute a schedule fires, in the server's own time (cron runs in the
host's local time, so the caller passes that host's UTC offset). Standard
five fields with lists, ranges and steps, month and weekday names, and the
@ shortcuts. When both day-of-month and day-of-week are restricted, cron
runs when *either* matches; that's kept here too.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

SHORTCUTS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _value(text: str, field: int) -> int:
    t = text.lower()
    if field == 3 and t[:3] in _MONTHS:
        return _MONTHS.index(t[:3]) + 1
    if field == 4 and t[:3] in _DAYS:
        return _DAYS.index(t[:3])
    return int(t)


def _field(text: str, field: int) -> set[int]:
    lo, hi = _RANGES[field]
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step < 1:
                raise ValueError("step must be at least 1")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, field), _value(b, field)
        else:
            start = _value(part, field)
            end = hi if "/" in text else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ValueError(f"{part!r} is out of range")
        out.update(range(start, end + 1, step))
    if field == 4 and 7 in out:  # 7 is Sunday too
        out.discard(7)
        out.add(0)
    return out


def parse(schedule: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Minutes, hours, days of month, months, weekdays, and whether the day
    fields were each restricted (not "*"). Raises ValueError for @reboot or
    anything unreadable."""
    text = SHORTCUTS.get(schedule.strip().lower(), schedule.strip())
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(f"not a five-field schedule: {schedule!r}")
    sets = [_field(p, i) for i, p in enumerate(parts)]
    return sets[0], sets[1], sets[2], sets[3], sets[4], parts[2] != "*", parts[4] != "*"


def _day_ok(d: date, dom: set[int], months: set[int], dow: set[int], dom_set: bool, dow_set: bool) -> bool:
    if d.month not in months:
        return False
    weekday = (d.weekday() + 1) % 7  # cron: 0 = Sunday
    if dom_set and dow_set:
        return d.day in dom or weekday in dow
    return d.day in dom and weekday in dow


def next_run(schedule: str, now: datetime, utc_offset_minutes: int = 0) -> datetime | None:
    """The next time `schedule` fires after `now`, as an aware datetime in the
    host's offset. None for @reboot, or a schedule that never fires (31 Feb)."""
    if schedule.strip().lower() == "@reboot":
        return None
    try:
        minutes, hours, dom, months, dow, dom_set, dow_set = parse(schedule)
    except ValueError:
        return None
    zone = timezone(timedelta(minutes=utc_offset_minutes))
    local = now.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = local.date()
    for _ in range(366 * 5):
        if _day_ok(day, dom, months, dow, dom_set, dow_set):
            for h in sorted(hours):
                for m in sorted(minutes):
                    at = datetime(day.year, day.month, day.day, h, m, tzinfo=zone)
                    if at >= local:
                        return at
        day += timedelta(days=1)
    return None


def _clock(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def describe(schedule: str) -> str:
    """A schedule in plain words."""
    s = schedule.strip()
    if s.lower() == "@reboot":
        return "When the server starts"
    if s.lower() in ("@hourly",):
        return "Every hour, on the hour"
    try:
        minutes, hours, dom, months, dow, dom_set, dow_set = parse(s)
    except ValueError:
        return s
    fields = SHORTCUTS.get(s.lower(), s).split()
    m_text, h_text = fields[0], fields[1]
    every_day = not dom_set and not dow_set and len(months) == 12
    # Time of day.
    if m_text.startswith("*/") and h_text == "*":
        when = f"Every {m_text[2:]} minutes"
    elif m_text == "*" and h_text == "*":
        when = "Every minute"
    elif h_text == "*" and len(minutes) == 1:
        when = f"Every hour at :{min(minutes):02d}"
    elif h_text.startswith("*/") and len(minutes) == 1:
        when = f"Every {h_text[2:]} hours at :{min(minutes):02d}"
    elif len(hours) * len(minutes) <= 4:
        when = "at " + _list([_clock(h, m) for h in sorted(hours) for m in sorted(minutes)])
    else:
        when = f"at minute {m_text} of hour {h_text}"
    # Which days.
    if not dom_set and not dow_set:
        days = "" if every_day and when.startswith("Every") else "Every day"
    elif dow_set and not dom_set:
        names = [DAY_NAMES[d] for d in sorted(dow)]
        days = "Every weekday" if sorted(dow) == [1, 2, 3, 4, 5] else "Weekends" if sorted(dow) == [0, 6] else "Every " + _list(names)
    elif dom_set and not dow_set:
        nth = _list([_ordinal(d) for d in sorted(dom)])
        days = f"On the {nth} of every month" if len(months) == 12 else f"On the {nth} of " + _list([MONTH_NAMES[m - 1] for m in sorted(months)])
    else:
        days = f"On days {fields[2]} (or weekdays {fields[4]})"
    if not dom_set and len(months) < 12:
        days = f"{days} in " + _list([MONTH_NAMES[m - 1] for m in sorted(months)])
    if not days:
        return when
    return f"{days}, {when.lower()}" if when.startswith("Every") else f"{days} {when}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
