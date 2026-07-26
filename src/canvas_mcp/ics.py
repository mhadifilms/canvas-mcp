"""iCalendar export.

The point of this file is reach. A student who won't open a chat window will still
look at the calendar on their phone, so getting every Canvas deadline into Google
or Apple Calendar - with alarms - does more good than any number of tools.

Written by hand against RFC 5545 rather than pulling in a dependency: the subset
needed for "a timed event with two alarms" is small, and the folding and escaping
rules are the only fiddly part.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PRODID = "-//canvas-mcp//Canvas deadlines//EN"


@dataclass
class CalendarItem:
    summary: str
    start: datetime
    uid_seed: str
    description: str = ""
    url: str = ""
    location: str = ""
    duration_minutes: int = 30
    alarms_minutes: tuple[int, ...] = (120, 1440)  # two hours and a day before
    categories: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        digest = hashlib.sha1(self.uid_seed.encode("utf-8")).hexdigest()[:24]
        return f"{digest}@canvas-mcp"


def build_calendar(
    items: list[CalendarItem],
    *,
    calendar_name: str = "Canvas deadlines",
    stamp: datetime | None = None,
) -> str:
    stamp = (stamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(calendar_name)}",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for item in items:
        lines.extend(_event(item, stamp))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def _event(item: CalendarItem, stamp: datetime) -> list[str]:
    start = item.start.astimezone(timezone.utc)
    end = start + timedelta(minutes=max(item.duration_minutes, 0))
    lines = [
        "BEGIN:VEVENT",
        f"UID:{item.uid}",
        f"DTSTAMP:{fmt(stamp)}",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SUMMARY:{escape(item.summary)}",
    ]
    if item.description:
        lines.append(f"DESCRIPTION:{escape(item.description)}")
    if item.url:
        lines.append(f"URL:{escape(item.url)}")
    if item.location:
        lines.append(f"LOCATION:{escape(item.location)}")
    if item.categories:
        lines.append("CATEGORIES:" + ",".join(escape(c) for c in item.categories))
    lines.append("TRANSP:TRANSPARENT")
    for minutes in item.alarms_minutes:
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{escape(item.summary)}",
                f"TRIGGER:-{_duration(minutes)}",
                "END:VALARM",
            ]
        )
    lines.append("END:VEVENT")
    return lines


def _duration(minutes: int) -> str:
    minutes = max(int(minutes), 0)
    if minutes % 1440 == 0 and minutes:
        return f"P{minutes // 1440}D"
    if minutes % 60 == 0 and minutes:
        return f"PT{minutes // 60}H"
    return f"PT{minutes}M"


def fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def escape(text: str) -> str:
    """RFC 5545 text escaping: backslash first, then the delimiters, then newlines."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str, limit: int = 73) -> str:
    """Content lines wrap at 75 octets. Fold on encoded bytes so non-ASCII titles
    (an accented instructor name, a course in another language) don't get split
    mid-character."""
    raw = line.encode("utf-8")
    if len(raw) <= limit:
        return line

    chunks: list[str] = []
    current = bytearray()
    for char in line:
        encoded = char.encode("utf-8")
        allowance = limit if not chunks else limit - 1  # continuations carry a leading space
        if len(current) + len(encoded) > allowance:
            chunks.append(current.decode("utf-8"))
            current = bytearray()
        current += encoded
    if current:
        chunks.append(current.decode("utf-8"))
    return ("\r\n ").join(chunks)
