import xml.etree.ElementTree as ET
from datetime import timezone

from ai_monitor.storage.models import Source
from ai_monitor.watchers import arxiv

SAMPLE_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2608.27454v2</id>
    <title>WikiSkill: Compiling Agent Experience
      into Persistent Knowledge</title>
    <updated>2026-08-27T17:59:11Z</updated>
    <link href="https://arxiv.org/abs/2608.27454v2" rel="alternate"/>
    <summary>Agent skills package specialized
      knowledge into reusable resources.</summary>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <published>2026-08-27T17:59:11Z</published>
    <arxiv:primary_category term="cs.AI"/>
    <author><name>Liyan Tang</name></author>
    <author><name>Cyrus Rashtchian</name></author>
  </entry>
</feed>
"""


def first_entry():
    feed = ET.fromstring(SAMPLE_FEED)
    return feed.find(f"{arxiv.ATOM}entry")


def test_parse_entry_maps_core_fields():
    item = arxiv.parse_entry(first_entry())
    assert item.source == Source.ARXIV
    assert item.title == "WikiSkill: Compiling Agent Experience into Persistent Knowledge"
    assert item.content == "Agent skills package specialized knowledge into reusable resources."
    assert item.authors == ["Liyan Tang", "Cyrus Rashtchian"]


def test_version_is_stripped_from_source_id():
    """A revised paper must update its existing row, not create a second one."""
    item = arxiv.parse_entry(first_entry())
    assert item.source_id == "2608.27454"
    assert item.url == "https://arxiv.org/abs/2608.27454"
    assert item.raw["arxiv_id"] == "2608.27454v2"


def test_published_at_is_timezone_aware():
    item = arxiv.parse_entry(first_entry())
    assert item.published_at.tzinfo == timezone.utc
    assert item.published_at.year == 2026


def test_categories_captured_in_raw():
    item = arxiv.parse_entry(first_entry())
    assert item.raw["categories"] == ["cs.AI", "cs.CL"]
    assert item.raw["primary_category"] == "cs.AI"
