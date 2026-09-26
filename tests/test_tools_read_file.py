"""Bounded GitHub file reads use fake responses; no network or model calls."""

import base64

import pytest

from ai_monitor.agent import tools
from ai_monitor.agent.loop import summarize


def fake_file(monkeypatch, content):
    encoded = base64.b64encode(content.encode()).decode()
    monkeypatch.setattr(
        tools, "_get", lambda path: {"encoding": "base64", "content": encoded, "size": len(content.encode())}
    )


def test_long_file_can_jump_to_relevant_section_and_continue(monkeypatch):
    content = "A" * 7000 + "MCP retain and recall are defined here. " + "B" * 7000
    fake_file(monkeypatch, content)

    opening = tools.read_file("owner/repo", "README.md")
    assert opening["truncated"] is True
    assert "MCP" not in opening["content"]
    assert opening["next_char"] == tools.MAX_FILE_CHARS

    relevant = tools.read_file("owner/repo", "README.md", find="mcp")
    assert relevant["start_char"] == 7000
    assert relevant["content"].startswith("MCP retain and recall")
    assert "MCP retain and recall" in summarize(relevant)
    assert len(relevant["content"]) <= tools.MAX_FILE_CHARS

    ending = tools.read_file("owner/repo", "README.md", start_char=relevant["next_char"])
    assert ending["start_char"] == relevant["next_char"]
    assert ending["truncated"] is False


def test_find_can_skip_earlier_match_and_reports_absence(monkeypatch):
    fake_file(monkeypatch, "MCP in header\n" + "x" * 7000 + "MCP implementation")
    later = tools.read_file("owner/repo", "README.md", start_char=1, find="MCP")
    assert later["content"].startswith("MCP implementation")
    missing = tools.read_file("owner/repo", "README.md", find="not present")
    assert missing["match_found"] is False
    assert missing["content"] == ""


def test_quoted_empty_directory_path_lists_root(monkeypatch):
    paths = []
    def fake_get(path):
        paths.append(path)
        return []
    monkeypatch.setattr(tools, "_get", fake_get)
    assert tools.list_files("owner/repo", '""')["path"] == "/"
    assert paths == ["/repos/owner/repo/contents/"]


@pytest.mark.parametrize("start_char", [-1, True, "5"])
def test_invalid_offset_is_rejected(monkeypatch, start_char):
    fake_file(monkeypatch, "hello")
    with pytest.raises(tools.ToolError, match="start_char"):
        tools.read_file("owner/repo", "README.md", start_char=start_char)
