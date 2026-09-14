"""Docker sandbox the verification agent executes repositories in.

The contract is enforced here, in code, rather than requested in a prompt.
README contents and command output are attacker-controlled: a hostile repo's
setup script and tests run with the model's blessing, so the boundary has to
hold without the model's cooperation.

Two decisions shape the module:

**Every call is a fresh `docker run --rm` against a per-repo named volume
mounted at /work.** State that matters (the clone, an installed venv) lives in
the volume; the container does not survive the call. That is what makes network
a per-call property rather than a per-run one - `setup()` gets the default
bridge, `run()` gets `--network none` - which in turn lets the tool descriptions
teach the model that network is a setup-phase privilege.

**No environment ever crosses the boundary.** The argv carries no `-e`/`--env`
and `os.environ` is never passed through, so ANTHROPIC_API_KEY and GITHUB_TOKEN
are not reachable from inside a container even by a script explicitly looking
for them. `tests/test_sandbox.py` asserts this over the constructed argv.

All subprocess calls funnel through a single injectable ``runner``, so the test
suite exercises the argv and the timeout/kill path without Docker installed.
"""

import logging
import re
import shlex
import subprocess
import time
import uuid
from typing import Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

IMAGE = "ai-monitor-sandbox:latest"
DOCKERFILE = "sandbox/Dockerfile"

# Every container and volume carries this so a crashed run's leftovers can be
# pruned by label before the next batch starts.
LABEL_KEY = "ai-monitor-sandbox"
LABEL = f"{LABEL_KEY}=1"

WORKDIR = "/work"
CLONE_PATH = f"{WORKDIR}/repo"

SETUP_TIMEOUT_S = 300.0
RUN_TIMEOUT_S = 120.0
# Bookkeeping commands (volume create/rm, du) are not the model's; they should
# be instant or something is wrong with the daemon.
ADMIN_TIMEOUT_S = 60.0

# Same principle as MAX_FILE_CHARS in tools.py: one command can emit megabytes
# of progress bars, which would blow the context window and the cost cap in a
# single call.
MAX_OUTPUT_CHARS = 4000

# Gates. The disk cap is checked by the caller after every call (the loop owns
# stopping); this module only measures.
#
# MAX_REPO_KB is a coarse pre-flight refusal, not the real enforcement. It is
# compared against get_repo_metadata's `size_kb`, which is GitHub's
# history-inclusive server-side size - but the clone is `--depth 1`, one
# commit's tree. The gap is routinely 5-30x on a mature monorepo, and largest
# for exactly the well-tested projects most worth running, so a tight gate
# refuses the wrong repositories for bandwidth it was never going to spend.
#
# The real enforcement is DISK_CAP_MB, measured on the volume after every call
# including the clone. So this gate only has to avoid wasting a 300 s setup
# window on an obvious loser, and is set below the disk cap so that a clone
# alone cannot exhaust the volume even if history and tree turn out equal.
#
# Raised from 200 MB on 2026-09-13: at 200 MB only 2 of 22 repositories in the
# database were clonable Python, which was shaping the eval set rather than
# protecting anything. `facts.clone_mb` now records what a shallow clone
# actually cost, so the next revision of this number is measured, not argued.
MAX_REPO_KB = 1_000_000  # 1 GB of history-inclusive size_kb
DISK_CAP_MB = 2048

# The language gate, and why it is code rather than a sentence in the prompt.
#
# The image is python:3.12-slim plus git, curl and a C toolchain. "Python only"
# was originally a prompt rule, and the prompt rule did not hold: in eval run 2
# `rig` (Rust) and `avoid-ai-writing` (JavaScript) each cloned a repository to
# discover a fact the metadata had already stated, on questions where reaching
# for a container was itself the wrong move. Two separate prompt edits asking
# the model to justify a clone before making one changed nothing measurable.
#
# So it moves here, beside the size gate, as a refusal in code before any
# docker call - the same shape, from the same metadata request, which already
# returns `language` and was throwing it away.
#
# Only Python. Not "Jupyter Notebook", not "Shell", not "TypeScript with a
# Python SDK inside": GitHub reports one dominant language, the eval set's
# expectations were written against a Python-only image, and widening this set
# is a change that should be measured rather than guessed. `None` - GitHub
# could not detect a language - is allowed through, exactly as `size_kb=None`
# skips the size gate: an absent fact is not evidence of a bad one.
RUNNABLE_LANGUAGES = frozenset({"Python"})

# The repo name reaches us from the model, and from there would reach a clone
# URL, a volume name, and an argv. Anything that is not plainly owner/name is
# refused before any of that happens.
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# What subprocess.run returns; injectable so tests never touch Docker.
Runner = Callable[..., subprocess.CompletedProcess]


class SandboxError(RuntimeError):
    """The sandbox refused to do something, or Docker itself failed."""


