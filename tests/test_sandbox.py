"""Sandbox tests.

The whole point of the sandbox is that its guarantees hold without the model's
cooperation, so these assert on the constructed docker argv rather than on
behaviour observed from inside a container. That keeps the suite offline: every
test here injects a fake runner and no Docker daemon is touched.

The single exception is the integration test at the bottom, which needs Docker
and skips cleanly without it.
"""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ai_monitor.agent import sandbox as sb
from ai_monitor.agent.sandbox import Sandbox, SandboxError


class FakeRunner:
    """Records every argv it is handed and replays scripted results.

    ``results`` maps a substring of the argv (joined) to the CompletedProcess or
    exception to produce; anything unmatched succeeds silently.
    """

    def __init__(self, results=None, default=("", "", 0)):
        self.results = results or {}
        self.default = default
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, timeout=None, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append({"timeout": timeout, **kwargs})

        joined = " ".join(argv)
        for needle, outcome in self.results.items():
            if needle in joined:
                if isinstance(outcome, BaseException):
                    raise outcome
                stdout, stderr, code = outcome
                return subprocess.CompletedProcess(argv, code, stdout, stderr)

        stdout, stderr, code = self.default
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    @property
    def docker_runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


def make_sandbox(runner=None, repo="owner/name"):
    runner = runner or FakeRunner()
    return Sandbox.create(repo, runner=runner), runner


# --- the network boundary ------------------------------------------------


def test_run_has_no_network_and_setup_does():
    """The whole reason setup and run are separate tools."""
    box, runner = make_sandbox()
    box.run("pytest -q")
    box.setup("pip install -e .")

    run_argv, setup_argv = runner.docker_runs
    assert "--network" in run_argv
    assert run_argv[run_argv.index("--network") + 1] == "none"
    assert setup_argv[setup_argv.index("--network") + 1] != "none"


def test_clone_gets_network():
    box, runner = make_sandbox()
    box.clone()
    argv = runner.docker_runs[0]
    assert argv[argv.index("--network") + 1] != "none"
    assert "git clone --depth 1" in argv[-1]


def test_clone_url_is_built_from_the_validated_name():
    """The URL is exactly the repo that passed validation, and nothing else.

    shlex.quote adds no quotes here, and that is the point: a name needing
    them could not have passed validate_repo in the first place.
    """
    box, runner = make_sandbox(repo="Foo_Bar/baz-1.2")
    box.clone()
    command = runner.docker_runs[0][-1]
    assert command == (
        "git clone --depth 1 https://github.com/Foo_Bar/baz-1.2.git /work/repo"
    )
    assert command.split() == command.strip().split()  # no injected whitespace


def test_clone_only_happens_once():
    box, _ = make_sandbox()
    box.clone()
    with pytest.raises(SandboxError):
        box.clone()


# --- the mandatory flags -------------------------------------------------


# Both branches, because the setup branch is the one that *has* network and so
# is the one where a dropped isolation flag would matter most.
@pytest.mark.parametrize("network", [True, False], ids=["setup", "run"])
@pytest.mark.parametrize(
    "flag,value",
    [
        ("--rm", None),
        ("--memory", "2g"),
        ("--cpus", "2"),
        ("--pids-limit", "256"),
        ("--user", "1000:1000"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--read-only", None),
        ("--tmpfs", "/tmp"),
        ("--env-file", "/dev/null"),
        ("--label", sb.LABEL),
        ("--network", None),
        ("-w", "/work"),
    ],
)
def test_every_mandatory_flag_is_present(flag, value, network):
    argv = sb.build_argv("vol", "true", network=network, name="c")
    # Exactly once, not merely present: docker honours the *last* occurrence of
    # a repeated flag, so a duplicate would silently override the value these
    # tests assert on.
    assert argv.count(flag) == 1
    if value is not None:
        assert argv[argv.index(flag) + 1] == value


@pytest.mark.parametrize("network", [True, False], ids=["setup", "run"])
def test_no_flag_is_passed_twice(network):
    """A repeated flag is how a control gets silently overridden."""
    argv = sb.build_argv("vol", "true", network=network, name="c")
    # Only the docker-run options; everything after the image belongs to bash.
    options = [a for a in argv[: argv.index(sb.IMAGE)] if a.startswith("-")]
    assert len(options) == len(set(options)), sorted(options)


def test_the_volume_is_the_only_mount():
    argv = sb.build_argv("vol", "true", network=False, name="c")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a in {"-v", "--volume"}]
    assert mounts == ["vol:/work"]

    # The equals-forms and --volumes-from would all bring in a host path while
    # slipping past a check that only looks for the bare tokens.
    for arg in argv:
        assert not arg.startswith(("--mount", "--volumes-from", "--volume=", "-v="))

    # /tmp is the only tmpfs; another one is another writable surface.
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert tmpfs == ["/tmp"]


