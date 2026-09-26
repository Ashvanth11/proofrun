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
import re
from typing import Any, Callable, Optional

import httpx

from ai_monitor.agent.sandbox import MAX_REPO_KB, RUNNABLE_LANGUAGES
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
    """Cheapest rung of the ladder: one request, no file contents.

    **Only fields GitHub computed.** Nothing the repository's author wrote is
    in here - that is `get_repo_description`, deliberately a separate tool.
    The split is what makes this tool's membership of `INSPECTING_TOOLS` safe:
    every field below is a fact about the repository rather than a claim by
    it, so an entry citing this call can carry a verdict.

    Field order is load-bearing. This dict is serialised and truncated into
    the trace, and that truncation is the critic's whole view of the call, so
    the fields a verdict can actually turn on - licence, language, size - go
    first and the numeric trivia goes last.
    """
    data = _get(f"/repos/{repo}")
    return {
        "full_name": data.get("full_name"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "language": data.get("language"),
        "size_kb": data.get("size"),
        "archived": data.get("archived"),
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "homepage": data.get("homepage"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
    }


def get_repo_description(repo: str) -> dict:
    """The author's own summary of the project. Their words, not GitHub's.

    Split out of `get_repo_metadata` because of what the bifrost investigation
    showed: `description` is author-written marketing copy, but arriving via
    the metadata tool it was capped at `inspected` and could therefore carry a
    `supported` verdict on its own - a claim proving itself.

    A separate tool fixes that with no new rule. This one is simply absent
    from `INSPECTING_TOOLS`, so the existing cap in `apply_integrity_rules`
    makes anything citing it `reported`, alongside the README it paraphrases.
    """
    data = _get(f"/repos/{repo}")
    return {
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "topics": data.get("topics", []),
    }


def list_files(repo: str, path: str = "") -> dict:
    """Second rung: see what exists before deciding what is worth reading."""
    # Some model calls have supplied the two quote characters for an empty
    # path, producing a GitHub 404 instead of listing the repository root.
    if path.strip() in {'""', "''"}:
        path = ""
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


def read_file(repo: str, path: str, start_char: int = 0, find: Optional[str] = None) -> dict:
    """Read a bounded section, optionally starting at a matching phrase."""
    data = _get(f"/repos/{repo}/contents/{path.strip('/')}")
    if isinstance(data, list):
        raise ToolError(f"{path} is a directory; use list_files")

    if data.get("encoding") != "base64" or not data.get("content"):
        raise ToolError(f"cannot decode {path} (encoding: {data.get('encoding')})")

    try:
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as exc:
        raise ToolError(f"failed to decode {path}: {exc}") from exc

    if isinstance(start_char, bool) or not isinstance(start_char, int) or start_char < 0:
        raise ToolError("start_char must be a non-negative integer")
    if find is not None:
        if not isinstance(find, str) or not find.strip():
            raise ToolError("find must be a non-empty phrase")
        match = re.search(re.escape(find), text[start_char:], flags=re.IGNORECASE)
        if match is None:
            return {
                "path": path,
                "size": data.get("size"),
                "total_chars": len(text),
                "find": find,
                "match_found": False,
                "content": "",
            }
        start_char += match.start()

    end_char = min(start_char + MAX_FILE_CHARS, len(text))
    return {
        "path": path,
        "size": data.get("size"),
        "total_chars": len(text),
        "start_char": start_char,
        "next_char": end_char if end_char < len(text) else None,
        "truncated": end_char < len(text),
        "content": text[start_char:end_char],
    }


# Schemas are written by hand rather than generated, because the descriptions
# are prompt engineering: they are what teaches the model when to escalate.
TOOL_SCHEMAS = [
    {
        "name": "get_repo_metadata",
        "description": (
            "Get the facts GitHub computed about a repository: language, "
            "licence, size, stars, forks, open issues, whether it is "
            "archived, and activity dates. Nothing the author wrote is here - "
            "for the project's own description use get_repo_description. "
            "Costs one request. Start here: language and size decide whether "
            "the question can be tested at all."
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
        "name": "get_repo_description",
        "description": (
            "Get the repository's own one-line description and topic tags. "
            "This is the author's summary of their own project - a claim "
            "about it, not a measurement of it - so treat it exactly as you "
            "would a sentence from the README, and never as evidence that "
            "what it says is true."
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
                    "description": "Directory path; use an empty string for the root (without literal quote characters)",
                },
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read up to 6000 characters of one file. Long files start at the "
            "beginning by default; use find to jump to a relevant phrase "
            "anywhere in the file, or start_char/next_char to continue reading. "
            "A truncated opening excerpt does not establish that later sections "
            "lack the feature in question. Use only when a specific file may "
            "resolve the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "path": {
                    "type": "string",
                    "description": "File path, e.g. README.md or pyproject.toml",
                },
                "start_char": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Character offset to start from; use next_char from a previous read to continue",
                },
                "find": {
                    "type": "string",
                    "description": "Optional case-insensitive phrase to find from start_char; read begins at the match",
                },
            },
            "required": ["repo", "path"],
            "additionalProperties": False,
        },
    },
]

