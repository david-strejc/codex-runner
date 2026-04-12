from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable

from .checks import DeterministicReport, run_deterministic_checks
from .config import FinishContract, init_plan, load_finish_contract
from .tmux import TmuxSession


SESSION_ID_RE = re.compile(r"session id:\s*([0-9A-Za-z._-]+)")
SHELL_COMMANDS = {"zsh", "bash", "sh", "fish", "dash", "ksh"}
TRUST_PROMPT = "Do you trust the contents of this directory?"
TRUST_ACCEPT_LABEL = "Yes, continue"
PLACEHOLDER_TASK = "Define the task and finish criteria for this repository."


@dataclass(slots=True)
class WorkerResult:
    status: str
    summary: str
    changes_made: list[str]
    remaining_work: list[str]
    blocking_reason: str | None
    verification: list[str]

    @classmethod
    def fallback(cls, reason: str) -> "WorkerResult":
        return cls(
            status="needs_more",
            summary=reason,
            changes_made=[],
            remaining_work=[reason],
            blocking_reason=None,
            verification=[],
        )


@dataclass(slots=True)
class JudgeDecision:
    decision: str
    summary: str
    reasons: list[str]
    instructions_for_worker: str
    missing_checks: list[str]

    @classmethod
    def fallback(cls, reason: str) -> "JudgeDecision":
        return cls(
            decision="continue",
            summary=reason,
            reasons=[reason],
            instructions_for_worker="Keep working. The judge could not parse a valid completion decision.",
            missing_checks=[reason],
        )


@dataclass(slots=True)
class RoundRecord:
    round_number: int
    worker_exit_code: int
    judge_exit_code: int
    worker_session_id: str | None
    worker_result: dict[str, Any]
    judge_decision: dict[str, Any]
    deterministic_report: dict[str, Any]


@dataclass(slots=True)
class RunnerState:
    version: int = 1
    status: str = "idle"
    task: str = ""
    session_name: str = ""
    worker_session_id: str | None = None
    current_round: int = 0
    started_at: str = ""
    updated_at: str = ""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "batch"
    worker_pane: str | None = None
    judge_pane: str | None = None
    judge_session_id: str | None = None
    watcher_pid: int | None = None
    window_target: str | None = None
    cleanup_scope: str | None = None
    worker_reboots: int = 0
    standby: bool = False
    started_without_task: bool = False
    awaiting_worker_change_after_complete: bool = False
    completion_latch_hash: str | None = None
    startup_turn_consumed: bool = False
    worker_event_offset: int = 0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _session_suffix() -> str:
    return str(time.time_ns())


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'timestamp': _now_iso(), **payload}) + '\n')


def _base_codex_args(*, sandbox: str, bypass: bool, approval_policy: str | None = None) -> list[str]:
    args: list[str] = []
    if bypass:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    elif approval_policy:
        args.extend(["-a", approval_policy])
    args.extend(["-s", sandbox])
    return args


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tail(path: Path, *, max_lines: int = 80, max_chars: int = 6000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    clipped = "\n".join(lines[-max_lines:])
    if len(clipped) <= max_chars:
        return clipped
    return clipped[-max_chars:]


def _clip_text(text: str, *, max_lines: int, max_chars: int) -> str:
    lines = text.splitlines()
    clipped = "\n".join(lines[-max_lines:])
    if len(clipped) <= max_chars:
        return clipped
    return clipped[-max_chars:]


def _worker_turn_summary_from_event(event: dict[str, Any]) -> str:
    input_messages = event.get("input_messages", [])
    if not isinstance(input_messages, list):
        input_messages = []
    inputs = "\n".join(f"- {item}" for item in input_messages if isinstance(item, str)) or "- (none)"
    last_message = str(event.get("last_assistant_message") or "").strip() or "(none)"
    turn_id = str(event.get("turn_id") or "").strip() or "(unknown)"
    return (
        f"Worker turn id: {turn_id}\n\n"
        f"Worker input messages:\n{inputs}\n\n"
        f"Worker last assistant message:\n{last_message}\n"
    )


def _worker_maybe_claims_completion(event: dict[str, Any]) -> bool:
    message = str(event.get("last_assistant_message") or "").strip().lower()
    if not message:
        return False
    negative_markers = (
        "not complete",
        "not finished",
        "still not complete",
        "still not finished",
        "remaining work",
        "branch is still not complete",
        "not in a finishable state",
        "i did not edit anything in this turn",
    )
    if any(marker in message for marker in negative_markers):
        return False
    completion_markers = (
        "task is complete",
        "task complete",
        "done.",
        "all set",
        "finished for now",
        "no remaining work",
        "no further work",
        "closeout criteria are currently satisfied",
        "the cleanup closeout criteria are currently satisfied",
        "really finished",
    )
    return any(marker in message for marker in completion_markers)


def _shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)

def _notify_hook_argv(*, role: str, output_path: Path) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("notify_hook.py")),
        role,
        str(output_path),
    ]


def _pane_has_trust_prompt(pane_text: str) -> bool:
    return TRUST_PROMPT in pane_text and TRUST_ACCEPT_LABEL in pane_text


def _auto_accept_trust_prompt(
    tmux: TmuxSession,
    pane_id: str,
    *,
    timeout_seconds: int = 8,
    on_accept: Callable[[], None] | None = None,
) -> bool:
    deadline = time.time() + timeout_seconds
    sent = False
    while time.time() < deadline:
        try:
            pane_text = tmux.capture_pane(pane_id, lines=80)
        except Exception:
            return sent
        if not isinstance(pane_text, str):
            pane_text = str(pane_text)
        if _pane_has_trust_prompt(pane_text):
            if not sent:
                tmux.press_enter(pane_id)
                sent = True
                if on_accept:
                    on_accept()
            time.sleep(0.5)
            continue
        if sent:
            return True
        if pane_text.strip() or SESSION_ID_RE.search(pane_text) or "OpenAI Codex" in pane_text or "Standing by" in pane_text:
            return False
        time.sleep(0.5)
    return sent



def _extract_session_id_from_text(text: str) -> str | None:
    match = SESSION_ID_RE.search(text)
    return match.group(1) if match else None


def _extract_session_id(console_log: Path) -> str | None:
    if not console_log.exists():
        return None
    return _extract_session_id_from_text(console_log.read_text(encoding="utf-8", errors="replace"))


def _load_structured_output(path: Path, result_type: type[WorkerResult] | type[JudgeDecision]) -> WorkerResult | JudgeDecision:
    if not path.exists():
        reason = f"Missing structured output file: {path}"
        return result_type.fallback(reason)  # type: ignore[return-value]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return result_type.fallback(f"Invalid JSON output: {exc}")  # type: ignore[return-value]
    if result_type is WorkerResult:
        return WorkerResult(
            status=str(payload.get("status", "needs_more")),
            summary=str(payload.get("summary", "")).strip() or "No worker summary",
            changes_made=[str(item) for item in payload.get("changes_made", [])],
            remaining_work=[str(item) for item in payload.get("remaining_work", [])],
            blocking_reason=str(payload.get("blocking_reason")) if payload.get("blocking_reason") else None,
            verification=[str(item) for item in payload.get("verification", [])],
        )
    return JudgeDecision(
        decision=str(payload.get("decision", "continue")),
        summary=str(payload.get("summary", "")).strip() or "No judge summary",
        reasons=[str(item) for item in payload.get("reasons", [])],
        instructions_for_worker=str(payload.get("instructions_for_worker", "")).strip() or "Continue working.",
        missing_checks=[str(item) for item in payload.get("missing_checks", [])],
    )


def _git_capture(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.strip()
    return completed.stdout.strip()


def _repo_context(repo_root: Path) -> dict[str, str]:
    if not (repo_root / ".git").exists():
        return {"status": "Not a git repository.", "diff_stat": "", "diff_names": ""}
    return {
        "status": _git_capture(repo_root, "status", "--short"),
        "diff_stat": _git_capture(repo_root, "diff", "--stat"),
        "diff_names": _git_capture(repo_root, "diff", "--name-only"),
    }


def _worker_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "summary",
            "changes_made",
            "remaining_work",
            "blocking_reason",
            "verification",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["done", "needs_more", "blocked"]},
            "summary": {"type": "string"},
            "changes_made": {"type": "array", "items": {"type": "string"}},
            "remaining_work": {"type": "array", "items": {"type": "string"}},
            "blocking_reason": {"type": ["string", "null"]},
            "verification": {"type": "array", "items": {"type": "string"}},
        },
    }


