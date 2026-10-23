"""Human-friendly date, duration, and capacity parsing for EventCheckin modals."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo


class ParseError(ValueError):
    """Raised with a message that is safe to show directly to the member."""


MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}  # fmt: skip

TIME_HINT = "Include a time, like 19:30 or 7:30pm."
DATE_HINT = (
    "I could not read that date. Try `2026-11-05 19:30`, `5 Nov 7:30pm`, `tomorrow 8pm`, or `fri 19:00`."
)

_MERIDIEM_TIME = re.compile(r"(?<![\d:/-])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b")
_CLOCK_TIME = re.compile(r"(?<![\d:/-])(\d{1,2}):(\d{2})(?![\d:/-])")
_ISO_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_NAMED_DATE = re.compile(
    r"(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>[a-z]+)|(?P<m2>[a-z]+)\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?)"
    r"(?:\s+(?P<year>\d{4}))?"
)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|hours?|hrs?|minutes?|mins?|d|h|m)(?![a-z])")


def _extract_time(text: str) -> tuple[int, int, str]:
    """Return (hour, minute, text-with-the-time-removed)."""
    for word, hour in (("noon", 12), ("midnight", 0)):
        if re.search(rf"\b{word}\b", text):
            return hour, 0, re.sub(rf"\b{word}\b", " ", text, count=1)

    match = _MERIDIEM_TIME.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise ParseError(f"`{match.group(0).strip()}` is not a valid time. {TIME_HINT}")
        if match.group(3) == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute, text[: match.start()] + " " + text[match.end() :]

    match = _CLOCK_TIME.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ParseError(f"`{match.group(0)}` is not a valid time. {TIME_HINT}")
        return hour, minute, text[: match.start()] + " " + text[match.end() :]

    raise ParseError(TIME_HINT)


def parse_start(text: str, tz: tzinfo, now: datetime) -> int:
    """Parse a start time written in the guild's timezone into Unix seconds.

    ``now`` must be timezone-aware and expressed in ``tz``.
    Accepts Unix timestamps and Discord ``<t:...>`` tags as an escape hatch.
    """
    raw = (text or "").strip()
    if not raw:
        raise ParseError("Enter a start date and time.")

    tag = re.fullmatch(r"<t:(\d{9,11})(?::[a-zA-Z])?>", raw)
    if tag:
        return int(tag.group(1))
    if re.fullmatch(r"\d{9,11}", raw):
        return int(raw)

    text = raw.lower().replace(",", " ").replace("@", " ")
    text = re.sub(r"(?<=\d)t(?=\d)", " ", text)  # ISO "2026-11-05T19:30"
    text = re.sub(r"\ba\.m\.?", "am", text)
    text = re.sub(r"\bp\.m\.?", "pm", text)
    text = re.sub(r"\b(at|on)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    hour, minute, remainder = _extract_time(text)
    date_text = re.sub(r"\s+", " ", remainder).strip()

    today = now.date()
    rolled_year_ok = False  # whether a past date may roll into next year
    roll_to_tomorrow = False

    if date_text in {"", "today", "tonight"}:
        day = today
        roll_to_tomorrow = date_text == ""
    elif date_text in {"tomorrow", "tmrw", "tmr"}:
        day = today + timedelta(days=1)
    elif iso := _ISO_DATE.fullmatch(date_text):
        year, month, dom = (int(part) for part in iso.groups())
        day = _make_date(year, month, dom)
    elif re.search(r"\d+\s*/\s*\d+", date_text):
        raise ParseError(
            "Numeric dates like 5/11 can mean different things in different countries. "
            "Use `2026-11-05` or a month name such as `5 Nov`."
        )
    elif named := _NAMED_DATE.fullmatch(date_text):
        month_word = named.group("m1") or named.group("m2")
        month = MONTHS.get(month_word)
        if not month:
            raise ParseError(DATE_HINT)
        dom = int(named.group("d1") or named.group("d2"))
        year = int(named.group("year")) if named.group("year") else today.year
        day = _make_date(year, month, dom)
        rolled_year_ok = named.group("year") is None
    else:
        words = date_text.split()
        prefix = ""
        if words and words[0] in {"next", "this"}:
            prefix, words = words[0], words[1:]
        if len(words) == 1 and words[0] in WEEKDAYS:
            ahead = (WEEKDAYS[words[0]] - today.weekday()) % 7
            if ahead == 0 and (prefix == "next" or _at(day=today, hour=hour, minute=minute, tz=tz) <= now):
                ahead = 7
            day = today + timedelta(days=ahead)
        else:
            raise ParseError(DATE_HINT)

    moment = _at(day=day, hour=hour, minute=minute, tz=tz)
    if roll_to_tomorrow and moment <= now:
        moment = _at(day=day + timedelta(days=1), hour=hour, minute=minute, tz=tz)
    if rolled_year_ok and moment <= now:
        moment = _at(day=_make_date(day.year + 1, day.month, day.day), hour=hour, minute=minute, tz=tz)
    return int(moment.timestamp())


def _make_date(year: int, month: int, day: int):
    try:
        return datetime(year, month, day).date()
    except ValueError as error:
        raise ParseError("That calendar date does not exist.") from error


def _at(*, day, hour: int, minute: int, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def parse_duration(text: str, default: int = 60) -> int:
    """Parse '90', '1h30m', '2 hours', '1.5h' into minutes (1 to 10,080)."""
    raw = (text or "").strip().lower()
    if not raw:
        return default

    if re.fullmatch(r"\d+", raw):
        minutes = int(raw)
    else:
        parts = list(_DURATION_PART.finditer(raw))
        leftover = re.sub(r"\band\b|[\s,]", "", _DURATION_PART.sub("", raw))
        if not parts or leftover:
            raise ParseError("Length should look like `90`, `1h30m`, or `2 hours`.")
        factors = {"d": 1440, "h": 60, "m": 1}
        minutes = round(sum(float(part.group(1)) * factors[part.group(2)[0]] for part in parts))

    if not 1 <= minutes <= 10080:
        raise ParseError("Length must be between 1 minute and 7 days.")
    return minutes


def parse_capacity(text: str) -> int:
    """Parse a capacity; blank or 'unlimited' means 0 (no limit)."""
    raw = (text or "").strip().lower()
    if raw in {"", "0", "unlimited", "none", "no limit", "nolimit", "infinite", "inf"}:
        return 0
    if not re.fullmatch(r"\d+", raw):
        raise ParseError("Max attendees must be a number, or leave it blank for unlimited.")
    value = int(raw)
    if value > 10000:
        raise ParseError("Max attendees can be at most 10,000.")
    return value
