"""Score investigation runs against the eval set's code-checked criteria.

Written before the batch ran, deliberately. Scoring code written *after* you
have seen the results is scoring code you have already, quietly, fitted to
them: a criterion that something failed is very easy to soften when you can
see which row it would rescue. These criteria are a commitment made in advance.

Every check here is an assert over stored data, never a judgement. There is no
model in this file. A question passes when all of the following hold:

- the verdict is one the question allows
- `execution` did what the question says it should have (see `check_execution`)
- where `blockers_any_of` is given, at least one of those blockers is named
- every ledger entry cites a tool call that actually happened
- the stop reason is `sufficient_info` rather than a cap
- the integrity rules did not have to downgrade the verdict

The last two are the ones worth explaining. A capped run is not a wrong answer,
it is an *unfinished* one, and counting it as a pass would reward a cap set too
low. A downgraded verdict means the agent claimed standing its trace did not
support - the run may still have landed on an allowed verdict, but it got there
by a route the ledger rules had to correct, and that is a failure of the thing
being measured.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ai_monitor.agent import investigate_runner as renderer

log = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
QUESTIONS_PATH = Path(__file__).resolve().parents[2] / "questions.yaml"

CAP_STOP_REASONS = frozenset(
    {"step_cap", "cost_cap", "time_cap", "sandbox_cap", "disk_cap"}
)
SANDBOX_TOOLS = frozenset({"sandbox_clone", "sandbox_setup", "sandbox_run"})
EXECUTION_MODES = frozenset({"required", "forbidden", "attempt"})


@dataclass
class Check:
    """One criterion's outcome. `detail` is what to print when it fails."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class Scored:
    repo: str
    question: str
    category: str
    checks: list[Check] = field(default_factory=list)
    verdict: Optional[str] = None
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    steps: int = 0
    observed: int = 0
    inspected: int = 0
    reported: int = 0
    sandbox_commands: int = 0
    critique_status: str = "not_run"

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


# --- reading what happened -----------------------------------------------


def executed_sandbox_calls(tool_calls: list[dict]) -> list[dict]:
    """Sandbox calls that actually ran something inside a container.

    A call refused before it reached Docker - an oversize repo turned away by
    the clone gate, a command sent before anything was cloned - returns a bare
    error and never starts a container. It costs nothing and proves nothing, so
    it does not count as having used the sandbox. An executed command always
    carries an exit code back, which is what separates the two.
    """
    ran = []
    for call in tool_calls:
        if call.get("tool") not in SANDBOX_TOOLS:
            continue
        if "exit_code" in (call.get("result_summary") or ""):
            ran.append(call)
    return ran


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    questions = data.get("questions") or []
    if not questions:
        raise ValueError(f"{path} contains no questions")

    for i, question in enumerate(questions, 1):
        mode = (question.get("expect") or {}).get("execution")
        if mode not in EXECUTION_MODES:
            raise ValueError(
                f"{path} question {i} ({question.get('repo')}): "
                f"execution must be one of {sorted(EXECUTION_MODES)}, got {mode!r}"
            )
    return questions


def latest_run(conn: sqlite3.Connection, repo: str, question: str) -> Optional[Any]:
    """The most recent stored run for this exact question.

    Matched on the question text as well as the repo: the same repository can
    appear in the set more than once, and earlier runs of a since-rewritten
    question must not be scored as if they answered the current one.
    """
    return conn.execute(
        """
        SELECT * FROM investigations
        WHERE repo = ? AND question = ?
        ORDER BY id DESC LIMIT 1
        """,
        (repo, question),
    ).fetchone()


# --- the criteria --------------------------------------------------------


