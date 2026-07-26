import pytest

from canvas_mcp import documents
from canvas_mcp.errors import CanvasMCPError


def test_plain_text_is_read_directly(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Week 6\n\nMitochondria are the point of the lab.", encoding="utf-8")
    assert "Mitochondria" in documents.extract_text(path)


def test_html_is_flattened(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<h1>Syllabus</h1><p>Late work loses <b>10%</b> a day.</p>", encoding="utf-8")
    text = documents.extract_text(path)
    assert "Syllabus" in text
    assert "<b>" not in text


def test_csv_becomes_a_readable_table(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("trial,mass\n1,4.2\n2,4.4\n", encoding="utf-8")
    text = documents.extract_text(path)
    assert "trial | mass" in text
    assert "1 | 4.2" in text


def test_truncation_is_announced(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("x" * 5000, encoding="utf-8")
    text = documents.extract_text(path, limit=100)
    assert "truncated" in text
    assert len(text) < 300


def test_unsupported_type_says_what_it_can_read_and_where_the_file_is(tmp_path):
    path = tmp_path / "lecture.mp4"
    path.write_bytes(b"\x00\x01")
    with pytest.raises(CanvasMCPError) as excinfo:
        documents.extract_text(path)
    message = str(excinfo.value)
    assert ".pdf" in message
    assert str(path) in message  # the student can still open it themselves


def test_empty_file_explains_rather_than_returning_nothing(tmp_path):
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(CanvasMCPError) as excinfo:
        documents.extract_text(path)
    assert "no extractable text" in str(excinfo.value)


def test_missing_optional_dependency_gives_an_install_command(tmp_path, monkeypatch):
    path = tmp_path / "slides.pptx"
    path.write_bytes(b"PK\x03\x04")

    real_import = __import__

    def blocked(name, *args, **kwargs):
        if name == "pptx":
            raise ImportError("no module named pptx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    with pytest.raises(CanvasMCPError) as excinfo:
        documents.extract_text(path)
    assert "pip install python-pptx" in str(excinfo.value)


def test_available_readers_reports_the_installation():
    readers = documents.available_readers()
    assert set(readers) == {"pdf", "docx", "pptx", "text/html/csv"}
    assert readers["text/html/csv"] is True


def test_supported_suffixes_cover_what_students_get_handed():
    suffixes = documents.supported_suffixes()
    for expected in (".pdf", ".docx", ".pptx", ".txt", ".csv", ".html", ".md"):
        assert expected in suffixes
