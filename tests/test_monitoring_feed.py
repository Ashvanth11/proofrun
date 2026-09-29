import json
from datetime import datetime, timezone

import httpx
import pytest

import app
import export_monitoring as export
import export_traces
import weekly_monitor
from ai_monitor import monitoring_feed as feed
from ai_monitor.agent import investigate as inv
from ai_monitor.agent import investigate_runner
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from tests.test_app import fake_run
from tests.test_export_traces import HOSTILE, Parsed, questions_file, store


def automatic_result(conn, description=HOSTILE):
    item = Item(source=Source.GITHUB, source_id="owner/name", title="Test repo", url="https://github.com/owner/name", content=description)
    item_id = db.upsert_item(conn, item)
    run = fake_run(app.prepare_question("Does owner/name work?"))
    run.report.limitations = ["Only a sample was checked."]
    inv.store_investigation(conn, run, item_id=item_id)


def test_shared_export_preserves_history_and_escapes_descriptions(tmp_path):
    path = tmp_path / "monitor.db"
    conn = db.connect(path)
    automatic_result(conn)
    inv.store_investigation(conn, fake_run(app.prepare_question("Does private/direct work?")))
    conn.close()
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text('<div class="wrap">Historical evaluations</div>')
    (out / "old-trace.html").write_text('<a href="index.html">&larr; all investigations</a>preserved')
    last = {"status": "completed", "finished_at": "2026-09-24T10:00:00+00:00", "investigated": 1}
    data = export.export(path, out, last)
    assert feed.validate_feed(data) == data
    assert len(data["investigations"]) == 1
    assert data["investigations"][0]["repository_description"] == HOSTILE
    parsed = Parsed()
    parsed.feed((out / "monitoring.html").read_text())
    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert ("a", "href", "https://github.com/Ashvanth11/proofrun#screenshot-walkthrough") in parsed.remote
    assert ("a", "href", "https://github.com/Ashvanth11/proofrun#setup") in parsed.remote
    assert (out / "old-trace.html").read_text() == '<a href="history.html">&larr; all investigations</a>preserved'
    assert "Historical evaluations" in (out / "history.html").read_text()
    assert (out / "history.html").read_text().count('id="weekly-monitoring-link"') == 1
    assert 'href="index.html"' in (out / "history.html").read_text()
    assert 'href="history.html"' in (out / "index.html").read_text()
    assert "proofrun-weekly-landing" in (out / "index.html").read_text()
    assert (out / "index.html").read_text() == (out / "monitoring.html").read_text()
    export.export(path, out, last)
    assert (out / "history.html").read_text().count('id="weekly-monitoring-link"') == 1


def test_trace_regeneration_keeps_weekly_home_and_updates_history(tmp_path):
    conn = db.connect(tmp_path / "traces.db")
    store(conn)
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text('<div class="wrap">Old showcase</div>')
    export.export(tmp_path / "traces.db", out)
    export_traces.export(conn, out, questions_file(tmp_path))
    conn.close()
    assert "proofrun-weekly-landing" in (out / "index.html").read_text()
    assert "Old showcase" not in (out / "history.html").read_text()
    assert 'href="index.html"' in (out / "history.html").read_text()
    assert 'href="history.html"' in (out / "owner-name.html").read_text()


def test_feed_network_failure_is_a_readable_fallback(monkeypatch):
    def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "stream", offline)
    assert feed.fetch_public_feed()[0] is None


def test_invalid_public_feed_is_rejected():
    with pytest.raises(ValueError):
        feed.validate_feed({"schema_version": 999, "investigations": []})
    with pytest.raises(ValueError):
        feed.validate_feed({"schema_version": 1, "investigations": [], "last_run": {"status": "running"}})


