import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ai_monitor.config.settings import settings
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/search/repositories"

# Topics that map onto the configured interest areas. GitHub's topic index is
# more reliable than full-text search for finding relevant repos.
#
# Kept short deliberately: GitHub ANDs multiple topic: qualifiers in one query
# (so asking for five topics matches almost nothing), which means one request
# per topic - against an unauthenticated search limit of 10 requests/minute.
DEFAULT_TOPICS = ["ai-agents", "llm", "llmops"]

DEFAULT_MIN_STARS = 100


class GitHubError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _parse_datetime(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_repo(repo: dict) -> Item:
    """Map a GitHub search result onto the normalized Item."""
    description = repo.get("description") or ""
    topics = repo.get("topics") or []

    # Search results carry no README, so the analyzable text is the description
    # plus topics. Fetching READMEs is deliberately left to the Phase 5 agent,
    # which decides per-repo whether the deeper read is worth it.
    content = description
    if topics:
        content = f"{description}\n\nTopics: {', '.join(topics)}".strip()

    return Item(
        source=Source.GITHUB,
        source_id=repo["full_name"],
        title=repo["full_name"],
        url=repo["html_url"],
        content=content,
        authors=[repo.get("owner", {}).get("login", "")] if repo.get("owner") else [],
        published_at=_parse_datetime(repo.get("created_at")),
        raw={
            "description": description,
            "topics": topics,
            "language": repo.get("language"),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "pushed_at": repo.get("pushed_at"),
            "open_issues": repo.get("open_issues_count"),
        },
    )


def build_query(topic: str, min_stars: int, pushed_since: datetime) -> str:
    """Query for one topic.

    One topic per query is not a stylistic choice: GitHub ANDs repeated
    topic: qualifiers, so a multi-topic query returns only repos carrying
    every topic - in practice, almost nothing.
    """
    return (
        f"topic:{topic} stars:>={min_stars} "
        f"pushed:>{pushed_since.date().isoformat()}"
    )


def _search(query: str, per_page: int, timeout: float) -> list[dict]:
    try:
        response = httpx.get(
            API_URL,
            params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": per_page,
            },
            headers=_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub request failed: {exc}") from exc

    if response.status_code == 403 and response.headers.get(
        "x-ratelimit-remaining"
    ) == "0":
        raise GitHubError(
            "GitHub search rate limit exhausted. Set GITHUB_TOKEN in .env to "
            "raise the limit from 10 to 30 requests/minute."
        )
    response.raise_for_status()
    return response.json().get("items", [])


def fetch(
    topics: Optional[list[str]] = None,
    min_stars: int = DEFAULT_MIN_STARS,
    days: int = 7,
    max_results: int = 25,
    timeout: float = 30.0,
) -> list[Item]:
    """Fetch recently-pushed, well-starred repos across the given topics.

    Note this is "recently active and popular", not star *velocity* - real
    velocity needs two snapshots over time. Star counts are stored in raw_json
    on every run so velocity becomes computable once history accumulates.

    A failure on one topic does not lose the results from the others; only an
    exhausted rate limit aborts, since every remaining query would also fail.
    """
    topics = topics or DEFAULT_TOPICS
    since = datetime.now(timezone.utc) - timedelta(days=days)
    per_topic = max(1, min(max_results, 100))

    seen: dict[str, Item] = {}
    for topic in topics:
        try:
            repos = _search(build_query(topic, min_stars, since), per_topic, timeout)
        except GitHubError as exc:
            if "rate limit" in str(exc):
                raise
            log.warning("github: topic %r failed: %s", topic, exc)
            continue
        except httpx.HTTPStatusError as exc:
            log.warning("github: topic %r returned %s", topic, exc.response.status_code)
            continue

        for repo in repos:
            name = repo.get("full_name")
            if not name or name in seen:
                continue  # a repo can carry several of our topics
            try:
                seen[name] = parse_repo(repo)
            except Exception:
                log.exception("failed to parse repo %s; skipping", name)

    items = sorted(seen.values(), key=lambda i: i.raw.get("stars") or 0, reverse=True)
    return items[:max_results]


def fetch_and_store(
    conn: sqlite3.Connection,
    topics: Optional[list[str]] = None,
    min_stars: int = DEFAULT_MIN_STARS,
    days: int = 7,
    max_results: int = 25,
) -> list[int]:
    items = fetch(
        topics=topics, min_stars=min_stars, days=days, max_results=max_results
    )
    ids = [db.upsert_item(conn, item) for item in items]
    log.info("github: fetched %d items", len(ids))
    return ids