def _judge_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "summary", "reasons", "instructions_for_worker", "missing_checks"],
        "properties": {
            "decision": {"type": "string", "enum": ["continue", "complete", "blocked"]},
            "summary": {"type": "string"},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "instructions_for_worker": {"type": "string"},
            "missing_checks": {"type": "array", "items": {"type": "string"}},
        },
    }


def _build_worker_prompt(
    task: str,
    finish_contract_markdown: str,
    todo_markdown: str,
    judge_feedback: str,
    round_number: int,
) -> str:
    return f"""You are the worker in a supervised coding loop.

Task:
{task}

Round:
{round_number}

Rules:
- Work directly in the repository.
- Read and obey `.plan/TODO.md`, `.plan/FINISH_CRITERIA.md`, and `.plan/WORK_LOCK` if present.
- Ignore `.plan/JUDGE_TODO.md`; that file belongs to the judge, not the worker.
- Do not ask the human whether you should continue. The judge decides that.
- Do not say `would you like me to continue`, `should I proceed`, `if you want, I can`, `let me know if you want`, or similar optional-next-step language.
- If you think the task is done, verify it against the finish criteria before returning `done`.
- Do not return `done` just because you finished one slice. Return `done` only when the whole requested task is complete and the finish criteria actually pass.
- Return `blocked` only for real blockers: missing credentials, destructive ambiguity, or missing external authority.
- `blocked` is forbidden for hesitation, uncertainty about the next local step, missing verification, or because more work still exists in TODO/WORK_LOCK.
- Prefer continuing work over asking for more instructions.
- Treat follow-ups like `continue`, `gogogo`, `yes continue`, `go ahead`, and `proceed` as proof that the previous stop was premature and should not be repeated.

Latest judge feedback:
{judge_feedback or "No prior judge feedback. Start from the task and repository state."}

Finish criteria:
{finish_contract_markdown}

Current TODO:
{todo_markdown}

Return only JSON matching the required schema.
"""


def _build_judge_prompt(
    task: str,
    finish_contract_markdown: str,
    deterministic_report: DeterministicReport,
    worker_result: WorkerResult,
    worker_exit_code: int,
    worker_console_tail: str,
    repo_context: dict[str, str],
    round_number: int,
) -> str:
    deterministic_json = json.dumps(deterministic_report.to_dict(), indent=2)
    worker_json = json.dumps(asdict(worker_result), indent=2)
    return f"""You are the independent work judge.

Your job is to decide whether the worker is REALLY done.

Task:
{task}

Round:
{round_number}

Hard rules:
- Do not trust the worker's completion claim by default.
- Prefer `continue` over `complete` when uncertain.
- If any deterministic required check failed, completion should normally be rejected.
- Choose `blocked` only for real human blockers, not for laziness or missing verification.
- Your `instructions_for_worker` must be direct and actionable.
- Maintain only a tiny scratchpad in `.plan/JUDGE_TODO.md`.
- `.plan/JUDGE_TODO.md` is for current completion-evaluation notes only: evidence, missing proof, real blockers, and the next concrete verdict check.
- Do not use `.plan/JUDGE_TODO.md` for product planning, feature design, scope expansion, or coding ideas unrelated to judging whether the current work is actually done.
- Treat these worker phrases as red flags that normally require `continue`, not `complete`: `would you like me to continue`, `should I proceed`, `if you want, I can`, `let me know if you want`, `all set`, `task complete`, `done`.
- Reject any worker stop that leaves active TODO items, WORK_LOCK, missing required paths, or failing verify commands.
- Reject soft blockage. `blocked` is valid only for missing credentials, destructive ambiguity, or external dependency failure with no practical workaround.
- Reject optional-next-step menus when one next step is clearly required.
- If the worker merely reports progress or one completed slice, that is not completion of the overall task.
- The user's session history contains repeated `continue` / `gogogo` follow-ups after premature stops. Your job is to suppress that pattern aggressively.

Finish criteria:
{finish_contract_markdown}

Deterministic report:
{deterministic_json}

Worker exit code:
{worker_exit_code}

Worker structured result:
{worker_json}

Worker console tail:
{worker_console_tail or "(empty)"}

Git status:
{repo_context['status'] or '(clean)'}

Git diff --stat:
{repo_context['diff_stat'] or '(no diff stat)'}

Git diff --name-only:
{repo_context['diff_names'] or '(no changed files)'}

Return only JSON matching the required schema.
"""


def _build_interactive_worker_prompt(task: str, finish_contract_markdown: str, todo_markdown: str) -> str:
    return f"""You are the interactive worker.

Task:
{task}

Rules:
- Work directly in this repository.
- Read and obey `.plan/TODO.md`, `.plan/FINISH_CRITERIA.md`, and `.plan/WORK_LOCK` if present.
- Ignore `.plan/JUDGE_TODO.md`; that file belongs to the judge.
- The user may talk to you normally in this session.
- Your completed turns are forwarded to the judge automatically. Do not rely on the human to type `continue` if work remains.
- If you think the task is done, make sure the finish criteria and verification commands really pass first.
- Prefer continuing work over asking whether you should proceed.
- Never ask `would you like me to continue`, `should I proceed`, `if you want, I can`, or `let me know if you want` when the next local step is discoverable from repo state.
- Treat human messages like `continue`, `gogogo`, `yes continue`, `go ahead`, and `proceed` as evidence that you previously stopped too early. Do not force the human to repeat those nudges.
- Do not use `done` or `complete` language unless the whole task is actually finished, not just one sub-slice.
- When you finish a turn, write a concise, factual assistant message because the judge will review that message.

Finish criteria:
{finish_contract_markdown}

Current TODO:
{todo_markdown}
"""


def _build_worker_shower_request_prompt(
    *,
    handoff_path: Path,
    latest_judge_instructions: str,
) -> str:
    return f"""Shower mode triggered.

You are about to be rebooted into a fresh worker session to keep context clean.

Required action:
- Write a concise but complete handoff summary to `{handoff_path}` right now.
- Use a shell command to write the file.
- After the file is written, stop and wait. Do not continue implementing work in this old session.
- Prefer a single write command such as:
  `cat > {handoff_path} <<'EOF'`
  `...summary here...`
  `EOF`
- Do not only answer in chat. The file at `{handoff_path}` is the contract.

The handoff summary must include:
- task status
- completed work
- files changed
- verification already run and outcomes
- remaining work
- real blockers or risks
- exact next step
- latest judge instructions

Latest judge instructions:
{latest_judge_instructions or "Continue the task and keep the board honest."}
"""


def _build_interactive_worker_standby_prompt() -> str:
    return """You are the interactive worker.

Standby rules:
- Do not start working yet.
- Wait for direct user instructions in this session.
- Do not ask what to do next.
- Do not claim progress or completion before the user gives you a task.
- Once the user gives a real task, start working normally.
- After the user gives a real task, read `.plan/TODO.md`, `.plan/FINISH_CRITERIA.md`, and `.plan/WORK_LOCK` if present and continue from there.
- After you have a real task, your completed turns are forwarded to the judge automatically.
"""


def _build_interactive_judge_standby_prompt() -> str:
    return """You are the interactive judge.

Standby rules:
- Do not start evaluating anything yet.
- Wait for injected evaluation requests from the watcher.
- This pane is also available for direct human interaction.
- The user may also talk to you directly in this session.
- Your job is only to decide whether the worker is REALLY finished for now.
- Keep `.plan/JUDGE_TODO.md` tiny and limited to verdict notes only.
- Do not act like a second worker.
"""


def _build_interactive_judge_prompt(
    task: str,
    finish_contract_markdown: str,
    todo_markdown: str,
    judge_todo_markdown: str,
) -> str:
    return f"""You are the interactive judge.

Primary job:
- decide whether the worker is REALLY finished for now

Scope rules:
- you are not a second worker
- do not expand scope
- do not plan product work
- do not ask the human to say `continue`

Allowed outputs:
- `continue`
- `complete`
- `blocked`

Judge scratchpad:
- use `.plan/JUDGE_TODO.md` only for tiny completion-evaluation notes
- keep it short: evidence, missing proof, real blockers, next verdict check

Evaluation rules:
- distrust worker `done` claims by default
- reject `done`, `complete`, `all set`, `if you want, I can`, `would you like me to continue`, and `should I proceed` when real work still remains
- prefer `continue` over `complete` when uncertain
- choose `blocked` only for real human-required blockers

The watcher will inject evaluation requests into this session.
When it does, write the requested JSON decision to the requested file path before doing anything else.
Human interaction is still allowed, but watcher requests take priority.

Task:
{task}

Finish criteria:
{finish_contract_markdown}

Current TODO:
{todo_markdown}

Current JUDGE_TODO:
{judge_todo_markdown}
"""


