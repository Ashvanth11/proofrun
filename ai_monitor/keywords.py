"""Shared keyword matching for the HN filter and the routing pre-filter.

Both need the same thing: does this text mention any of these terms? Both got it
subtly wrong in different ways, so the logic lives here once.

Two rules, and they pull against each other:

- Word boundaries are required. Without them "agent" fires on "urgent" and "ai"
  fires on "said", which drags in unrelated items.
- A trailing "s" must still match. Without it "agent" misses "agents", which
  silently drops relevant items - the worse failure, because nothing signals it.

Only a plural "s" is allowed, not open-ended prefix matching: `agent\\w*` would
also match "agentry", and for short terms `ai\\w*` would match "aid" and "air".
"""

import re

_MULTIWORD = re.compile(r"\s")


def build_pattern(terms) -> re.Pattern:
    """Compile terms into one alternation with plural-tolerant boundaries.

    Multi-word phrases are matched literally - "tool use" has no plural form
    worth handling, and its internal space already acts as a boundary.
    """
    parts = []
    for term in sorted({t.strip().lower() for t in terms if t and t.strip()}):
        if _MULTIWORD.search(term):
            parts.append(re.escape(term))
        else:
            parts.append(rf"\b{re.escape(term)}s?\b")

    if not parts:
        return re.compile(r"$^")  # matches nothing
    return re.compile("|".join(parts), re.IGNORECASE)


def find_matches(text: str, pattern: re.Pattern) -> list[str]:
    """Distinct matched terms, lowercased, in sorted order."""
    if not text:
        return []
    return sorted({m.group(0).lower() for m in pattern.finditer(text)})


def matches(text: str, pattern: re.Pattern) -> bool:
    return bool(text) and bool(pattern.search(text))