class CommandResult(BaseModel):
    """One command's outcome, shaped for handing straight back to the model."""

    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int
    elapsed_s: float = Field(default=0.0)
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def validate_repo(repo: str) -> str:
    """Return ``repo`` if it is plainly ``owner/name``, else raise.

    The pattern alone is not enough. ``../x`` and ``-rm/x`` both satisfy it -
    dots and dashes are legal in GitHub names - and both are exactly the shapes
    that turn into a path escape or an option injection further down. So the
    components are checked as well.
    """
    if not isinstance(repo, str) or not REPO_PATTERN.match(repo):
        raise SandboxError(f"invalid repository name: {repo!r}")

    for part in repo.split("/"):
        if part in {".", ".."}:
            raise SandboxError(f"invalid repository name: {repo!r}")
        if part.startswith("-"):
            # Would be read as a flag by whatever consumes the argv.
            raise SandboxError(f"invalid repository name: {repo!r}")
    return repo


def _subprocess_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The only place this module actually spawns a process.

    Always an argv list, never ``shell=True``: the command string is only ever
    interpreted by bash *inside* the container, where it is already contained.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def build_argv(
    volume: str,
    command: str,
    *,
    network: bool,
    name: str,
    image: str = IMAGE,
) -> list[str]:
    """Build the `docker run` argv for one sandboxed command.

    Pure, so the flag guarantees can be asserted directly on the return value
    rather than inferred from a mock's call history. Every flag below is
    mandatory; see the threat model in ROADMAP-stage2.md for which one stops
    what.
    """
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        # Isolation. --network none is the control that makes exfiltration
        # during execution impossible rather than merely discouraged.
        "--network",
        "bridge" if network else "none",
        # Resource ceilings: fork bombs, runaway allocation, busy loops.
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        # Privilege: non-root, no capabilities, no way to regain any.
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        # The mounted volume is the only writable path that persists; /tmp is a
        # tmpfs so pip and git have somewhere to scribble.
        "--read-only",
        "--tmpfs",
        "/tmp",
        # No host environment, and no host filesystem besides the volume.
        "--env-file",
        "/dev/null",
        "--label",
        LABEL,
        "-v",
        f"{volume}:{WORKDIR}",
        "-w",
        WORKDIR,
        image,
        "bash",
        "-lc",
        command,
    ]
    return argv


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