def _build_judge_request_prompt(
    *,
    task: str,
    finish_contract_markdown: str,
    deterministic_report: DeterministicReport,
    worker_turn_summary: str,
    repo_context: dict[str, str],
    output_path: Path,
) -> str:
    deterministic_json = json.dumps(deterministic_report.to_dict(), indent=2)
    output_path_text = str(output_path)
    git_status = _clip_text(repo_context["status"] or "(clean)", max_lines=40, max_chars=2500)
    git_diff_stat = _clip_text(repo_context["diff_stat"] or "(no diff stat)", max_lines=30, max_chars=2000)
    git_diff_names = _clip_text(repo_context["diff_names"] or "(no changed files)", max_lines=40, max_chars=2500)
    return f"""Judge evaluation request.

Your only job here is to decide whether the worker is REALLY finished for now.

Rules:
- do not act like a second worker
- do not expand scope
- prefer `continue` over `complete` when uncertain
- `blocked` is only for real human-required blockers
- if deterministic checks fail, completion should normally be rejected

Task:
{task}

Finish criteria:
{finish_contract_markdown}

Deterministic report:
{deterministic_json}

Worker turn summary:
{worker_turn_summary}

Git status:
{git_status}

Git diff --stat:
{git_diff_stat}

Git diff --name-only:
{git_diff_names}

Required action:
1. Optionally update `.plan/JUDGE_TODO.md` with tiny scratchpad notes.
2. Write EXACT JSON to `{output_path_text}`.
3. The JSON must be a single object with keys:
   - `decision`: `continue` | `complete` | `blocked`
   - `summary`: short string
   - `reasons`: string array
   - `instructions_for_worker`: short direct instruction
   - `missing_checks`: string array

Use a shell command to write the JSON file. Do not ask the human anything.
"""


def _build_auto_judge_subprocess_prompt(*, request_path: Path, output_path: Path) -> str:
    return (
        f"Read {request_path} and follow it exactly.\n"
        f"Write the JSON decision to {output_path}.\n"
        "Do not ask the human anything.\n"
    )


def _build_rebooted_worker_prompt(
    *,
    task: str,
    finish_contract_markdown: str,
    todo_markdown: str,
    handoff_summary: str,
    latest_judge_instructions: str,
) -> str:
    return f"""You are the interactive worker in a fresh rebooted session.

Task:
{task}

Fresh-session rules:
- This session was rebooted by shower mode to keep context clean.
- Read and obey `.plan/TODO.md`, `.plan/FINISH_CRITERIA.md`, and `.plan/WORK_LOCK` if present.
- Ignore `.plan/JUDGE_TODO.md`; that file belongs to the judge.
- Continue from the handoff summary below instead of redoing the entire investigation.
- Never ask `would you like me to continue`.
- Treat human messages like `continue`, `gogogo`, `yes continue` as proof that work should have continued automatically.
- Do not stop while real work remains.
- Your completed turns are forwarded to the judge automatically, so keep end-of-turn summaries concise and factual.

Finish criteria:
{finish_contract_markdown}

Current TODO:
{todo_markdown}

Worker handoff summary:
{handoff_summary}

Latest judge instructions before shower:
{latest_judge_instructions or "Continue working."}
"""


def _ensure_plan_files(repo_root: Path, task: str | None) -> tuple[FinishContract, str]:
    finish_path = repo_root / ".plan" / "FINISH_CRITERIA.md"
    todo_path = repo_root / ".plan" / "TODO.md"
    if not finish_path.exists() or not todo_path.exists():
        init_plan(repo_root, task or PLACEHOLDER_TASK)
    contract, raw_markdown = load_finish_contract(repo_root)
    if task:
        contract.task = task
    return contract, raw_markdown


