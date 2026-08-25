from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Source(str, Enum):
    ARXIV = "arxiv"
    GITHUB = "github"
    HN = "hn"


class Item(BaseModel):
    """Normalized unit of content; every watcher maps its source into this."""

    source: Source
    source_id: str  # native id: arxiv id, repo full_name, hn story id, ...
    title: str
    url: str
    content: str = ""  # abstract / readme / description
    authors: list[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    raw: dict[str, Any] = Field(default_factory=dict)  # full original payload