def test_repeated_week_does_not_make_more_paid_calls(tmp_path):
    conn = db.connect(tmp_path / "monitor.db")
    calls = []
    def fetch(**kwargs):
        calls.append("fetch")
        return [Item(source=Source.GITHUB, source_id="owner/name", title="Test", url="https://github.com/owner/name")]
    def analyze(*args, **kwargs):
        calls.append("analyze")
    def investigate(conn, client, **kwargs):
        assert kwargs["limit"] == 1
        assert kwargs["model"] == inv.INVESTIGATE_MODEL
        calls.append("investigate")
        automatic_result(conn, "A tool for tests.")
    args = dict(now=datetime(2026, 9, 24, tzinfo=timezone.utc), fetch=fetch, analyze=analyze, investigate_candidates=investigate)
    first = weekly_monitor.run_week(conn, object(), **args)
    assert first["status"] == "completed" and first["investigated"] == 1
    assert weekly_monitor.run_week(conn, object(), **args) == first
    assert calls == ["fetch", "analyze", "investigate"]
    conn.close()


def test_manual_rerun_uses_saved_batch_and_replaces_no_claim(tmp_path):
    conn = db.connect(tmp_path / "monitor.db")
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    items = [
        Item(source=Source.GITHUB, source_id=repo, title=repo, url=f"https://github.com/{repo}", content="A description")
        for repo in ("owner/vague", "owner/best")
    ]
    fetched = []

    def fetch(**kwargs):
        fetched.append(True)
        return items

    def analyze(conn, item_id, item, **kwargs):
        conn.execute(
            "INSERT INTO analyses (item_id,relevance_score) VALUES (?,?)",
            (item_id, 0.9 if item.source_id == "owner/vague" else 0.8),
        )
        conn.commit()

    def first_attempt(conn, client, **kwargs):
        row = conn.execute("SELECT * FROM items WHERE source_id='owner/vague'").fetchone()
        investigate_runner._store_no_claim(conn, row, inv.INVESTIGATE_MODEL)

    first = weekly_monitor.run_week(
        conn, object(), now=now, fetch=fetch, analyze=analyze,
        investigate_candidates=first_attempt,
    )
    assert first["investigated"] == 1

    def second_attempt(conn, client, **kwargs):
        assert kwargs["select_best_claim"] is True
        assert kwargs["force"] is True
        assert len(kwargs["eligible_item_ids"]) == 2
        item_id = conn.execute("SELECT id FROM items WHERE source_id='owner/best'").fetchone()[0]
        inv.store_investigation(
            conn, fake_run(app.prepare_question("Does owner/best work?")), item_id=item_id,
        )
        return 1, None

    second = weekly_monitor.rerun_saved_week(
        conn, object(), now=now, investigate_candidates=second_attempt,
    )
    assert second["status"] == "completed" and second["investigated"] == 1
    assert fetched == [True]
    assert [r[0] for r in conn.execute("SELECT repo FROM investigations")] == ["owner/best"]
    assert conn.execute("SELECT COUNT(*) FROM weekly_discoveries WHERE week=?", (second["week"],)).fetchone()[0] == 2
    conn.close()


def test_failed_week_is_saved_and_blocks_automatic_paid_retry(tmp_path):
    conn = db.connect(tmp_path / "monitor.db")
    def failed(**kwargs):
        raise RuntimeError("network failed")
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="network failed"):
        weekly_monitor.run_week(conn, object(), now=now, fetch=failed)
    with pytest.raises(RuntimeError, match="incomplete"):
        weekly_monitor.run_week(conn, object(), now=now, fetch=failed)
    assert conn.execute("select status from weekly_runs").fetchone()[0] == "failed"
    conn.close()


def test_zero_new_results_is_still_a_completed_monitoring_check(tmp_path):
    conn = db.connect(tmp_path / "monitor.db")
    record = weekly_monitor.run_week(conn, object(), fetch=lambda **kw: [], investigate_candidates=lambda *a, **kw: None)
    assert record["status"] == "completed"
    assert record["discovered"] == 0 and record["investigated"] == 0
    conn.close()