class CodexRunner:
    def __init__(
        self,
        repo_root: Path,
        *,
        task: str | None,
        worker_model: str | None,
        judge_model: str | None,
        max_rounds: int,
        keep_session: bool,
        codex_bin: str,
        tmux_bin: str,
        worker_sandbox: str,
        judge_sandbox: str,
        worker_approval_policy: str,
        judge_approval_policy: str,
        bypass_approvals_and_sandbox: bool,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.task_override = task
        self.worker_model = worker_model
        self.judge_model = judge_model
        self.max_rounds = max_rounds
        self.keep_session = keep_session
        self.codex_bin = codex_bin
        self.tmux_bin = tmux_bin
        self.worker_sandbox = worker_sandbox
        self.judge_sandbox = judge_sandbox
        self.worker_approval_policy = worker_approval_policy
        self.judge_approval_policy = judge_approval_policy
        self.bypass_approvals_and_sandbox = bypass_approvals_and_sandbox
        self.plan_dir = self.repo_root / ".plan"
        self.runtime_dir = self.plan_dir / "codex-runner"
        self.rounds_dir = self.runtime_dir / "rounds"
        self.state_path = self.runtime_dir / "state.json"

    def _load_or_init_contract(self) -> tuple[FinishContract, str]:
        return _ensure_plan_files(self.repo_root, self.task_override)

    def _load_todo_markdown(self) -> str:
        todo_path = self.plan_dir / "TODO.md"
        if not todo_path.exists():
            return "# TODO\n\nMissing `.plan/TODO.md`\n"
        return todo_path.read_text(encoding="utf-8")

    def _load_state(self, task: str, session_name: str, mode: str) -> RunnerState:
        if self.state_path.exists():
            payload = _read_json(self.state_path)
            return RunnerState(
                version=int(payload.get("version", 1)),
                status=str(payload.get("status", "idle")),
                task=str(payload.get("task", task)),
                session_name=str(payload.get("session_name", session_name)),
                worker_session_id=payload.get("worker_session_id"),
                current_round=int(payload.get("current_round", 0)),
                started_at=str(payload.get("started_at", _now_iso())),
                updated_at=str(payload.get("updated_at", _now_iso())),
                rounds=list(payload.get("rounds", [])),
                mode=str(payload.get("mode", mode)),
                worker_pane=payload.get("worker_pane"),
                judge_pane=payload.get("judge_pane"),
                judge_session_id=payload.get("judge_session_id"),
                watcher_pid=payload.get("watcher_pid"),
                window_target=payload.get("window_target"),
                cleanup_scope=payload.get("cleanup_scope"),
                worker_reboots=int(payload.get("worker_reboots", 0)),
                standby=bool(payload.get("standby", False)),
                started_without_task=bool(payload.get("started_without_task", False)),
                awaiting_worker_change_after_complete=bool(payload.get("awaiting_worker_change_after_complete", False)),
                completion_latch_hash=payload.get("completion_latch_hash"),
                startup_turn_consumed=bool(payload.get("startup_turn_consumed", False)),
                worker_event_offset=int(payload.get("worker_event_offset", 0)),
            )
        now = _now_iso()
        return RunnerState(status="running", task=task, session_name=session_name, started_at=now, updated_at=now, mode=mode)

    def _save_state(self, state: RunnerState) -> None:
        state.updated_at = _now_iso()
        _write_json(self.state_path, asdict(state))

    def _write_schema(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _build_exec_script(
        self,
        *,
        command: list[str],
        prompt_path: Path,
        console_log_path: Path,
        exit_code_path: Path,
        working_dir: Path,
    ) -> str:
        return "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -o pipefail",
                f"cd {shlex.quote(str(working_dir))}",
                f"rm -f {shlex.quote(str(exit_code_path))}",
                f"{_shell_join(command)} < {shlex.quote(str(prompt_path))} 2>&1 | tee {shlex.quote(str(console_log_path))}",
                'rc=${PIPESTATUS[0]}',
                f'printf "%s\\n" "$rc" > {shlex.quote(str(exit_code_path))}',
            ]
        ) + "\n"

    def _wait_for_exit_code(self, path: Path, *, timeout_seconds: int = 7200) -> int:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if path.exists():
                return int(path.read_text(encoding="utf-8").strip())
            time.sleep(0.5)
        raise TimeoutError(f"Timed out waiting for {path}")

    def run(self) -> int:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.rounds_dir.mkdir(parents=True, exist_ok=True)
        contract, finish_markdown = self._load_or_init_contract()
        task = contract.task
        slug = re.sub(r"[^a-z0-9]+", "-", self.repo_root.name.lower()).strip("-") or "repo"
        session_name = f"codex-runner-{slug}-{_session_suffix()}"
        state = self._load_state(task, session_name, "batch")
        tmux = TmuxSession(state.session_name, tmux_bin=self.tmux_bin, remain_on_exit="on")
        layout = tmux.create()

        print(f"tmux session: {layout.session_name}")
        print(f"worker pane: {layout.worker_pane}")
        print(f"judge pane: {layout.judge_pane}")

        judge_feedback = ""
        state.status = "running"
        state.mode = "batch"
        state.worker_pane = layout.worker_pane
        state.judge_pane = layout.judge_pane
        state.window_target = layout.window_target
        state.cleanup_scope = layout.cleanup_scope
        self._save_state(state)

        try:
            for round_number in range(state.current_round + 1, self.max_rounds + 1):
                state.current_round = round_number
                round_dir = self.rounds_dir / f"round-{round_number:03d}"
                round_dir.mkdir(parents=True, exist_ok=True)

                worker_prompt_path = round_dir / "worker-prompt.txt"
                worker_schema_path = round_dir / "worker-output-schema.json"
                worker_last_path = round_dir / "worker-last.json"
                worker_console_log = round_dir / "worker-console.log"
                worker_exit_path = round_dir / "worker-exit-code.txt"

                worker_prompt_path.write_text(
                    _build_worker_prompt(
                        task=task,
                        finish_contract_markdown=finish_markdown,
                        todo_markdown=self._load_todo_markdown(),
                        judge_feedback=judge_feedback,
                        round_number=round_number,
                    ),
                    encoding="utf-8",
                )
                self._write_schema(worker_schema_path, _worker_schema())

                worker_command = [self.codex_bin, "exec", *_base_codex_args(sandbox=self.worker_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.worker_approval_policy)]
                if state.worker_session_id:
                    worker_command = [
                        self.codex_bin,
                        "exec",
                        "resume",
                        *(
                            ["--dangerously-bypass-approvals-and-sandbox"]
                            if self.bypass_approvals_and_sandbox
                            else ["-a", self.worker_approval_policy]
                        ),
                        state.worker_session_id,
                    ]
                else:
                    worker_command.extend(["-C", str(self.repo_root)])
                if self.worker_model:
                    worker_command.extend(["-m", self.worker_model])
                worker_command.extend([
                    "--output-schema",
                    str(worker_schema_path),
                    "-o",
                    str(worker_last_path),
                    "-",
                ])

                worker_script_path = round_dir / "worker-command.sh"
                worker_script_path.write_text(
                    self._build_exec_script(
                        command=worker_command,
                        prompt_path=worker_prompt_path,
                        console_log_path=worker_console_log,
                        exit_code_path=worker_exit_path,
                        working_dir=self.repo_root,
                    ),
                    encoding="utf-8",
                )
                worker_script_path.chmod(0o755)
                tmux.run_script(layout.worker_pane, str(worker_script_path))
                worker_exit_code = self._wait_for_exit_code(worker_exit_path)
                if not state.worker_session_id:
                    state.worker_session_id = _extract_session_id(worker_console_log)

                worker_result = _load_structured_output(worker_last_path, WorkerResult)
                deterministic_report = run_deterministic_checks(self.repo_root, contract)
                _write_json(round_dir / "deterministic-report.json", deterministic_report.to_dict())
                repo_context = _repo_context(self.repo_root)

                judge_prompt_path = round_dir / "judge-prompt.txt"
                judge_schema_path = round_dir / "judge-output-schema.json"
                judge_last_path = round_dir / "judge-last.json"
                judge_console_log = round_dir / "judge-console.log"
                judge_exit_path = round_dir / "judge-exit-code.txt"

                judge_prompt_path.write_text(
                    _build_judge_prompt(
                        task=task,
                        finish_contract_markdown=finish_markdown,
                        deterministic_report=deterministic_report,
                        worker_result=worker_result,
                        worker_exit_code=worker_exit_code,
                        worker_console_tail=_tail(worker_console_log),
                        repo_context=repo_context,
                        round_number=round_number,
                    ),
                    encoding="utf-8",
                )
                self._write_schema(judge_schema_path, _judge_schema())

                judge_command = [self.codex_bin, "exec", *_base_codex_args(sandbox=self.judge_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.judge_approval_policy), "--ephemeral", "-C", str(self.repo_root)]
                if self.judge_model:
                    judge_command.extend(["-m", self.judge_model])
                judge_command.extend([
                    "--output-schema",
                    str(judge_schema_path),
                    "-o",
                    str(judge_last_path),
                    "-",
                ])

                judge_script_path = round_dir / "judge-command.sh"
                judge_script_path.write_text(
                    self._build_exec_script(
                        command=judge_command,
                        prompt_path=judge_prompt_path,
                        console_log_path=judge_console_log,
                        exit_code_path=judge_exit_path,
                        working_dir=self.repo_root,
                    ),
                    encoding="utf-8",
                )
                judge_script_path.chmod(0o755)
                tmux.run_script(layout.judge_pane, str(judge_script_path))
                judge_exit_code = self._wait_for_exit_code(judge_exit_path)
                judge_decision = _load_structured_output(judge_last_path, JudgeDecision)

                if judge_decision.decision == "complete" and not deterministic_report.passed:
                    judge_decision = JudgeDecision(
                        decision="continue",
                        summary="Judge said complete but deterministic finish gates still fail.",
                        reasons=["Deterministic finish gates failed."],
                        instructions_for_worker="The deterministic finish gates still fail. Fix the reported failures and do not claim completion yet.",
                        missing_checks=[
                            check.name for check in deterministic_report.checks if not check.passed and check.required
                        ],
                    )

                record = RoundRecord(
                    round_number=round_number,
                    worker_exit_code=worker_exit_code,
                    judge_exit_code=judge_exit_code,
                    worker_session_id=state.worker_session_id,
                    worker_result=asdict(worker_result),
                    judge_decision=asdict(judge_decision),
                    deterministic_report=deterministic_report.to_dict(),
                )
                state.rounds.append(asdict(record))
                self._save_state(state)

                print(f"round {round_number}: worker={worker_result.status} judge={judge_decision.decision}")

                if judge_decision.decision == "complete" and deterministic_report.passed:
                    state.status = "complete"
                    self._save_state(state)
                    print("Runner complete: finish criteria passed and judge approved completion.")
                    return 0

                if judge_decision.decision == "blocked":
                    state.status = "blocked"
                    self._save_state(state)
                    print("Runner blocked: judge requested human intervention.")
                    return 2

                judge_feedback = judge_decision.instructions_for_worker

            state.status = "failed"
            self._save_state(state)
            print(f"Runner failed: reached max rounds ({self.max_rounds}) without completion.")
            return 1
        finally:
            if not self.keep_session:
                tmux.kill(layout)