REGISTRY: dict[str, Callable[..., dict]] = {
    "get_repo_metadata": get_repo_metadata,
    "get_repo_description": get_repo_description,
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
IMAGE_CONTENTS = (
    "The image contains python3, pip, git, curl and a C toolchain, and nothing else - there is no cargo, no node, no go and no jdk, and installing one does not persist. "
)

SANDBOX_TOOL_SCHEMAS = [
    {
        "name": "sandbox_clone",
        # The limit is interpolated, not typed out: it was written as a
        # literal once and went stale the moment the gate moved, leaving the
        # model working from a number the code had stopped enforcing.
        "description": (
            "Shallow-clone the repository into a fresh isolated container "
            "volume at /work/repo. Call this once, and only once you have "
            "decided the question actually needs the code run. Two refusals "
            "happen before anything is downloaded, both read from "
            "get_repo_metadata: repositories "
            f"over {MAX_REPO_KB // 1000} MB are refused, and so is any "
            f"repository whose language is not "
            f"{' or '.join(sorted(RUNNABLE_LANGUAGES))} - check the language "
            "before you call this. Only /work persists between commands; the "
            "rest of the container's filesystem is read-only and is discarded "
            "after every call."
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
            "Requires a successful sandbox_clone first; do not call after a clone refusal. "
            + IMAGE_CONTENTS +
            "Network is a setup-phase privilege and these calls are strictly "
            "limited - do the install in as few commands as you can. Anything "
            "written outside /work is lost before your next call, so install "
            "into the project or a virtualenv under /work, never into a home "
            "directory."
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
            "Requires a successful sandbox_clone first; do not call after a clone refusal. "
            + IMAGE_CONTENTS +
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

# Which tools produce *inspected* evidence: structured facts about the
# repository that GitHub computed rather than the author wrote - the licence
# field, the language, whether it is archived, which files exist. A README is
# not on this list, and neither is read_file: a file's contents are the
# author's words, and the author is one of the parties with a stake in the
# answer.
#
# `get_repo_description` is deliberately absent, which is the whole of the fix
# for the hole the bifrost run exposed. `description` and `topics` used to
# arrive through get_repo_metadata and were therefore capped at `inspected`,
# so a verdict could rest on the project's own marketing copy restating the
# claim under investigation. Splitting the tool closes that with no new rule:
# the existing cap sees a tool that is not on this list and writes `reported`.
#
# A split rather than a heuristic, because the alternative is inspecting which
# *field* the model named in its source string - and the model is the thing
# being constrained, so a rule that depends on its spelling is not a rule.
INSPECTING_TOOLS = frozenset({"get_repo_metadata", "list_files"})


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
        self.clone_mb = 0.0
        self.volume_mb = 0.0
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
            meta = self._metadata(self.repo)
        except ToolError as exc:
            # Unknown size means the gate cannot fire, and an ungated clone is
            # exactly the unbounded download the cap exists to prevent. The
            # same request carries the language, so both gates fail together.
            return {
                "error": f"cannot check the repository's size or language: {exc}"
            }, True

        self._box = self._factory(
            self.repo,
            size_kb=meta.get("size_kb"),
            language=meta.get("language"),
        )
        result = self._finish(self._box.clone())
        # What --depth 1 really cost, as opposed to the history-inclusive
        # size_kb the gate had to guess from. MAX_REPO_KB gets re-tuned from
        # these, not from argument.
        self.clone_mb = self.volume_mb
        return result

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
        self.volume_mb = used
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
