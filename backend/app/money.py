"""Money and date helpers. Amounts are integer paise everywhere."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .config import settings

IST = ZoneInfo(settings.timezone)


def to_paise(value) -> int | None:
    """Parse '1,23,456.70', 'Rs. 1,234', '₹500', 1234.5 or Decimal into paise."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value * 100
    if isinstance(value, (float, Decimal)):
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = re.sub(r"(?i)(rs\.?|inr|₹|cr\.?|dr\.?)", "", s)
    s = re.sub(r"[^\d.]", "", s)
    if not s or s == ".":
        return None
    try:
        p = int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return -p if neg else p


def rupees(paise: int | None) -> str:
    """Indian grouping: 1234567.8 -> '₹12,34,567.80'."""
    if paise is None:
        return "—"
    neg = paise < 0
    paise = abs(paise)
    r, p = divmod(paise, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    out = f"₹{s}" + (f".{p:02d}" if p else "")
    return f"-{out}" if neg else out


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def ist_today(now: datetime | None = None) -> date:
    return to_ist(now or now_utc()).date()


def ist_day_start_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


_DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y",
                 "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %B %Y", "%b %d, %Y", "%d/%b/%Y"]


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", s) else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime_ist(value) -> datetime | None:
    """Parse a date or ISO datetime given in IST; return naive UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            d = parse_date(s)
            if d is None:
                return None
            dt = datetime(d.year, d.month, d.day, 12, 0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def days_between(a: datetime | date, b: datetime | date) -> int:
    if isinstance(a, datetime):
        a = to_ist(a).date()
    if isinstance(b, datetime):
        b = to_ist(b).date()
    return (b - a).days


def add(dt: datetime, value: float, unit: str) -> datetime:
    return dt + (timedelta(hours=value) if unit == "hours" else timedelta(days=value))
