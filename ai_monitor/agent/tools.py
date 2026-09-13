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


# --- server-side tools ---------------------------------------------------

# Anthropic executes this one; there is no implementation and no registry
# entry. `max_uses` is the cap, enforced server-side, which is why web search
# does not appear in the loop's ToolCap list.
#
# The type is model-dated, not model-agnostic: `web_search_20260209` is the
# variant Sonnet 5 takes. Verified at build time against the pinned SDK - see
# ROADMAP-stage2.md, "Web search tool type".
WEB_SEARCH_TYPE = "web_search_20260209"
DEFAULT_MAX_WEB_SEARCHES = 5


def web_search_tool(max_uses: int = DEFAULT_MAX_WEB_SEARCHES) -> dict:
    return {"type": WEB_SEARCH_TYPE, "name": "web_search", "max_uses": max_uses}


# --- sandbox tools -------------------------------------------------------

# Setup and run are two tools rather than one tool with a `network` flag
# because these descriptions are the prompt engineering: they are what teaches
# the model that network is a setup-phase privilege and not a default.
SANDBOX_TOOL_SCHEMAS = [
    {
        "name": "sandbox_clone",
        "description": (
            "Shallow-clone the repository into a fresh isolated container "
            "volume at /work/repo. Call this once, and only once you have "
            "decided the question actually needs the code run. Repositories "
            "over 200 MB are refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"}
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sandbox_setup",
        "description": (
            "Run one shell command in the container WITH network access, for "
            "installation only: 'cd repo && pip install -e .' and the like. "
            "Network is a setup-phase privilege and these calls are strictly "
            "limited - do the install in as few commands as you can."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command, run by bash inside the container",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sandbox_run",
        "description": (
            "Run one shell command in the container with NO network at all. "
            "This is where the capability the question names gets exercised: "
            "one command, one input, once. Anything it prints is first-hand "
            "evidence; anything you only read about is not."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command, run by bash inside the container",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]

SANDBOX_TOOL_NAMES = frozenset(s["name"] for s in SANDBOX_TOOL_SCHEMAS)
# Which tools produce *observed* evidence: a command actually ran. Cloning is
# not one of them - it demonstrates nothing about what the code does.
OBSERVING_TOOLS = frozenset({"sandbox_setup", "sandbox_run"})


class SandboxTools:
    """Sandbox tools bound to one investigation's container.

    The module-level REGISTRY cannot express these: the executors close over a
    live volume that exists for the length of one run and must be destroyed
    afterwards whatever happens. So the investigation agent builds one of these
    per run and hands `execute` to the loop.

    The sandbox is created lazily, on the first clone, because most questions
    are answered by reading and never pay for a container at all.
    """

    def __init__(
        self,
        repo: str,
        factory: Optional[Callable[..., Any]] = None,
        metadata: Optional[Callable[[str], dict]] = None,
        disk_cap_mb: Optional[float] = None,
    ) -> None:
        from ai_monitor.agent import sandbox as sandbox_mod

        self.repo = sandbox_mod.validate_repo(repo)
        self._factory = factory or sandbox_mod.Sandbox.create
        self._metadata = metadata or get_repo_metadata
        self._disk_cap_mb = (
            disk_cap_mb if disk_cap_mb is not None else sandbox_mod.DISK_CAP_MB
        )
        self._box: Any = None

        # Objective bookkeeping for the report's `facts` block. These are
        # measured here rather than asked of the model, because the whole
        # point of that block is that it comes from what happened.
        self.commands_run = 0
        self.setup_commands = 0
        self.setup_seconds = 0.0
        self.install_succeeded = False
        self._exercised = False

    # --- lifecycle -------------------------------------------------------

    @property
    def cloned(self) -> bool:
        return self._box is not None

    def close(self) -> None:
        """Destroy the volume. Idempotent; safe to call when nothing was built."""
        if self._box is not None:
            self._box.destroy()
            self._box = None

    def __enter__(self) -> "SandboxTools":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # --- the executor the loop calls -------------------------------------

    def execute(self, name: str, arguments: dict) -> tuple[dict, bool]:
        """Run a sandbox tool, falling through to the read-only tools.

        Same contract as `execute` above: failures come back as results the
        model can react to, not exceptions. The one exception that does escape
        is `StopLoop`, which is how the disk cap stops the run.
        """
        from ai_monitor.agent.sandbox import SandboxError

        if name not in SANDBOX_TOOL_NAMES:
            return execute(name, arguments)

        try:
            if name == "sandbox_clone":
                return self._clone(**arguments)
            if name == "sandbox_setup":
                return self._command("setup", **arguments)
            return self._command("run", **arguments)
        except SandboxError as exc:
            log.info("sandbox tool %s refused: %s", name, exc)
            return {"error": str(exc)}, True
        except TypeError as exc:  # model supplied wrong/missing arguments
            return {"error": f"bad arguments for {name}: {exc}"}, True

    # --- the tools themselves --------------------------------------------

    def _clone(self, repo: str) -> tuple[dict, bool]:
        # The repo name reaches us from the model, and the model has been
        # reading attacker-controlled text all run. It gets to clone the
        # repository under investigation or nothing.
        if repo != self.repo:
            return (
                {
                    "error": (
                        f"this investigation is about {self.repo}; "
                        f"refusing to clone {repo!r}"
                    )
                },
                True,
            )
        if self._box is not None:
            return {"error": f"{self.repo} has already been cloned"}, True

        try:
            size_kb = self._metadata(self.repo).get("size_kb")
        except ToolError as exc:
            # Unknown size means the gate cannot fire, and an ungated clone is
            # exactly the unbounded download the cap exists to prevent.
            return {"error": f"cannot check repository size: {exc}"}, True

        self._box = self._factory(self.repo, size_kb=size_kb)
        return self._finish(self._box.clone())

    def _command(self, kind: str, command: str) -> tuple[dict, bool]:
        if self._box is None:
            return {"error": "nothing is cloned yet; call sandbox_clone first"}, True

        self.commands_run += 1
        if kind == "setup":
            self.setup_commands += 1
            result = self._box.setup(command)
            self.install_succeeded = result.ok
        else:
            result = self._box.run(command)
            # Setup is over at the first command that runs without network and
            # succeeds: that is the one exercising the capability, not
            # preparing for it.
            if result.ok:
                self._exercised = True
        return self._finish(result)

    def _finish(self, result: Any) -> tuple[dict, bool]:
        """Shape one CommandResult for the model, then check the disk cap."""
        payload = result.model_dump()
        if not self._exercised:
            self.setup_seconds += result.elapsed_s
        used = self._box.disk_usage_mb()
        if used > self._disk_cap_mb:
            from ai_monitor.agent.loop import StopLoop

            log.info("sandbox: %s over disk cap at %.0f MB", self.repo, used)
            raise StopLoop(
                "disk_cap",
                {
                    **payload,
                    "error": (
                        f"the sandbox volume reached {used:.0f} MB, over the "
                        f"{self._disk_cap_mb:.0f} MB cap; the run stops here"
                    ),
                },
            )
        return payload, not result.ok