class InteractiveRunner:
    def __init__(
        self,
        repo_root: Path,
        *,
        task: str | None,
        worker_model: str | None,
        judge_model: str | None,
        codex_bin: str,
        tmux_bin: str,
        worker_sandbox: str,
        judge_sandbox: str,
        worker_approval_policy: str,
        judge_approval_policy: str,
        bypass_approvals_and_sandbox: bool,
        attach: bool,
        idle_seconds: int,
        poll_seconds: int,
        shower_enabled: bool,
        shower_interval: int,
        shower_timeout_seconds: int,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.task_override = task
        self.worker_model = worker_model
        self.judge_model = judge_model
        self.codex_bin = codex_bin
        self.tmux_bin = tmux_bin
        self.worker_sandbox = worker_sandbox
        self.judge_sandbox = judge_sandbox
        self.worker_approval_policy = worker_approval_policy
        self.judge_approval_policy = judge_approval_policy
        self.bypass_approvals_and_sandbox = bypass_approvals_and_sandbox
        self.attach = attach
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self.shower_enabled = shower_enabled
        self.shower_interval = max(1, shower_interval)
        self.shower_timeout_seconds = max(1, shower_timeout_seconds)
        self.plan_dir = self.repo_root / ".plan"
        self.runtime_dir = self.plan_dir / "codex-runner"
        self.logs_dir = self.runtime_dir / "interactive"
        self.state_path = self.runtime_dir / "state.json"

    def _worker_turn_events_path(self) -> Path:
        return self.logs_dir / "worker-turn-events.jsonl"

    def _notify_argv(self, *, role: str) -> list[str]:
        return _notify_hook_argv(role=role, output_path=self._worker_turn_events_path())

    def _interactive_command(
        self,
        *,
        prompt: str,
        model: str | None,
        sandbox: str,
        approval_policy: str,
        session_id: str | None = None,
        notify_argv: list[str] | None = None,
    ) -> str:
        if session_id:
            parts = [
                self.codex_bin,
                "resume",
                *_base_codex_args(sandbox=sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=approval_policy),
                "-C",
                str(self.repo_root),
                "--no-alt-screen",
            ]
            if model:
                parts.extend(["-m", model])
            if notify_argv:
                parts.extend(["-c", f"notify={json.dumps(notify_argv)}"])
            parts.extend([session_id, prompt])
            return _shell_join(parts)

        parts = [
            self.codex_bin,
            *_base_codex_args(sandbox=sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=approval_policy),
            "-C",
            str(self.repo_root),
            "--no-alt-screen",
        ]
        if model:
            parts.extend(["-m", model])
        if notify_argv:
            parts.extend(["-c", f"notify={json.dumps(notify_argv)}"])
        parts.append(prompt)
        return _shell_join(parts)

    def start(self) -> int:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        contract, finish_markdown = _ensure_plan_files(self.repo_root, self.task_override)
        task = contract.task
        slug = re.sub(r"[^a-z0-9]+", "-", self.repo_root.name.lower()).strip("-") or "repo"
        session_name = f"codex-runner-{slug}-{_session_suffix()}"
        state = RunnerState(
            status="running",
            task=task,
            session_name=session_name,
            started_at=_now_iso(),
            updated_at=_now_iso(),
            mode="interactive",
            standby=self.task_override is None,
            started_without_task=self.task_override is None,
        )
        tmux = TmuxSession(session_name, tmux_bin=self.tmux_bin, remain_on_exit="failed")
        layout = tmux.create()
        state.worker_pane = layout.worker_pane
        state.judge_pane = layout.judge_pane
        state.window_target = layout.window_target
        state.cleanup_scope = layout.cleanup_scope
        self._save_state(state)

        worker_log = self.logs_dir / "worker-pane.log"
        judge_log = self.logs_dir / "judge-pane.log"
        self._worker_turn_events_path().write_text("", encoding="utf-8")
        tmux.pipe_pane(layout.worker_pane, worker_log)
        tmux.pipe_pane(layout.judge_pane, judge_log)

        todo_markdown = self.plan_dir.joinpath("TODO.md").read_text(encoding="utf-8")
        standby_mode = self.task_override is None
        worker_prompt = _build_interactive_worker_standby_prompt() if standby_mode else _build_interactive_worker_prompt(task, finish_markdown, todo_markdown)
        worker_script = self.logs_dir / "start-worker.sh"
        worker_script.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(self.repo_root))}\n"
            f"exec {self._interactive_command(prompt=worker_prompt, model=self.worker_model, sandbox=self.worker_sandbox, approval_policy=self.worker_approval_policy, notify_argv=self._notify_argv(role='worker'))}\n",
            encoding="utf-8",
        )
        worker_script.chmod(0o755)
        tmux.run_script(layout.worker_pane, str(worker_script))
        _auto_accept_trust_prompt(tmux, layout.worker_pane, timeout_seconds=20)

        judge_prompt = _build_interactive_judge_standby_prompt()
        judge_script = self.logs_dir / "start-judge.sh"
        judge_script.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(self.repo_root))}\n"
            f"exec {self._interactive_command(prompt=judge_prompt, model=self.judge_model, sandbox=self.judge_sandbox, approval_policy=self.judge_approval_policy)}\n",
            encoding="utf-8",
        )
        judge_script.chmod(0o755)
        tmux.run_script(layout.judge_pane, str(judge_script))
        _auto_accept_trust_prompt(tmux, layout.judge_pane, timeout_seconds=20)

        watcher_log = self.logs_dir / "watcher.log"
        watcher_command = [
            sys.executable,
            str(Path(__file__).with_name("watch_entry.py")),
            "watch",
            str(self.repo_root),
            "--session-name",
            layout.session_name,
            "--worker-pane",
            layout.worker_pane,
            "--judge-pane",
            layout.judge_pane,
            "--codex-bin",
            self.codex_bin,
            "--tmux-bin",
            self.tmux_bin,
            "--judge-sandbox",
            self.judge_sandbox,
            "--judge-approval-policy",
            self.judge_approval_policy,
            "--worker-sandbox",
            self.worker_sandbox,
            "--worker-approval-policy",
            self.worker_approval_policy,
            "--idle-seconds",
            str(self.idle_seconds),
            "--poll-seconds",
            str(self.poll_seconds),
            "--shower-interval",
            str(self.shower_interval),
            "--shower-timeout-seconds",
            str(self.shower_timeout_seconds),
        ]
        if self.shower_enabled:
            watcher_command.append("--shower-enabled")
        if self.judge_model:
            watcher_command.extend(["--judge-model", self.judge_model])
        if self.worker_model:
            watcher_command.extend(["--worker-model", self.worker_model])
        if self.bypass_approvals_and_sandbox:
            watcher_command.append("--dangerous")
        env = os.environ.copy()
        watcher_log_handle = watcher_log.open("a", encoding="utf-8")
        watcher = subprocess.Popen(
            watcher_command,
            cwd=self.repo_root,
            env=env,
            stdout=watcher_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state.watcher_pid = watcher.pid
        self._save_state(state)

        print(f"tmux session: {layout.session_name}")
        print(f"worker pane: {layout.worker_pane}")
        print(f"judge pane: {layout.judge_pane}")
        print(f"repo: {self.repo_root}")
        print("interactive worker and judge are live; you can talk to both panes normally")
        print("watcher evaluations use the visible judge pane; you can see automatic judge requests there")
        if standby_mode:
            print("no --task was provided, so the worker is in standby waiting for your instructions")

        should_attach = self.attach and not os.environ.get("TMUX")
        if self.attach and os.environ.get("TMUX"):
            tmux.attach_or_switch(layout, force_inside_tmux=True)
        elif should_attach:
            tmux.attach_or_switch(layout)
        return 0

    def _save_state(self, state: RunnerState) -> None:
        state.updated_at = _now_iso()
        _write_json(self.state_path, asdict(state))


class JudgeWatcher:
    def __init__(
        self,
        repo_root: Path,
        *,
        session_name: str,
        worker_pane: str,
        judge_pane: str,
        codex_bin: str,
        tmux_bin: str,
        judge_model: str | None,
        judge_sandbox: str,
        judge_approval_policy: str,
        worker_model: str | None,
        worker_sandbox: str,
        worker_approval_policy: str,
        bypass_approvals_and_sandbox: bool,
        idle_seconds: int,
        poll_seconds: int,
        shower_enabled: bool,
        shower_interval: int,
        shower_timeout_seconds: int,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.session_name = session_name
        self.worker_pane = worker_pane
        self.judge_pane = judge_pane
        self.codex_bin = codex_bin
        self.tmux_bin = tmux_bin
        self.judge_model = judge_model
        self.judge_sandbox = judge_sandbox
        self.judge_approval_policy = judge_approval_policy
        self.worker_model = worker_model
        self.worker_sandbox = worker_sandbox
        self.worker_approval_policy = worker_approval_policy
        self.bypass_approvals_and_sandbox = bypass_approvals_and_sandbox
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self.shower_enabled = shower_enabled
        self.shower_interval = max(1, shower_interval)
        self.shower_timeout_seconds = max(1, shower_timeout_seconds)
        self.plan_dir = self.repo_root / ".plan"
        self.runtime_dir = self.plan_dir / "codex-runner"
        self.logs_dir = self.runtime_dir / "interactive"
        self.state_path = self.runtime_dir / "state.json"
        self.tmux = TmuxSession(session_name, tmux_bin=tmux_bin)
        self.last_hash = ""
        self.last_change_at = time.time()
        self.last_signature = ""
        self.last_action_at = 0.0

    def _worker_log_path(self) -> Path:
        return self.logs_dir / "worker-pane.log"

    def _worker_turn_events_path(self) -> Path:
        return self.logs_dir / "worker-turn-events.jsonl"

    def _judge_log_path(self) -> Path:
        return self.logs_dir / "judge-pane.log"

    def _watcher_log_path(self) -> Path:
        return self.logs_dir / "watcher-events.jsonl"

    def _log_watcher_event(self, event: str, **payload: Any) -> None:
        _append_jsonl(self._watcher_log_path(), {"event": event, **payload})

    def _bootstrap_worker_pane(self) -> None:
        _auto_accept_trust_prompt(
            self.tmux,
            self.worker_pane,
            timeout_seconds=8,
            on_accept=lambda: self._log_watcher_event("worker_trust_prompt_accepted", worker_pane=self.worker_pane),
        )

    def _bootstrap_judge_pane(self) -> None:
        _auto_accept_trust_prompt(
            self.tmux,
            self.judge_pane,
            timeout_seconds=8,
            on_accept=lambda: self._log_watcher_event("judge_trust_prompt_accepted", judge_pane=self.judge_pane),
        )

    def _detect_worker_session_id(self) -> str | None:
        log_path = self._worker_log_path()
        if log_path.exists():
            session_id = _extract_session_id(log_path)
            if session_id:
                return session_id
        capture = self.tmux.capture_pane(self.worker_pane)
        return _extract_session_id_from_text(capture)

    def _detect_judge_session_id(self) -> str | None:
        log_path = self._judge_log_path()
        if log_path.exists():
            session_id = _extract_session_id(log_path)
            if session_id:
                return session_id
        capture = self.tmux.capture_pane(self.judge_pane)
        return _extract_session_id_from_text(capture)

    def _is_idle(self, pane_text: str) -> bool:
        current_hash = hashlib.sha256(pane_text.encode("utf-8", errors="ignore")).hexdigest()
        now = time.time()
        if current_hash != self.last_hash:
            self.last_hash = current_hash
            self.last_change_at = now
            return False
        return (now - self.last_change_at) >= self.idle_seconds

    def _signature(self, pane_text: str, deterministic_report: DeterministicReport) -> str:
        todo = (self.plan_dir / "TODO.md").read_text(encoding="utf-8") if (self.plan_dir / "TODO.md").exists() else ""
        status = _git_capture(self.repo_root, "status", "--short")
        base = json.dumps({
            "todo": todo,
            "status": status,
            "checks": deterministic_report.to_dict(),
            "pane": pane_text[-3000:],
        }, sort_keys=True)
        return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()

    def _new_worker_command(self, prompt: str) -> str:
        parts = [self.codex_bin, *_base_codex_args(sandbox=self.worker_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.worker_approval_policy), "-C", str(self.repo_root), "--no-alt-screen", "-c", f"notify={json.dumps(_notify_hook_argv(role='worker', output_path=self._worker_turn_events_path()))}"]
        if self.worker_model:
            parts.extend(["-m", self.worker_model])
        parts.append(prompt)
        return _shell_join(parts)

    def _resume_command(self, session_id: str, prompt: str) -> str:
        parts = [self.codex_bin, "resume", *_base_codex_args(sandbox=self.worker_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.worker_approval_policy), "-C", str(self.repo_root), "--no-alt-screen", "-c", f"notify={json.dumps(_notify_hook_argv(role='worker', output_path=self._worker_turn_events_path()))}"]
        if self.worker_model:
            parts.extend(["-m", self.worker_model])
        parts.extend([session_id, prompt])
        return _shell_join(parts)

    def _read_worker_turn_events_since(self, offset: int) -> tuple[int, list[dict[str, Any]]]:
        path = self._worker_turn_events_path()
        if not path.exists():
            return offset, []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        new_offset = len(lines)
        events: list[dict[str, Any]] = []
        for raw_line in lines[offset:]:
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("role") == "worker":
                events.append(payload)
        return new_offset, events

    def _judge_command(self, prompt: str, session_id: str | None) -> str:
        if session_id:
            parts = [
                self.codex_bin,
                "resume",
                *_base_codex_args(sandbox=self.judge_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.judge_approval_policy),
                "-C",
                str(self.repo_root),
                "--no-alt-screen",
            ]
            if self.judge_model:
                parts.extend(["-m", self.judge_model])
            parts.extend([session_id, prompt])
            return _shell_join(parts)

        parts = [
            self.codex_bin,
            *_base_codex_args(sandbox=self.judge_sandbox, bypass=self.bypass_approvals_and_sandbox, approval_policy=self.judge_approval_policy),
            "-C",
            str(self.repo_root),
            "--no-alt-screen",
        ]
        if self.judge_model:
            parts.extend(["-m", self.judge_model])
        parts.append(prompt)
        return _shell_join(parts)

    def _ensure_judge_session(self, state: RunnerState) -> str | None:
        current_command = self.tmux.pane_current_command(self.judge_pane).lower()
        detected_session_id = self._detect_judge_session_id()
        if detected_session_id and state.judge_session_id != detected_session_id:
            state.judge_session_id = detected_session_id
            self._save_state(state)
        if current_command not in SHELL_COMMANDS:
            return state.judge_session_id

        restart_prompt = (
            "Resume judge mode. Wait for evaluation requests. "
            "Your only job is to decide whether the worker is REALLY finished for now. "
            "Write requested JSON verdict files immediately when the watcher asks. "
            "Use .plan/JUDGE_TODO.md as a tiny scratchpad only."
        )
        script_path = self.logs_dir / f"restart-judge-{int(time.time())}.sh"
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(self.repo_root))}\n"
            f"exec {self._judge_command(restart_prompt, state.judge_session_id)}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.tmux.run_script(self.judge_pane, str(script_path))
        self.tmux.pipe_pane(self.judge_pane, self._judge_log_path())
        self._bootstrap_judge_pane()

        deadline = time.time() + 30
        while time.time() < deadline:
            detected_session_id = self._detect_judge_session_id()
            if detected_session_id:
                state.judge_session_id = detected_session_id
                self._save_state(state)
                return detected_session_id
            time.sleep(0.5)
        return state.judge_session_id

    def _run_judge(self, contract: FinishContract, finish_markdown: str, deterministic_report: DeterministicReport, worker_turn_summary: str, state: RunnerState) -> tuple[JudgeDecision, int]:
        round_dir = self.logs_dir / f"watch-{time.time_ns()}"
        round_dir.mkdir(parents=True, exist_ok=True)
        decision_path = round_dir / "decision.json"
        request_path = round_dir / "judge-request.md"
        judge_session_id = self._ensure_judge_session(state)
        request_prompt = _build_judge_request_prompt(
            task=contract.task,
            finish_contract_markdown=finish_markdown,
            deterministic_report=deterministic_report,
            worker_turn_summary=worker_turn_summary,
            repo_context=_repo_context(self.repo_root),
            output_path=decision_path,
        )
        request_path.write_text(request_prompt, encoding="utf-8")
        inject_text = (
            f"Read {request_path} and follow it now. "
            f"Write the JSON decision to {decision_path}. "
            "Do not ask the human anything."
        )
        self._log_watcher_event(
            "judge_request_built",
            judge_mode="interactive-pane",
            judge_session_id=judge_session_id,
            request_path=str(request_path),
            decision_path=str(decision_path),
            worker_pane=self.worker_pane,
            judge_pane=self.judge_pane,
        )
        self._log_watcher_event(
            "judge_request_started",
            judge_mode="interactive-pane",
            decision_path=str(decision_path),
        )
        self.tmux.send_keys(self.judge_pane, inject_text, press_enter=True)
        self._log_watcher_event(
            "judge_request_injected",
            judge_mode="interactive-pane",
            judge_session_id=judge_session_id,
            inject_text=inject_text,
            decision_path=str(decision_path),
        )

        deadline = time.time() + 900
        next_heartbeat_at = time.time() + 30
        while time.time() < deadline:
            if decision_path.exists():
                decision = _load_structured_output(decision_path, JudgeDecision)
                self._log_watcher_event(
                    "judge_decision_received",
                    decision_path=str(decision_path),
                    decision=asdict(decision),
                    judge_exit_code=0,
                )
                return decision, 0
            new_session_id = self._detect_judge_session_id()
            if new_session_id and state.judge_session_id != new_session_id:
                state.judge_session_id = new_session_id
                self._save_state(state)
            if time.time() >= next_heartbeat_at:
                self._log_watcher_event(
                    "judge_request_waiting",
                    judge_mode="interactive-pane",
                    decision_path=str(decision_path),
                    elapsed_seconds=int(900 - max(0, deadline - time.time())),
                    judge_session_id=state.judge_session_id,
                )
                next_heartbeat_at = time.time() + 30
            time.sleep(0.5)
        fallback = JudgeDecision.fallback(f"Judge did not produce {decision_path}")
        self._log_watcher_event(
            "judge_decision_timeout",
            decision_path=str(decision_path),
            judge_mode="interactive-pane",
            judge_session_id=state.judge_session_id,
            judge_pane_tail=self.tmux.capture_pane(self.judge_pane, lines=80)[-4000:],
        )
        return fallback, 1

    def _load_state(self) -> RunnerState:
        payload = _read_json(self.state_path) if self.state_path.exists() else {}
        return RunnerState(
            version=int(payload.get("version", 1)),
            status=str(payload.get("status", "running")),
            task=str(payload.get("task", PLACEHOLDER_TASK)),
            session_name=str(payload.get("session_name", self.session_name)),
            worker_session_id=payload.get("worker_session_id"),
            current_round=int(payload.get("current_round", 0)),
            started_at=str(payload.get("started_at", _now_iso())),
            updated_at=str(payload.get("updated_at", _now_iso())),
            rounds=list(payload.get("rounds", [])),
            mode=str(payload.get("mode", "interactive")),
            worker_pane=payload.get("worker_pane"),
            judge_pane=payload.get("judge_pane"),
            judge_session_id=payload.get("judge_session_id"),
            watcher_pid=payload.get("watcher_pid"),
            window_target=payload.get("window_target"),
            cleanup_scope=payload.get("cleanup_scope"),
            worker_reboots=int(payload.get("worker_reboots", 0)),
            standby=bool(payload.get("standby", False)),
            started_without_task=bool(payload.get("started_without_task", False)),
            awaiting_worker_change_after_complete=bool(payload.get("awaiting_worker_change_after_complete", False)),
            completion_latch_hash=payload.get("completion_latch_hash"),
            startup_turn_consumed=bool(payload.get("startup_turn_consumed", False)),
            worker_event_offset=int(payload.get("worker_event_offset", 0)),
        )

    def _save_state(self, state: RunnerState) -> None:
        state.updated_at = _now_iso()
        _write_json(self.state_path, asdict(state))

    def _send_continue(self, session_id: str | None, instructions: str, current_command: str) -> str:
        self._log_watcher_event(
            "worker_transfer_begin",
            transfer_kind="continue",
            worker_pane=self.worker_pane,
            worker_session_id=session_id,
            current_command=current_command,
            instructions=instructions,
        )
        if current_command.lower() in SHELL_COMMANDS:
            command = self.codex_bin
            method = "new_worker"
            if session_id:
                command = self._resume_command(session_id, instructions)
                method = "resume_worker"
            else:
                command = self._new_worker_command(instructions)
            script_path = self.logs_dir / f"resume-worker-{int(time.time())}.sh"
            script_path.write_text(
                "#!/usr/bin/env bash\n"
                f"cd {shlex.quote(str(self.repo_root))}\n"
                f"exec {command}\n",
                encoding="utf-8",
            )
            script_path.chmod(0o755)
            self.tmux.run_script(self.worker_pane, str(script_path))
            self.tmux.pipe_pane(self.worker_pane, self._worker_log_path())
            self._bootstrap_worker_pane()
            self._log_watcher_event(
                "worker_transfer_delivered",
                transfer_kind="continue",
                delivery_method=method,
                worker_pane=self.worker_pane,
                script_path=str(script_path),
                instructions=instructions,
            )
            return method
        self.tmux.send_keys(self.worker_pane, instructions, press_enter=True)
        self._log_watcher_event(
            "worker_transfer_delivered",
            transfer_kind="continue",
            delivery_method="tmux_send_keys",
            worker_pane=self.worker_pane,
            instructions=instructions,
        )
        return "tmux_send_keys"

    def _should_shower(self, *, round_number: int, decision: JudgeDecision) -> bool:
        return (
            self.shower_enabled
            and decision.decision == "continue"
            and self.shower_interval > 0
            and round_number % self.shower_interval == 0
        )

    def _wait_for_file(self, path: Path, *, timeout_seconds: int) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if path.exists() and path.read_text(encoding="utf-8", errors="replace").strip():
                return True
            time.sleep(0.5)
        return False

    def _wait_for_worker_handoff(
        self,
        path: Path,
        *,
        timeout_seconds: int,
        start_offset: int,
    ) -> tuple[bool, bool]:
        deadline = time.time() + timeout_seconds
        saw_turn_complete = False
        offset = start_offset
        while time.time() < deadline:
            if path.exists() and path.read_text(encoding="utf-8", errors="replace").strip():
                return True, saw_turn_complete
            offset, worker_events = self._read_worker_turn_events_since(offset)
            if worker_events:
                saw_turn_complete = True
                return False, True
            time.sleep(0.5)
        return False, saw_turn_complete

    def _fallback_handoff_summary(self, *, pane_text: str, instructions: str) -> str:
        tail = pane_text[-4000:].strip() or "(worker pane was empty)"
        return (
            "Fallback handoff summary generated by codex-runner because the worker did not produce a shower handoff file.\n\n"
            f"Latest judge instructions:\n{instructions or 'Continue working.'}\n\n"
            f"Recent worker pane tail:\n{tail}\n"
        )

    def _restart_worker_from_handoff(
        self,
        *,
        handoff_summary: str,
        latest_judge_instructions: str,
        state: RunnerState,
    ) -> None:
        todo_markdown = self.plan_dir.joinpath("TODO.md").read_text(encoding="utf-8") if self.plan_dir.joinpath("TODO.md").exists() else "# TODO\n"
        contract, finish_markdown = _ensure_plan_files(self.repo_root, None)
        prompt = _build_rebooted_worker_prompt(
            task=contract.task,
            finish_contract_markdown=finish_markdown,
            todo_markdown=todo_markdown,
            handoff_summary=handoff_summary,
            latest_judge_instructions=latest_judge_instructions,
        )
        script_path = self.logs_dir / f"reboot-worker-{int(time.time())}.sh"
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(self.repo_root))}\n"
            f"exec {self._new_worker_command(prompt)}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.tmux.run_script(self.worker_pane, str(script_path))
        self.tmux.pipe_pane(self.worker_pane, self._worker_log_path())
        self._bootstrap_worker_pane()
        state.worker_session_id = None
        state.worker_reboots += 1
        self.last_hash = ""
        self.last_change_at = time.time()

    def _perform_shower(
        self,
        *,
        state: RunnerState,
        current_command: str,
        pane_text: str,
        instructions: str,
        round_number: int,
    ) -> str:
        shower_dir = self.logs_dir / f"shower-{time.time_ns()}"
        shower_dir.mkdir(parents=True, exist_ok=True)
        handoff_path = shower_dir / "worker-handoff.md"
        request_path = shower_dir / "worker-shower-request.txt"
        request = _build_worker_shower_request_prompt(
            handoff_path=handoff_path,
            latest_judge_instructions=instructions,
        )
        request_path.write_text(request, encoding="utf-8")
        inject_text = (
            f"Read {request_path} and follow it now. "
            f"Write the handoff summary to {handoff_path}. "
            "After the file is written, stop and wait."
        )
        self._log_watcher_event(
            "worker_shower_started",
            round_number=round_number,
            handoff_path=str(handoff_path),
            current_command=current_command,
            instructions=instructions,
        )

        if current_command.lower() not in SHELL_COMMANDS:
            start_offset = len(
                self._worker_turn_events_path().read_text(encoding="utf-8", errors="replace").splitlines()
            ) if self._worker_turn_events_path().exists() else 0
            self.tmux.send_keys(self.worker_pane, inject_text, press_enter=True)
            wrote_handoff, saw_turn_complete = self._wait_for_worker_handoff(
                handoff_path,
                timeout_seconds=self.shower_timeout_seconds,
                start_offset=start_offset,
            )
        else:
            wrote_handoff = False
            saw_turn_complete = False

        handoff_summary = (
            handoff_path.read_text(encoding="utf-8", errors="replace").strip()
            if wrote_handoff
            else self._fallback_handoff_summary(pane_text=pane_text, instructions=instructions)
        )
        if not wrote_handoff:
            self._log_watcher_event(
                "worker_shower_fallback",
                round_number=round_number,
                handoff_path=str(handoff_path),
                saw_turn_complete=saw_turn_complete,
            )
        self._restart_worker_from_handoff(
            handoff_summary=handoff_summary,
            latest_judge_instructions=instructions,
            state=state,
        )
        self._log_watcher_event(
            "worker_shower_completed",
            round_number=round_number,
            handoff_path=str(handoff_path),
            worker_reboots=state.worker_reboots,
            handoff_preview=handoff_summary[:1000],
        )
        return handoff_summary

    def _cleanup_owned_session_on_terminal(self, state: RunnerState) -> None:
        if state.cleanup_scope != "session" or not state.session_name:
            return
        layout = type(
            "Layout",
            (),
            {
                "cleanup_scope": "session",
                "window_target": state.window_target or "",
                "session_name": state.session_name,
            },
        )
        try:
            self.tmux.kill(layout)
        except Exception:
            pass

    def _reset_worker_to_standby(self, state: RunnerState) -> None:
        standby_prompt = _build_interactive_worker_standby_prompt()
        script_path = self.logs_dir / f"standby-worker-{int(time.time())}.sh"
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex.quote(str(self.repo_root))}\n"
            f"exec {self._new_worker_command(standby_prompt)}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.tmux.run_script(self.worker_pane, str(script_path))
        self.tmux.pipe_pane(self.worker_pane, self._worker_log_path())
        self._bootstrap_worker_pane()
        state.worker_session_id = None
        state.standby = True
        state.started_without_task = True
        state.status = "running"
        state.awaiting_worker_change_after_complete = False
        state.completion_latch_hash = None
        state.startup_turn_consumed = False
        self._log_watcher_event(
            "worker_reset_to_standby",
            worker_pane=self.worker_pane,
            script_path=str(script_path),
        )
        self.last_hash = ""
        self.last_signature = ""
        self.last_change_at = time.time()
        self.last_action_at = 0.0

    def run(self) -> int:
        contract, finish_markdown = _ensure_plan_files(self.repo_root, None)
        state = self._load_state()
        self._bootstrap_worker_pane()
        self._bootstrap_judge_pane()
        try:
            while True:
                session_id = self._detect_worker_session_id()
                if session_id and state.worker_session_id != session_id:
                    state.worker_session_id = session_id
                    self._save_state(state)

                new_offset, worker_events = self._read_worker_turn_events_since(state.worker_event_offset)
                if new_offset != state.worker_event_offset:
                    state.worker_event_offset = new_offset
                    self._save_state(state)

                if not worker_events:
                    time.sleep(self.poll_seconds)
                    continue

                for worker_event in worker_events:
                    pane_text = self.tmux.capture_pane(self.worker_pane, lines=200)
                    current_command = self.tmux.pane_current_command(self.worker_pane)
                    turn_id = str(worker_event.get("turn_id") or "")
                    if state.standby:
                        if not state.startup_turn_consumed:
                            state.startup_turn_consumed = True
                            self._save_state(state)
                            self._log_watcher_event(
                                "worker_startup_turn_ignored",
                                worker_pane=self.worker_pane,
                                turn_id=turn_id,
                            )
                            continue
                        state.standby = False
                        state.started_without_task = False
                        self._save_state(state)
                        self._log_watcher_event(
                            "worker_activated",
                            worker_pane=self.worker_pane,
                            turn_id=turn_id,
                        )

                    include_verify_commands = _worker_maybe_claims_completion(worker_event)
                    deterministic_report = run_deterministic_checks(
                        self.repo_root,
                        contract,
                        include_verify_commands=include_verify_commands,
                    )
                    self._log_watcher_event(
                        "worker_turn_completed",
                        worker_pane=self.worker_pane,
                        worker_session_id=state.worker_session_id,
                        turn_id=turn_id,
                        deterministic_passed=deterministic_report.passed,
                        include_verify_commands=include_verify_commands,
                    )
                    decision, judge_exit = self._run_judge(
                        contract,
                        finish_markdown,
                        deterministic_report,
                        _worker_turn_summary_from_event(worker_event),
                        state,
                    )
                    state.current_round += 1
                    state.rounds.append(
                        asdict(
                            RoundRecord(
                                round_number=state.current_round,
                                worker_exit_code=0,
                                judge_exit_code=judge_exit,
                                worker_session_id=state.worker_session_id,
                                worker_result=asdict(WorkerResult.fallback("interactive worker under observation")),
                                judge_decision=asdict(decision),
                                deterministic_report=deterministic_report.to_dict(),
                            )
                        )
                    )
                    if decision.decision == "complete" and deterministic_report.passed:
                        state.status = "running"
                        self._save_state(state)
                        self._log_watcher_event(
                            "judge_complete_nonterminal",
                            round_number=state.current_round,
                            decision=asdict(decision),
                            worker_session_id=state.worker_session_id,
                            worker_pane=self.worker_pane,
                        )
                        print("judge: completion approved")
                        continue
                    if decision.decision == "blocked":
                        state.status = "blocked"
                        self._save_state(state)
                        self._log_watcher_event(
                            "judge_blocked_nonterminal",
                            round_number=state.current_round,
                            decision=asdict(decision),
                        )
                        print("judge: blocked")
                        continue
                    if decision.instructions_for_worker:
                        if self._should_shower(round_number=state.current_round, decision=decision):
                            self._perform_shower(
                                state=state,
                                current_command=current_command,
                                pane_text=pane_text,
                                instructions=decision.instructions_for_worker,
                                round_number=state.current_round,
                            )
                            self._log_watcher_event(
                                "worker_transfer_skipped_for_shower",
                                round_number=state.current_round,
                                decision=asdict(decision),
                            )
                            print(f"judge: continue -> shower reboot after round {state.current_round}")
                        else:
                            delivery_method = self._send_continue(session_id, decision.instructions_for_worker, current_command)
                            self._log_watcher_event(
                                "worker_transfer_complete",
                                round_number=state.current_round,
                                decision=asdict(decision),
                                delivery_method=delivery_method,
                                worker_session_id=session_id,
                            )
                            print(f"judge: continue -> {decision.instructions_for_worker}")
                    else:
                        self._log_watcher_event(
                            "worker_transfer_skipped_no_instructions",
                            round_number=state.current_round,
                            decision=asdict(decision),
                        )
                    self._save_state(state)
                time.sleep(self.poll_seconds)
        finally:
            latest_state = self._load_state()
            if latest_state.watcher_pid == os.getpid():
                latest_state.watcher_pid = None
                self._save_state(latest_state)