class Sandbox:
    """A per-repo Docker volume plus the rules for running commands against it.

    Use as a context manager; ``destroy()`` runs on the way out whether the
    block finished or raised, because a volume left behind is disk the next run
    cannot use.
    """

    def __init__(
        self,
        repo: str,
        volume: str,
        runner: Optional[Runner] = None,
        image: str = IMAGE,
    ) -> None:
        self.repo = validate_repo(repo)
        self.volume = volume
        self.image = image
        self._runner: Runner = runner or _subprocess_runner
        self._destroyed = False
        self._cloned = False

    # --- lifecycle -------------------------------------------------------

    @classmethod
    def create(
        cls,
        repo: str,
        size_kb: Optional[int] = None,
        language: Optional[str] = None,
        runner: Optional[Runner] = None,
        image: str = IMAGE,
    ) -> "Sandbox":
        """Validate, gate on language and size, then create the volume.

        Every refusal happens before any Docker call: an invalid name must
        never reach an argv, and a Rust monorepo should cost nothing to
        decline. ``size_kb`` and ``language`` both come from
        ``tools.get_repo_metadata``; ``None`` means the caller did not look it
        up and that gate is skipped.
        """
        validate_repo(repo)
        # Language first: it is categorical ("this can never work here"),
        # while size is circumstantial ("not this one, not today").
        if language is not None and language not in RUNNABLE_LANGUAGES:
            raise SandboxError(
                f"{repo} is a {language} project and the sandbox image runs "
                f"Python only - it has no cargo, node, go or jdk, and a "
                f"toolchain installed during setup does not survive the call. "
                f"Blocker: unsupported_language"
            )
        if size_kb is not None and size_kb > MAX_REPO_KB:
            raise SandboxError(
                f"{repo} is {size_kb / 1000:.0f} MB, over the "
                f"{MAX_REPO_KB / 1000:.0f} MB clone limit"
            )

        volume = _volume_name(repo)
        sandbox = cls(repo, volume, runner=runner, image=image)
        sandbox._admin(
            ["docker", "volume", "create", "--label", LABEL, volume],
            "create volume",
        )
        log.info("sandbox: created volume %s for %s", volume, repo)
        return sandbox

    def destroy(self) -> None:
        """Remove the volume. Idempotent, and never raises.

        Called from ``finally``/``__exit__``, including on the paths where
        something has already gone wrong, so it must not replace the original
        exception with one of its own.
        """
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self._admin(
                ["docker", "volume", "rm", "-f", self.volume], "remove volume"
            )
            log.info("sandbox: removed volume %s", self.volume)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask errors
            log.warning("sandbox: could not remove volume %s: %s", self.volume, exc)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.destroy()
        return False

    # --- the three operations the agent gets -----------------------------

    def clone(self) -> CommandResult:
        """Shallow-clone the repository into the volume. Once per sandbox.

        ``--depth 1`` because history is bandwidth we never read, and the
        repository is only ever the validated ``owner/name``, so interpolating
        it into the shell string is safe by construction.
        """
        if self._cloned:
            raise SandboxError(f"{self.repo} has already been cloned")
        self._cloned = True

        url = f"https://github.com/{self.repo}.git"
        command = f"git clone --depth 1 {shlex.quote(url)} {shlex.quote(CLONE_PATH)}"
        return self._exec(command, network=True, timeout=SETUP_TIMEOUT_S)

    def setup(self, command: str) -> CommandResult:
        """Run one install-phase command *with* network."""
        return self._exec(command, network=True, timeout=SETUP_TIMEOUT_S)

    def run(self, command: str) -> CommandResult:
        """Run one command with no network at all."""
        return self._exec(command, network=False, timeout=RUN_TIMEOUT_S)

    def disk_usage_mb(self) -> float:
        """Size of the volume, measured from a throwaway container.

        The caller checks this against DISK_CAP_MB after every call; a repo
        that pulls a 2 GB wheel stops there and the report says so.
        """
        result = self._exec(
            f"du -sk {WORKDIR}", network=False, timeout=ADMIN_TIMEOUT_S
        )
        if not result.ok:
            raise SandboxError(f"could not measure volume: {result.stderr[:200]}")
        try:
            kb = int(result.stdout.split()[0])
        except (IndexError, ValueError) as exc:
            raise SandboxError(f"unparseable du output: {result.stdout[:200]}") from exc
        return kb / 1024

    # --- plumbing --------------------------------------------------------

    def _exec(self, command: str, *, network: bool, timeout: float) -> CommandResult:
        """Run one command in a fresh container, bounded by ``timeout``.

        A timeout is a result, not an exception: "the tests hung for 120
        seconds" is exactly the kind of finding the report should carry. The
        container is killed by name on the way out, because `subprocess`
        killing the client does not stop the daemon-side container.
        """
        if self._destroyed:
            raise SandboxError("sandbox has been destroyed")

        name = _container_name()
        argv = build_argv(
            self.volume, command, network=network, name=name, image=self.image
        )
        started = time.monotonic()

        try:
            completed = self._runner(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            self._kill(name)
            log.info("sandbox: %s timed out after %.0fs", self.repo, elapsed)
            return CommandResult(
                command=command,
                stderr=f"command exceeded the {timeout:.0f}s limit and was killed",
                exit_code=124,  # what `timeout(1)` reports, for the same reason
                elapsed_s=elapsed,
                timed_out=True,
            )
        except OSError as exc:  # docker missing, daemon down
            raise SandboxError(f"could not run docker: {exc}") from exc

        stdout, out_cut = _truncate(completed.stdout or "")
        stderr, err_cut = _truncate(completed.stderr or "")
        return CommandResult(
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=completed.returncode,
            elapsed_s=time.monotonic() - started,
            truncated=out_cut or err_cut,
        )

    def _kill(self, name: str) -> None:
        try:
            self._runner(["docker", "kill", name], timeout=ADMIN_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best effort on an error path
            log.warning("sandbox: could not kill container %s: %s", name, exc)

    def _admin(self, argv: list[str], what: str) -> subprocess.CompletedProcess:
        try:
            completed = self._runner(argv, timeout=ADMIN_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{what}: docker did not respond") from exc
        except OSError as exc:
            raise SandboxError(f"{what}: could not run docker: {exc}") from exc

        if completed.returncode != 0:
            raise SandboxError(f"{what}: {(completed.stderr or '').strip()[:200]}")
        return completed


def _volume_name(repo: str) -> str:
    """A unique, label-safe volume name derived from the repository.

    Suffixed with a random hex so two runs on the same repo cannot collide on
    one volume, and so a leftover from a crashed run is never silently reused.
    """
    slug = repo.replace("/", "-").replace(".", "-")
    return f"ai-monitor-{slug}-{uuid.uuid4().hex[:8]}"


def _container_name() -> str:
    return f"ai-monitor-run-{uuid.uuid4().hex[:12]}"


def prune(runner: Optional[Runner] = None) -> None:
    """Remove containers and volumes left behind by a crashed run.

    Everything this module creates is labelled, so the batch script can clear
    the wreckage of a previous run without touching anything else on the host.
    """
    run = runner or _subprocess_runner
    run(
        ["docker", "container", "prune", "-f", "--filter", f"label={LABEL}"],
        timeout=ADMIN_TIMEOUT_S,
    )
    run(
        ["docker", "volume", "prune", "-f", "--filter", f"label={LABEL}"],
        timeout=ADMIN_TIMEOUT_S,
    )