def test_weekly_batch_exports_other_nine_without_verdicts_or_scores(tmp_path):
    path = tmp_path / "monitor.db"
    conn = db.connect(path)
    items = [Item(source=Source.GITHUB, source_id=f"owner/repo{i}", title=f"Repo {i}",
                  url=f"https://github.com/owner/repo{i}", content=HOSTILE if i == 1 else "A small developer tool.") for i in range(10)]
    def investigate(conn, client, **kwargs):
        item_id = conn.execute("SELECT id FROM items WHERE source_id='owner/repo0'").fetchone()[0]
        inv.store_investigation(conn, fake_run(app.prepare_question("Does owner/repo0 work?")), item_id=item_id)
    record = weekly_monitor.run_week(conn, object(), now=datetime(2026, 9, 24, tzinfo=timezone.utc),
        fetch=lambda **kw: items, analyze=lambda *a, **kw: None, investigate_candidates=investigate)
    conn.close()
    data = export.export(path, tmp_path / "site", record)
    feed.validate_feed(data)
    assert len(data["discoveries"]) == 9
    assert data["discovery_week"] == "2026-W39"
    assert "owner/repo0" not in {x["repo"] for x in data["discoveries"]}
    assert all(x["status"] == "not_investigated" for x in data["discoveries"])
    assert all("verdict" not in x and "relevance_score" not in x for x in data["discoveries"])
    parsed = Parsed()
    parsed.feed((tmp_path / "site" / "monitoring.html").read_text())
    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert "Also discovered" in " ".join(parsed.text)
    assert "Not investigated" in " ".join(parsed.text)


def test_discoveries_stay_with_completed_week_and_skip_prior_investigations(tmp_path):
    path = tmp_path / "monitor.db"
    conn = db.connect(path)
    automatic_result(conn, "Previously investigated tool")
    items = [Item(source=Source.GITHUB, source_id=repo, title=repo, url=f"https://github.com/{repo}", content="Description")
             for repo in ["owner/name", "owner/new"]]
    weekly_monitor.run_week(conn, object(), now=datetime(2026, 9, 24, tzinfo=timezone.utc),
        fetch=lambda **kw: items, analyze=lambda *a, **kw: None, investigate_candidates=lambda *a, **kw: None)
    conn.execute("INSERT INTO weekly_runs (week,status,started_at) VALUES ('2026-W40','failed','2026-10-01')")
    conn.commit()
    conn.close()
    batch = feed.load_weekly_discoveries(path)
    assert batch["discovery_week"] == "2026-W39"
    assert [x["repo"] for x in batch["discoveries"]] == ["owner/new"]
    assert feed.load_weekly_discoveries(path, "2026-W40")["discoveries"] == []


def test_old_feed_and_database_without_weekly_history_remain_supported(tmp_path):
    path = tmp_path / "old.db"
    db.connect(path).close()
    assert feed.load_weekly_discoveries(path) == {"discovery_week": None, "discoveries": []}
    broken = tmp_path / "broken.db"
    broken.write_text("not a sqlite database")
    assert feed.load_weekly_discoveries(broken) == {"discovery_week": None, "discoveries": []}
    assert feed.validate_feed({"schema_version": 1, "investigations": []})["investigations"] == []
    with pytest.raises(ValueError):
        feed.validate_feed({"schema_version": 1, "investigations": [], "discovery_week": "2026-W39", "discoveries": [
            {"repo": "owner/name", "repository_description": "A tool", "status": "supported"}
        ]})


def test_ui_displays_published_discoveries_offline(monkeypatch):
    from streamlit.testing.v1 import AppTest
    public = {"schema_version": 1, "investigations": [], "last_run": None,
              "discovery_week": "2026-W39", "discoveries": [
                  {"repo": "owner/new", "repository_description": "Turns CSV files into charts.", "status": "not_investigated"}]}
    monkeypatch.setattr(feed, "fetch_public_feed", lambda: (public, ""))
    page = AppTest.from_file(app.__file__).run(timeout=30)
    assert not page.exception
    assert any(x.value == "Not investigated" for x in page.caption)
    assert any(x.value == "Turns CSV files into charts." for x in page.text)
    assert any(x.label == "View repository ↗" for x in page.get("link_button"))