def check_execution(mode: str, run_verdict: str, observed: int, ran: int,
                    blockers: list[str], allowed_blockers: list[str]) -> Check:
    """What the sandbox was supposed to have done.

    Three modes rather than a boolean, because a boolean made every
    `could_not_test` question unpassable: refusing to run something correctly
    never produces an `observed` entry, so "needs execution" and "produced
    execution evidence" are not the same claim.

    - `required`  the capability had to be exercised; an observed entry is the
                  only proof of that
    - `forbidden` the question was answerable without a container, and reaching
                  for one is the failure being tested for
    - `attempt`   running it was the right call, but an honest, named blocker
                  is an equally correct outcome
    """
    if mode == "required":
        return Check(
            "execution",
            observed >= 1,
            f"needed an observed entry, got {observed} (ran {ran} command(s))",
        )

    if mode == "forbidden":
        return Check(
            "execution",
            ran == 0,
            f"should not have used the sandbox, ran {ran} command(s)",
        )

    # attempt
    if observed >= 1:
        return Check("execution", True)
    if run_verdict == "could_not_test" and _has_allowed_blocker(
        blockers, allowed_blockers
    ):
        return Check("execution", True)
    return Check(
        "execution",
        False,
        "needed either an observed entry or could_not_test with a named "
        f"blocker; got verdict={run_verdict!r} blockers={blockers}",
    )


def _has_allowed_blocker(blockers: list[str], allowed: list[str]) -> bool:
    # No list means any named blocker is acceptable.
    return bool(blockers) if not allowed else bool(set(blockers) & set(allowed))


def score_one(question: dict, row: Optional[Any]) -> Scored:
    expect = question.get("expect") or {}
    scored = Scored(
        repo=question["repo"],
        question=question["question"].strip(),
        category=question.get("category", "-"),
    )

    if row is None:
        scored.checks.append(Check("ran", False, "no stored run for this question"))
        return scored
    scored.checks.append(Check("ran", True))

    report = json.loads(row["report"]) if row["report"] else None
    ledger = report["ledger"] if report else []
    blockers = report["blockers"] if report else []
    tool_calls = json.loads(row["tool_calls"] or "[]")
    ran = executed_sandbox_calls(tool_calls)

    scored.verdict = row["verdict"]
    scored.cost_usd = row["cost_usd"] or 0.0
    scored.wall_seconds = row["wall_seconds"] or 0.0
    scored.steps = row["steps_taken"] or 0
    scored.sandbox_commands = len(ran)
    scored.critique_status = _column(row, "critique_status", "not_run")
    for entry in ledger:
        setattr(scored, entry["kind"], getattr(scored, entry["kind"]) + 1)

    allowed = expect.get("verdict") or []
    scored.checks.append(
        Check(
            "verdict",
            row["verdict"] in allowed,
            f"got {row['verdict']!r}, expected one of {allowed}",
        )
    )

    scored.checks.append(
        check_execution(
            expect["execution"],
            row["verdict"],
            scored.observed,
            len(ran),
            blockers,
            expect.get("blockers_any_of") or [],
        )
    )

    if expect.get("blockers_any_of"):
        wanted = expect["blockers_any_of"]
        # Only binding once the run actually declined to test something.
        applies = row["verdict"] == "could_not_test"
        scored.checks.append(
            Check(
                "blockers",
                not applies or bool(set(blockers) & set(wanted)),
                f"could_not_test named {blockers}, none of which is in {wanted}",
            )
        )

    uncited = [
        e["source"]
        for e in ledger
        if not _cites_a_real_call(e.get("source", ""), tool_calls)
    ]
    scored.checks.append(
        Check(
            "citations",
            not uncited,
            f"{len(uncited)} entry/entries cite no tool call: {uncited[:3]}",
        )
    )

    scored.checks.append(
        Check(
            "stop_reason",
            row["stop_reason"] == "sufficient_info",
            f"stopped on {row['stop_reason']!r} - a capped run is unfinished, "
            "not answered",
        )
    )

    scored.checks.append(
        Check(
            "not_downgraded",
            not row["downgraded"],
            "the integrity rules had to downgrade the verdict",
        )
    )
    return scored


