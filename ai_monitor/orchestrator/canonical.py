"""Extracting stable identities from messy URLs and titles.

Cross-source duplication happens in one direction: Hacker News links *out* to
arXiv and GitHub. So the way an HN story is recognized as the same thing as an
arXiv paper is by pulling the arXiv id out of the story's outbound URL. That
extraction is what makes dedup possible; the matching in dedup.py is the easy
part once identities are comparable.
"""

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query parameters that identify a referrer rather than the resource.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "fbclid", "gclid", "mc_cid", "mc_eid",
    "s", "t", "_hsenc", "_hsmi", "igshid",
}

# arXiv ids appear as 2501.00001, with an optional version and an optional
# "arXiv:" prefix, in URLs or free text.
ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv[:\s]+)(\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)
BARE_ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")

GITHUB_REPO_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE
)

# Suffixes GitHub URLs pick up that are not part of the repo identity.
_GITHUB_SUFFIXES = (".git",)


def strip_tracking(url: str) -> str:
    """Remove tracking parameters and normalize a URL for comparison."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()

    if not parsed.netloc:
        return url.strip()

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]

    path = parsed.path.rstrip("/") or "/"

    return urlunparse(
        (
            parsed.scheme.lower() or "https",
            host,
            path,
            "",
            urlencode(sorted(kept)),
            "",  # drop the fragment
        )
    )


def extract_arxiv_id(text: str, allow_bare: bool = False) -> Optional[str]:
    """Pull a version-less arXiv id out of a URL or text.

    Versions are stripped so v1 and v2 of a paper resolve to one identity, the
    same rule the arXiv watcher applies to source_id.
    """
    if not text:
        return None

    match = ARXIV_ID_RE.search(text)
    if match:
        return match.group(1)

    # A bare "2501.00001" is ambiguous with version numbers and dates, so it is
    # only trusted when the caller says the field should contain an id.
    if allow_bare:
        bare = BARE_ARXIV_RE.search(text)
        if bare:
            return bare.group(1)
    return None


def extract_github_repo(text: str) -> Optional[str]:
    """Pull owner/name out of a GitHub URL, lowercased for comparison."""
    if not text:
        return None
    match = GITHUB_REPO_RE.search(text)
    if not match:
        return None

    owner, name = match.group(1), match.group(2)
    for suffix in _GITHUB_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]

    # Paths like github.com/org/repo/pull/12 still yield org/repo, which is the
    # identity we want.
    return f"{owner.lower()}/{name.lower()}"


def canonical_identity(source: str, source_id: str, url: str) -> Optional[str]:
    """The cross-source identity of an item, or None if it has no stable one.

    An arXiv paper, a GitHub repo, and an HN story pointing at either all
    resolve to the same string, which is what lets them collapse together.
    """
    arxiv_id = extract_arxiv_id(url) or (
        extract_arxiv_id(source_id, allow_bare=True) if source == "arxiv" else None
    )
    if arxiv_id:
        return f"arxiv:{arxiv_id}"

    repo = extract_github_repo(url) or (
        source_id.lower() if source == "github" and "/" in source_id else None
    )
    if repo:
        return f"github:{repo}"

    # An HN discussion of a blog post has no shared identity with anything
    # else; title matching is the only remaining signal for those.
    return None


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# Words that carry no distinguishing signal in this domain.
_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with",
    "show", "hn", "via", "using", "towards", "toward",
}


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    if not title:
        return ""
    text = _PUNCT_RE.sub(" ", title.lower())
    return _WS_RE.sub(" ", text).strip()


def title_tokens(title: str) -> frozenset:
    """Significant words in a title, for order-insensitive comparison."""
    return frozenset(
        word for word in normalize_title(title).split() if word not in _STOPWORDS
    )


def title_similarity(a: str, b: str) -> float:
    """Token-set overlap (Jaccard) between two titles, 0.0 to 1.0.

    Token-set rather than string distance because the same work gets titled
    "Show HN: WikiSkill" on HN and "WikiSkill: Compiling Agent Experience" on
    arXiv - different strings, overlapping vocabulary.
    """
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
