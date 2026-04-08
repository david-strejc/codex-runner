from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re


JSON_BLOCK_RE = re.compile(r"```json(?:\s+codex-runner)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class ContractError(ValueError):
    pass


@dataclass(slots=True)
class VerifyCommand:
    id: str
    command: str
    required: bool = True
    timeout_seconds: int = 900


@dataclass(slots=True)
class TodoRules:
    require_file: bool = True
    in_progress_must_be_empty: bool = True
    blocked_must_be_empty: bool = True


@dataclass(slots=True)
class WorkLockRules:
    must_be_absent_for_complete: bool = True


@dataclass(slots=True)
class FinishContract:
    version: int
    task: str
    done_when: list[str] = field(default_factory=list)
    required_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    verify_commands: list[VerifyCommand] = field(default_factory=list)
    todo: TodoRules = field(default_factory=TodoRules)
    work_lock: WorkLockRules = field(default_factory=WorkLockRules)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "FinishContract":
        verify_commands: list[VerifyCommand] = []
        for index, raw in enumerate(payload.get("verify_commands", []), start=1):
            if isinstance(raw, str):
                command = raw.strip()
                if not command:
                    raise ContractError("verify_commands string entries must be non-empty")
                verify_commands.append(
                    VerifyCommand(
                        id=f"verify-{index}",
                        command=command,
                    )
                )
                continue
            if not isinstance(raw, dict):
                raise ContractError("verify_commands entries must be objects or strings")
            command = str(raw.get("command", "")).strip()
            if not command:
                raise ContractError("verify_commands object entries must contain a non-empty command")
            verify_commands.append(
                VerifyCommand(
                    id=str(raw.get("id", "")).strip() or f"verify-{index}",
                    command=command,
                    required=bool(raw.get("required", True)),
                    timeout_seconds=int(raw.get("timeout_seconds", 900)),
                )
            )
        todo_payload = payload.get("todo", {})
        work_lock_payload = payload.get("work_lock", {})
        if todo_payload is None or not isinstance(todo_payload, dict):
            raise ContractError("todo must be an object")
        if work_lock_payload is None or not isinstance(work_lock_payload, dict):
            raise ContractError("work_lock must be an object")
        task = str(payload.get("task", "")).strip()
        if not task:
            raise ContractError("task must be a non-empty string")
        return cls(
            version=int(payload.get("version", 1)),
            task=task,
            done_when=[str(item) for item in payload.get("done_when", [])],
            required_paths=[str(item) for item in payload.get("required_paths", [])],
            forbidden_paths=[str(item) for item in payload.get("forbidden_paths", [])],
            verify_commands=verify_commands,
            todo=TodoRules(
                require_file=bool(todo_payload.get("require_file", True)),
                in_progress_must_be_empty=bool(todo_payload.get("in_progress_must_be_empty", True)),
                blocked_must_be_empty=bool(todo_payload.get("blocked_must_be_empty", True)),
            ),
            work_lock=WorkLockRules(
                must_be_absent_for_complete=bool(work_lock_payload.get("must_be_absent_for_complete", True))
            ),
        )


def todo_template(task: str) -> str:
    return f"""# TODO

## Backlog

## In Progress
- [ ] {task} (owner: main)

## Blocked

## Done
"""


def judge_todo_template() -> str:
    return """# JUDGE TODO

## Current Verdict Work

## Evidence

## Missing Proof

## Real Blockers

## Notes
"""


def finish_criteria_template(task: str) -> str:
    machine_contract = {
        "version": 1,
        "task": task,
        "done_when": [
            "The requested task is implemented end-to-end.",
            "No active work remains in `.plan/TODO.md`.",
            "The verification commands succeed.",
        ],
        "required_paths": [],
        "forbidden_paths": [],
        "verify_commands": [],
        "todo": {
            "require_file": True,
            "in_progress_must_be_empty": True,
            "blocked_must_be_empty": True,
        },
        "work_lock": {
            "must_be_absent_for_complete": True,
        },
    }
    pretty = json.dumps(machine_contract, indent=2)
    return (
        "# Finish Criteria\n\n"
        "Edit the JSON block below. The runner enforces the machine fields directly and also feeds this whole file to the judge.\n\n"
        "```json codex-runner\n"
        f"{pretty}\n"
        "```\n"
    )


def init_plan(repo_root: Path, task: str, *, force: bool = False) -> list[Path]:
    plan_dir = repo_root / ".plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    todo_path = plan_dir / "TODO.md"
    if force or not todo_path.exists():
        todo_path.write_text(todo_template(task), encoding="utf-8")
        created.append(todo_path)

    judge_todo_path = plan_dir / "JUDGE_TODO.md"
    if force or not judge_todo_path.exists():
        judge_todo_path.write_text(judge_todo_template(), encoding="utf-8")
        created.append(judge_todo_path)

    finish_path = plan_dir / "FINISH_CRITERIA.md"
    if force or not finish_path.exists():
        finish_path.write_text(finish_criteria_template(task), encoding="utf-8")
        created.append(finish_path)

    work_lock_path = plan_dir / "WORK_LOCK"
    if force or not work_lock_path.exists():
        work_lock_path.write_text("Work remains. Remove only when the finish criteria really pass.\n", encoding="utf-8")
        created.append(work_lock_path)

    return created


def load_finish_contract(repo_root: Path) -> tuple[FinishContract, str]:
    finish_path = repo_root / ".plan" / "FINISH_CRITERIA.md"
    if not finish_path.exists():
        raise ContractError(f"Missing finish criteria file: {finish_path}")
    content = finish_path.read_text(encoding="utf-8")
    match = JSON_BLOCK_RE.search(content)
    if not match:
        raise ContractError("FINISH_CRITERIA.md must contain a fenced JSON block")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ContractError("finish criteria JSON must be an object")
    return FinishContract.from_dict(payload), content
