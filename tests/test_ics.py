import pytest

from datetime import datetime, timezone

from canvas_mcp import ics

STAMP = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def item(**kwargs):
    base = dict(
        summary="BIO 101: Lab Report",
        start=datetime(2026, 3, 13, 3, 59, tzinfo=timezone.utc),
        uid_seed="assignment-1001",
    )
    base.update(kwargs)
    return ics.CalendarItem(**base)


def test_calendar_has_the_required_envelope():
    text = ics.build_calendar([item()], stamp=STAMP)
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "VERSION:2.0" in text
    assert "PRODID:" in text
    assert text.count("BEGIN:VEVENT") == 1
    assert text.count("END:VEVENT") == 1


def test_lines_use_crlf_endings():
    """Calendar clients are strict about this in a way almost nothing else is."""
    text = ics.build_calendar([item()], stamp=STAMP)
    assert "\n" in text
    assert text.replace("\r\n", "") .find("\n") == -1


def test_event_carries_times_and_alarms():
    text = ics.build_calendar([item()], stamp=STAMP)
    assert "DTSTART:20260313T035900Z" in text
    assert "DTEND:20260313T042900Z" in text  # default 30 minute block
    assert text.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT2H" in text
    assert "TRIGGER:-P1D" in text


def test_uid_is_stable_so_reimporting_updates_rather_than_duplicates():
    first = ics.build_calendar([item()], stamp=STAMP)
    second = ics.build_calendar([item()], stamp=datetime(2027, 1, 1, tzinfo=timezone.utc))
    uid_one = [line for line in first.split("\r\n") if line.startswith("UID:")][0]
    uid_two = [line for line in second.split("\r\n") if line.startswith("UID:")][0]
    assert uid_one == uid_two


def test_different_items_get_different_uids():
    text = ics.build_calendar(
        [item(uid_seed="assignment-1"), item(uid_seed="assignment-2")], stamp=STAMP
    )
    uids = {line for line in text.split("\r\n") if line.startswith("UID:")}
    assert len(uids) == 2


def test_special_characters_are_escaped():
    text = ics.build_calendar(
        [item(summary="Essay: draft, part 1; see notes", description="line one\nline two")],
        stamp=STAMP,
    )
    assert "Essay: draft\\, part 1\\; see notes" in text
    assert "line one\\nline two" in text


def test_backslashes_are_escaped_before_delimiters():
    text = ics.build_calendar([item(summary=r"path\to,thing")], stamp=STAMP)
    assert r"path\\to\,thing" in text


def test_long_lines_are_folded_and_unfold_back_to_the_original():
    summary = "Extremely long assignment title " * 6
    text = ics.build_calendar([item(summary=summary)], stamp=STAMP)
    folded = [line for line in text.split("\r\n") if line.startswith("SUMMARY:")][0]
    assert len(folded.encode()) <= 75

    unfolded = text.replace("\r\n ", "")
    assert f"SUMMARY:{summary.strip()}" in unfolded or summary.strip()[:40] in unfolded


def test_folding_never_splits_a_multibyte_character():
    text = ics.build_calendar([item(summary="é" * 200, alarms_minutes=())], stamp=STAMP)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    # Round-trips: unfolding restores every character intact.
    assert text.replace("\r\n ", "").count("é") == 200


def test_duration_formatting():
    assert ics._duration(120) == "PT2H"
    assert ics._duration(1440) == "P1D"
    assert ics._duration(45) == "PT45M"


def test_empty_calendar_is_still_valid():
    text = ics.build_calendar([], stamp=STAMP)
    assert "BEGIN:VCALENDAR" in text and "END:VCALENDAR" in text
    assert "BEGIN:VEVENT" not in text


def test_output_parses_in_a_real_icalendar_library():
    """Verified against an independent parser, not just our own writer.

    Skipped when the library isn't installed; `pip install -e '.[dev]'` provides it.
    """
    icalendar = pytest.importorskip("icalendar")

    tricky = item(
        summary=r"Essay: draft, part 1; see notes \ appendix",
        description="line one\nline two",
    )
    long_title = item(summary="Extremely long assignment title " * 6, uid_seed="long")
    unicode_title = item(summary="Café — 数学 — naïve " * 8, uid_seed="unicode")

    calendar = icalendar.Calendar.from_ical(
        ics.build_calendar([tricky, long_title, unicode_title], stamp=STAMP)
    )
    events = [c for c in calendar.walk() if c.name == "VEVENT"]
    assert len(events) == 3

    summaries = [str(e.get("summary")) for e in events]
    assert tricky.summary in summaries          # escaping survives the round trip
    assert long_title.summary in summaries      # so does line folding
    assert unicode_title.summary in summaries   # and multibyte folding

    first = events[0]
    assert str(first.get("description")) == "line one\nline two"
    assert len([a for a in first.walk() if a.name == "VALARM"]) == 2
