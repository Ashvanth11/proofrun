"""Tools the repo-analysis agent can call against the GitHub contents API.

Each tool is defined once and exposes two things: a JSON schema the model sees,
and a callable the loop executes. Keeping them together stops the schema and
the implementation from drifting apart.

Rate limits are the binding constraint here. The GitHub *core* API allows 60
requests/hour unauthenticated (the search API's 10/min is a separate budget),
so an agent that reads several files per repo will exhaust it quickly. Set
GITHUB_TOKEN to raise this to 5,000/hour.
"""

import base64
import logging
from typing import Any, Callable, Optional

import httpx

from ai_monitor.watchers.github import _headers

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

# Truncation guard: a single vendored file can be megabytes, which would blow
# the context window and the cost cap in one call.
MAX_FILE_CHARS = 6000
MAX_LISTING_ENTRIES = 60


class ToolError(RuntimeError):
    """A tool failed in a way the model should see and can react to."""


def _get(path: str, timeout: float = 20.0) -> Any:
    try:
        response = httpx.get(f"{API_ROOT}{path}", headers=_headers(), timeout=timeout)
    except httpx.HTTPError as exc:
        raise ToolError(f"request failed: {exc}") from exc

    if response.status_code == 404:
        raise ToolError(f"not found: {path}")
    if response.status_code == 403:
        if response.headers.get("x-ratelimit-remaining") == "0":
            raise ToolError(
                "GitHub rate limit exhausted (60/hr unauthenticated). "
                "Set GITHUB_TOKEN to raise it to 5000/hr."
            )
        raise ToolError("forbidden")
    response.raise_for_status()
    return response.json()


def get_repo_metadata(repo: str) -> dict:
    """Cheapest rung of the ladder: one request, no file contents."""
    data = _get(f"/repos/{repo}")
    return {
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "topics": data.get("topics", []),
        "language": data.get("language"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "size_kb": data.get("size"),
        "archived": data.get("archived"),
        "homepage": data.get("homepage"),
    }


def list_files(repo: str, path: str = "") -> dict:
    """Second rung: see what exists before deciding what is worth reading."""
    data = _get(f"/repos/{repo}/contents/{path.strip('/')}")
    if isinstance(data, dict):  # a file path was passed, not a directory
        return {
            "path": path,
            "note": "this is a file, not a directory; use read_file",
            "size": data.get("size"),
        }

    entries = [
        {"name": e["name"], "type": e["type"], "size": e.get("size", 0)}
        for e in data[:MAX_LISTING_ENTRIES]
    ]
    return {
        "path": path or "/",
        "entries": entries,
        "truncated": len(data) > MAX_LISTING_ENTRIES,
    }


def read_file(repo: str, path: str) -> dict:
    """Third rung: the expensive one. Contents are truncated."""
    data = _get(f"/repos/{repo}/contents/{path.strip('/')}")
    if isinstance(data, list):
        raise ToolError(f"{path} is a directory; use list_files")

    if data.get("encoding") != "base64" or not data.get("content"):
        raise ToolError(f"cannot decode {path} (encoding: {data.get('encoding')})")

    try:
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as exc:
        raise ToolError(f"failed to decode {path}: {exc}") from exc

    truncated = len(text) > MAX_FILE_CHARS
    return {
        "path": path,
        "size": data.get("size"),
        "truncated": truncated,
        "content": text[:MAX_FILE_CHARS],
    }


# Schemas are written by hand rather than generated, because the descriptions
# are prompt engineering: they are what teaches the model when to escalate.
TOOL_SCHEMAS = [
    {
        "name": "get_repo_metadata",
        "description": (
            "Get high-level repository facts: description, topics, language, "
            "stars, license, activity dates. Costs one request. Start here - "
            "for many repositories this alone is enough to judge relevance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/name form, e.g. langfuse/langfuse",
                }
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories at a path in the repository. Use this "
            "when the metadata is ambiguous and you need to see what the "
            "project actually contains before reading anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "path": {
                    "type": "string",
                    "description": "Directory path; empty string for the root",
                },
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents (truncated to a few thousand characters). "
            "This is the most expensive tool - use it only when a specific file "
            "will resolve a specific question, such as reading README.md to "
            "understand what a project does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "path": {
                    "type": "string",
                    "description": "File path, e.g. README.md or pyproject.toml",
                },
            },
            "required": ["repo", "path"],
            "additionalProperties": False,
        },
    },
]

REGISTRY: dict[str, Callable[..., dict]] = {
    "get_repo_metadata": get_repo_metadata,
    "list_files": list_files,
    "read_file": read_file,
}


def execute(name: str, arguments: dict) -> tuple[dict, bool]:
    """Run a tool by name. Returns (result, is_error).

    Tool failures are returned to the model rather than raised: a missing file
    is information the agent should react to, not a reason to abort the run.
    """
    func = REGISTRY.get(name)
    if func is None:
        return {"error": f"unknown tool: {name}"}, True

    try:
        return func(**arguments), False
    except ToolError as exc:
        log.info("tool %s failed: %s", name, exc)
        return {"error": str(exc)}, True
    except TypeError as exc:  # model supplied wrong/missing arguments
        return {"error": f"bad arguments for {name}: {exc}"}, True
