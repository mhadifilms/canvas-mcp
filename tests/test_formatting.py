from datetime import datetime, timedelta, timezone

from canvas_mcp.formatting import (
    format_due,
    html_to_text,
    humanize_delta,
    parse_iso,
    points_label,
    submission_status,
    truncate,
)

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def test_parse_iso_handles_canvas_z_suffix():
    assert parse_iso("2026-03-10T15:00:00Z") == NOW
    assert parse_iso(None) is None
    assert parse_iso("not a date") is None


def test_humanize_delta_reads_naturally():
    assert humanize_delta(NOW + timedelta(days=3), reference=NOW) == "in 3 days"
    assert humanize_delta(NOW + timedelta(hours=4), reference=NOW) == "in 4 hours"
    assert humanize_delta(NOW - timedelta(days=2), reference=NOW) == "2 days ago"
    assert humanize_delta(NOW + timedelta(days=21), reference=NOW) == "in 3 weeks"


def test_format_due_without_a_date():
    assert format_due(None) == "no due date"


def test_format_due_includes_relative_hint():
    text = format_due("2026-03-13T03:59:00Z", timezone.utc, reference=NOW)
    assert "in 2 days" in text
    assert "Mar" in text


def test_html_to_text_keeps_structure_and_links():
    html = (
        "<h2>Lab Report</h2><p>Write up the <strong>cell</strong> lab.</p>"
        "<ul><li>Three pages</li><li>See <a href='https://example.edu/rubric'>rubric</a></li></ul>"
        "<script>alert(1)</script>"
    )
    text = html_to_text(html)
    assert "Lab Report" in text
    assert "- Three pages" in text
    assert "https://example.edu/rubric" in text
    assert "alert" not in text


def test_html_to_text_truncates():
    text = html_to_text("<p>" + "x" * 500 + "</p>", limit=100)
    assert text.startswith("x" * 100)
    assert "truncated" in text


def test_html_to_text_survives_broken_markup():
    assert "hello" in html_to_text("<p>hello<<<")


def test_points_label():
    assert points_label(50) == "50 pts"
    assert points_label(1) == "1 pt"
    assert points_label(None) == ""
    assert points_label(2.5) == "2.5 pts"


def test_submission_status_covers_the_states_students_care_about():
    assert submission_status({"missing": True}) == "MISSING"
    assert submission_status({"excused": True}) == "excused"
    assert submission_status({"workflow_state": "graded", "grade": "A"}).startswith("graded: A")
    assert "late" in submission_status({"workflow_state": "graded", "grade": "B", "late": True})
    assert submission_status({"submitted_at": "2026-03-01T00:00:00Z"}).startswith("submitted")
    assert submission_status(None, due="2020-01-01T00:00:00Z") == "NOT SUBMITTED (past due)"
    assert submission_status(None, due="2099-01-01T00:00:00Z") == "not submitted"


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 10) == "abc"