def read_runner_status(repo_root: Path) -> dict[str, Any]:
    state_path = repo_root / ".plan" / "codex-runner" / "state.json"
    if not state_path.exists():
        return {
            "status": "missing",
            "detail": f"No runner state at {state_path}",
        }
    return _read_json(state_path)


def stop_runner(repo_root: Path, *, tmux_bin: str = "tmux") -> int:
    state_path = repo_root / ".plan" / "codex-runner" / "state.json"
    if not state_path.exists():
        print(f"no runner state at {state_path}")
        return 1
    payload = _read_json(state_path)
    session_name = str(payload.get("session_name") or "")
    worker_pane = payload.get("worker_pane")
    judge_pane = payload.get("judge_pane")
    watcher_pid = payload.get("watcher_pid")
    tmux = TmuxSession(session_name or "codex-runner", tmux_bin=tmux_bin)
    if watcher_pid:
        try:
            os.kill(int(watcher_pid), 15)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    if worker_pane:
        try:
            tmux.terminate_pane(str(worker_pane))
        except Exception:
            pass
    if judge_pane:
        try:
            tmux.terminate_pane(str(judge_pane))
        except Exception:
            pass
    cleanup_scope = payload.get("cleanup_scope") or "window"
    window_target = payload.get("window_target")
    if window_target and cleanup_scope == "window":
        layout = type("Layout", (), {"cleanup_scope": "window", "window_target": window_target, "session_name": session_name})
        try:
            tmux.kill(layout)
        except Exception:
            pass
    elif session_name:
        layout = type("Layout", (), {"cleanup_scope": "session", "window_target": payload.get("window_target") or '', "session_name": session_name})
        try:
            tmux.kill(layout)
        except Exception:
            pass
    payload["status"] = "stopped"
    payload["worker_pane"] = None
    payload["judge_pane"] = None
    payload["watcher_pid"] = None
    payload["updated_at"] = _now_iso()
    _write_json(state_path, payload)
    print("runner stopped")
    return 0
