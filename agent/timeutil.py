from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import WAT


def month_add(month: str, delta: int) -> str:
    y, m = (int(x) for x in month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_of(d: datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def deadline_for(month: str, deadline_day: int) -> datetime:
    """Deadline = day N of the month after the reporting month, 23:59:59 WAT (System 3, D4)."""
    nxt = month_add(month, 1)
    y, m = (int(x) for x in nxt.split("-"))
    return datetime(y, m, deadline_day, 23, 59, 59, tzinfo=WAT)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(WAT)
    d = date.fromisoformat(value)
    return datetime(d.year, d.month, d.day, 8, 0, tzinfo=WAT)  # daily run time


def fmt_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return datetime(y, m, 1).strftime("%b %Y")


def days_until(target: datetime, now: datetime) -> float:
    return (target - now) / timedelta(days=1)