def test_the_command_is_handed_to_bash_inside_the_container():
    """Never interpreted by a host shell; bash only ever sees it inside."""
    argv = sb.build_argv("vol", "rm -rf /", network=False, name="c")
    assert argv[-3:] == ["bash", "-lc", "rm -rf /"]


# --- no secrets cross the boundary ---------------------------------------


def test_argv_carries_no_environment_passthrough():
    argv = sb.build_argv("vol", "true", network=True, name="c")
    assert "-e" not in argv
    assert not any(a == "--env" or a.startswith("--env=") for a in argv)
    # --env-file /dev/null is the opposite: it is what guarantees emptiness.
    assert argv[argv.index("--env-file") + 1] == "/dev/null"


def test_host_secrets_never_reach_the_argv(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-sentinel-value")

    box, runner = make_sandbox()
    box.setup("pip install -e .")
    box.run("pytest")

    for argv in runner.calls:
        joined = " ".join(argv)
        assert "sentinel" not in joined


def test_no_env_flag_reaches_any_docker_call():
    """`-e VAR` with no `=` passes the host's value while putting no secret
    text in the argv, so the flag itself must never appear - checking the argv
    for secret *values* would not catch it."""
    box, runner = make_sandbox()
    box.clone()
    box.setup("pip install -e .")
    box.run("pytest")

    for argv in runner.calls:
        for arg in argv[:-1]:  # the trailing command string is bash's, not docker's
            assert arg not in {"-e", "--env"}
            assert not arg.startswith(("--env=", "-e="))


def test_subprocess_is_never_called_with_a_shell(monkeypatch):
    """A host-side shell would make the model's command string host-executable."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb._subprocess_runner(["docker", "version"], timeout=5)

    assert seen["kwargs"].get("shell") is not True
    assert isinstance(seen["argv"], list)
    assert "env" not in seen["kwargs"]  # host env is not curated, just unused


def test_no_call_in_the_module_passes_shell_true():
    """Checked over the AST, so a docstring mentioning it cannot pass or fail it."""
    import ast

    with open(sb.__file__) as fh:
        tree = ast.parse(fh.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert kw.arg != "shell", ast.dump(node)


# --- input validation ----------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "../x",
        "x/../y",
        "../..",
        "--rm/x",
        "x/--rm",
        "-v/x",
        "owner/name; rm -rf /",
        "owner name",
        "owner/name/extra",
        "owner",
        "",
        "owner/na$me",
        "owner/name\nother/name",
    ],
)
def test_hostile_repo_names_are_refused_before_any_docker_call(repo):
    runner = FakeRunner()
    with pytest.raises(SandboxError):
        Sandbox.create(repo, runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("repo", ["owner/name", "a-b/c.d", "Foo_Bar/baz-1.2"])
def test_ordinary_repo_names_are_accepted(repo):
    assert sb.validate_repo(repo) == repo


def test_oversize_repo_is_refused_before_any_docker_call():
    runner = FakeRunner()
    with pytest.raises(SandboxError, match="clone limit"):
        Sandbox.create("owner/name", size_kb=sb.MAX_REPO_KB + 1, runner=runner)
    assert runner.calls == []


@pytest.mark.parametrize("language", ["Rust", "JavaScript", "Go", "TypeScript"])
def test_a_non_python_repo_is_refused_before_any_docker_call(language):
    """The gate that two prompt edits could not enforce.

    `rig` (Rust) and `avoid-ai-writing` (JavaScript) both cloned in eval run 2
    on questions their metadata already settled. The refusal has to cost
    nothing, so it happens before `docker volume create`.
    """
    runner = FakeRunner()
    with pytest.raises(SandboxError, match="unsupported_language"):
        Sandbox.create("owner/name", language=language, runner=runner)
    assert runner.calls == []


def test_the_refusal_names_the_language_it_refused():
    """The message is the model's whole view of why, so it has to say."""
    with pytest.raises(SandboxError) as caught:
        Sandbox.create("owner/name", language="Rust", runner=FakeRunner())

    message = str(caught.value)
    assert "Rust" in message
    # The extraction prompt maps this token onto the blocker of the same name.
    assert "unsupported_language" in message


def test_a_python_repo_passes_the_language_gate():
    """The positive control: the gate must not refuse what the image can run."""
    runner = FakeRunner()
    box = Sandbox.create("owner/name", language="Python", runner=runner)
    assert runner.calls[0][:3] == ["docker", "volume", "create"]
    assert box.volume.startswith("ai-monitor-owner-name-")


def test_an_undetected_language_is_allowed_through():
    """`None` means GitHub could not tell, not that it told us something bad.

    Same treatment as `size_kb=None`: an absent fact does not get to refuse a
    repository, because then a metadata hiccup would silently narrow the agent.
    """
    runner = FakeRunner()
    Sandbox.create("owner/name", language=None, runner=runner)
    assert runner.calls[0][:3] == ["docker", "volume", "create"]


def test_the_language_gate_fires_before_the_size_gate():
    """Categorical beats circumstantial: Rust is never runnable here, at any size."""
    with pytest.raises(SandboxError, match="unsupported_language"):
        Sandbox.create(
            "owner/name",
            size_kb=sb.MAX_REPO_KB + 1,
            language="Rust",
            runner=FakeRunner(),
        )


def test_repo_under_the_size_gate_is_allowed():
    box, runner = make_sandbox()
    assert runner.calls[0][:3] == ["docker", "volume", "create"]
    assert box.volume.startswith("ai-monitor-owner-name-")


# --- timeouts, truncation, cleanup ---------------------------------------


def test_timeout_kills_the_container_and_returns_a_result():
    """A hung test suite is a finding, not an exception."""
    runner = FakeRunner(
        results={"bash -lc": subprocess.TimeoutExpired(cmd="docker", timeout=120)}
    )
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner

    result = box.run("sleep 9999")

    assert result.timed_out is True
    assert result.exit_code == 124
    assert not result.ok
    kills = [c for c in runner.calls if c[:2] == ["docker", "kill"]]
    assert len(kills) == 1
    # Killed by the same name the container was started under.
    started = runner.docker_runs[0]
    assert kills[0][2] == started[started.index("--name") + 1]


def test_setup_timeout_uses_the_longer_budget_and_still_kills():
    """The 300s network-enabled path needs the same kill as the 120s one."""
    runner = FakeRunner(
        results={"bash -lc": subprocess.TimeoutExpired(cmd="docker", timeout=300)}
    )
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner

    result = box.setup("pip install -e .")

    assert result.timed_out is True
    assert result.exit_code == 124
    assert "300s" in result.stderr
    assert [c for c in runner.calls if c[:2] == ["docker", "kill"]]


def test_a_failing_kill_still_returns_the_timeout_result():
    """Cleanup failing must not turn a reportable timeout into a crash."""
    runner = FakeRunner(
        results={
            "docker kill": OSError("daemon gone"),
            "bash -lc": subprocess.TimeoutExpired(cmd="docker", timeout=120),
        }
    )
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner

    result = box.run("sleep 9999")

    assert result.timed_out is True
    assert result.exit_code == 124


def test_output_is_truncated_per_stream():
    flood = "x" * (sb.MAX_OUTPUT_CHARS * 3)
    runner = FakeRunner(default=(flood, flood, 0))
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner

    result = box.run("cat huge.log")

    assert len(result.stdout) == sb.MAX_OUTPUT_CHARS
    assert len(result.stderr) == sb.MAX_OUTPUT_CHARS
    assert result.truncated is True


def test_short_output_is_not_marked_truncated():
    runner = FakeRunner(default=("ok", "", 0))
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner
    result = box.run("echo ok")
    assert result.truncated is False
    assert result.ok


def test_nonzero_exit_is_reported_not_raised():
    runner = FakeRunner(default=("", "ImportError: no module named torch", 1))
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner
    result = box.run("pytest -q")
    assert result.exit_code == 1
    assert "ImportError" in result.stderr


def test_destroy_runs_even_when_a_command_raises():
    runner = FakeRunner(results={"bash -lc": OSError("docker daemon gone")})

    with pytest.raises(SandboxError):
        with Sandbox("owner/name", "vol-1", runner=runner) as box:
            box.run("pytest")

    removals = [c for c in runner.calls if c[:3] == ["docker", "volume", "rm"]]
    assert removals, "the volume must be removed on the exception path"


def test_context_manager_destroys_the_volume():
    runner = FakeRunner()
    with Sandbox.create("owner/name", runner=runner) as box:
        box.run("true")
    assert ["docker", "volume", "rm", "-f", box.volume] in runner.calls


def test_destroy_is_idempotent():
    box, runner = make_sandbox()
    box.destroy()
    box.destroy()
    removals = [c for c in runner.calls if c[:3] == ["docker", "volume", "rm"]]
    assert len(removals) == 1


def test_destroy_never_raises_even_when_docker_fails():
    runner = FakeRunner(results={"volume rm": ("", "no such volume", 1)})
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner
    box.destroy()  # must not raise


def test_commands_after_destroy_are_refused():
    box, _ = make_sandbox()
    box.destroy()
    with pytest.raises(SandboxError):
        box.run("true")


def test_disk_usage_parses_du_output():
    runner = FakeRunner(default=("2097152\t/work\n", "", 0))
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner
    assert box.disk_usage_mb() == pytest.approx(2048.0)


def test_disk_usage_measures_without_network():
    runner = FakeRunner(default=("100\t/work\n", "", 0))
    box = Sandbox.create("owner/name", runner=FakeRunner())
    box._runner = runner
    box.disk_usage_mb()
    argv = runner.docker_runs[0]
    assert argv[argv.index("--network") + 1] == "none"


def test_prune_filters_on_the_label():
    runner = FakeRunner()
    sb.prune(runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any("container prune" in j and sb.LABEL in j for j in joined)
    assert any("volume prune" in j and sb.LABEL in j for j in joined)


def test_timeouts_match_the_contract():
    box, runner = make_sandbox()
    box.setup("pip install -e .")
    box.run("pytest")
    setup_timeout = runner.kwargs[-2]["timeout"]
    run_timeout = runner.kwargs[-1]["timeout"]
    assert setup_timeout == sb.SETUP_TIMEOUT_S == 300
    assert run_timeout == sb.RUN_TIMEOUT_S == 120


# --- integration ---------------------------------------------------------


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_real_container_runs_and_has_no_network_during_run():
    """The only test that touches Docker. Proves the argv means what it says.

    Requires the image to have been built:
        docker build -t ai-monitor-sandbox:latest -f sandbox/Dockerfile sandbox
    """
    built = subprocess.run(
        ["docker", "image", "inspect", sb.IMAGE],
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        pytest.skip(f"{sb.IMAGE} not built")

    with Sandbox.create("owner/name") as box:
        hello = box.run("echo hello")
        assert hello.ok and "hello" in hello.stdout

        # The volume is writable by the unprivileged user, the root fs is not.
        assert box.run("touch /work/marker").ok
        assert not box.run("touch /marker").ok

        # No network during run: the request must fail, not hang.
        offline = box.run("curl -sS -m 3 https://example.com")
        assert offline.exit_code != 0

        # Nothing of the host's environment is visible.
        env = box.run("env")
        assert "ANTHROPIC_API_KEY" not in env.stdout
        assert "GITHUB_TOKEN" not in env.stdout

        # The privilege flags are asserted in the argv elsewhere; here we check
        # they actually took effect, which the argv alone cannot show.
        ident = box.run("id -u; id -g")
        assert ident.stdout.split() == ["1000", "1000"]

        caps = box.run("grep CapEff /proc/self/status")
        assert caps.stdout.split()[-1].strip("0") == "", caps.stdout

        nnp = box.run("grep NoNewPrivs /proc/self/status")
        assert nnp.stdout.split()[-1] == "1", nnp.stdout

        # An install in `setup` must still be there in `run`. Under --read-only
        # pip falls back to a user-site install; PYTHONUSERBASE is what puts
        # that fallback on the volume instead of the tmpfs, where it would
        # vanish before the next container and turn a working repo into a
        # refuted one. No network needed to prove the mechanism.
        usersite = box.run("python -c 'import site; print(site.getusersitepackages())'")
        target = usersite.stdout.strip()
        assert target.startswith("/work/"), target

        seeded = box.setup(f"mkdir -p {target} && echo 'VALUE = 42' > {target}/probe.py")
        assert seeded.ok, seeded.stderr
        imported = box.run("python -c 'import probe; print(probe.VALUE)'")
        assert imported.ok and "42" in imported.stdout, imported.stderr

        assert box.disk_usage_mb() >= 0
        volume = box.volume

    remaining = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={volume}"],
        capture_output=True,
        text=True,
    )
    assert remaining.stdout.strip() == "", "the volume must not survive the block"

    # --rm should leave nothing, including on the paths where a container was
    # killed rather than exiting on its own.
    containers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={sb.LABEL_KEY}"],
        capture_output=True,
        text=True,
    )
    assert containers.stdout.strip() == "", "no container may survive the block"