def _cites_a_real_call(source: str, tool_calls: list[dict]) -> bool:
    text = (source or "").lower()
    return any((c.get("tool") or "").lower() in text for c in tool_calls)


def _column(row: Any, name: str, default: Any) -> Any:
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def score(
    conn: sqlite3.Connection, questions: Optional[list[dict]] = None
) -> list[Scored]:
    questions = questions if questions is not None else load_questions()
    return [
        score_one(q, latest_run(conn, q["repo"], q["question"].strip()))
        for q in questions
    ]


# --- reporting -----------------------------------------------------------


def summarize(results: list[Scored]) -> dict:
    by_check: dict[str, int] = {}
    for result in results:
        for check in result.failures:
            by_check[check.name] = by_check.get(check.name, 0) + 1

    verdicts: dict[str, int] = {}
    for result in results:
        key = result.verdict or "none"
        verdicts[key] = verdicts.get(key, 0) + 1

    return {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "verdicts": verdicts,
        "failures_by_check": by_check,
        "cost_usd": sum(r.cost_usd for r in results),
        "wall_seconds": sum(r.wall_seconds for r in results),
        "observed": sum(r.observed for r in results),
        "inspected": sum(r.inspected for r in results),
        "reported": sum(r.reported for r in results),
        "critiques_run": sum(1 for r in results if r.critique_status == "ok"),
        "critiques_failed": sum(1 for r in results if r.critique_status == "failed"),
    }


def render_report(results: list[Scored]) -> str:
    stats = summarize(results)
    lines = [
        "# Proofrun eval",
        "",
        f"{stats['passed']} of {stats['n']} questions pass every criterion. "
        f"${stats['cost_usd']:.2f}, {stats['wall_seconds'] / 60:.0f} min.",
        "",
        "Verdicts: "
        + ", ".join(f"{n} {v}" for v, n in sorted(stats["verdicts"].items()))
        + ".",
        "",
        f"Evidence: {stats['observed']} observed, {stats['inspected']} inspected, "
        f"{stats['reported']} reported. Grounding critique ran on "
        f"{stats['critiques_run']} of {stats['n']}"
        + (
            f" ({stats['critiques_failed']} failed to run)."
            if stats["critiques_failed"]
            else "."
        ),
        "",
        "A question passes only if every criterion holds: the verdict is one it "
        "allows, the sandbox did what it should have, every ledger entry cites "
        "a real tool call, the run stopped because it was finished rather than "
        "capped, and no verdict had to be downgraded.",
        "",
        "| Question | Category | Verdict | Obs | Insp | Rep | Cmds | Cost | Pass | Failed |",
        "|---|---|---|---:|---:|---:|---:|---:|:--:|---|",
    ]
    for r in results:
        failed = ", ".join(c.name for c in r.failures) or "-"
        lines.append(
            f"| {renderer.clean(r.question, 90)} | {r.category} "
            f"| {r.verdict or 'none'} | {r.observed} | {r.inspected} | {r.reported} "
            f"| {r.sandbox_commands} | ${r.cost_usd:.3f} "
            f"| {'yes' if r.passed else 'NO'} | {failed} |"
        )

    if stats["failures_by_check"]:
        lines += ["", "## Where it failed", ""]
        for name, count in sorted(
            stats["failures_by_check"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"- **{name}**: {count}")
        lines += [""]
        for r in results:
            if r.passed:
                continue
            lines.append(f"### {renderer.clean(r.repo, 60)}")
            for check in r.failures:
                lines.append(f"- `{check.name}` - {renderer.clean(check.detail, 300)}")
            lines.append("")

    return "\n".join(lines)


def run(
    conn: sqlite3.Connection,
    questions_path: Path = QUESTIONS_PATH,
    write: bool = True,
) -> tuple[list[Scored], Optional[Path]]:
    results = score(conn, load_questions(questions_path))
    if not write:
        return results, None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"eval-investigations-{stamp}.md"
    path.write_text(render_report(results))
    return results, path
