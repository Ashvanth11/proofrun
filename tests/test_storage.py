from datetime import datetime, timezone

import pytest

from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def make_item(**overrides) -> Item:
    defaults = dict(
        source=Source.ARXIV,
        source_id="2501.00001",
        title="Attention Is Not All You Need After All",
        url="https://arxiv.org/abs/2501.00001",
        content="We revisit the transformer architecture...",
        authors=["A. Researcher", "B. Scientist"],
        published_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Item(**defaults)


def test_insert_and_fetch(conn):
    item_id = db.upsert_item(conn, make_item())
    row = db.get_item(conn, item_id)
    assert row["source"] == "arxiv"
    assert row["source_id"] == "2501.00001"
    assert row["title"] == "Attention Is Not All You Need After All"
    assert "A. Researcher" in row["authors"]


def test_reupsert_does_not_duplicate(conn):
    first_id = db.upsert_item(conn, make_item())
    second_id = db.upsert_item(conn, make_item(title="Updated title"))
    assert first_id == second_id
    assert db.count_items(conn) == 1
    assert db.get_item(conn, first_id)["title"] == "Updated title"


def test_same_source_id_different_source_is_distinct(conn):
    a = db.upsert_item(conn, make_item())
    b = db.upsert_item(conn, make_item(source=Source.HN, url="https://news.ycombinator.com/item?id=1"))
    assert a != b
    assert db.count_items(conn) == 2


def test_get_items_filters_by_source(conn):
    db.upsert_item(conn, make_item())
    db.upsert_item(
        conn,
        make_item(
            source=Source.GITHUB,
            source_id="org/repo",
            url="https://github.com/org/repo",
        ),
    )
    arxiv_rows = db.get_items(conn, source="arxiv")
    assert len(arxiv_rows) == 1
    assert arxiv_rows[0]["source"] == "arxiv"
    assert len(db.get_items(conn)) == 2


def test_reupsert_preserves_canonical_id(conn):
    original = db.upsert_item(conn, make_item())
    dup = db.upsert_item(
        conn,
        make_item(source=Source.HN, source_id="999", url="https://news.ycombinator.com/item?id=999"),
    )
    conn.execute("UPDATE items SET canonical_id = ? WHERE id = ?", (original, dup))
    conn.commit()

    db.upsert_item(
        conn,
        make_item(source=Source.HN, source_id="999", url="https://news.ycombinator.com/item?id=999"),
    )
    assert db.get_item(conn, dup)["canonical_id"] == original
