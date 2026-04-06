from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess

from .config import FinishContract, VerifyCommand


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    required: bool = True


@dataclass(slots=True)
class DeterministicReport:
    passed: bool
    checks: list[CheckResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
        }

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        for check in self.checks:
            status = "PASS" if check.passed else "FAIL"
            lines.append(f"{status} {check.name}: {check.detail}")
        return lines


def parse_todo_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current and line.lstrip().startswith("- ["):
            sections.setdefault(current, []).append(line.strip())
    return sections


def _tail(text: str, max_lines: int = 20, max_chars: int = 1500) -> str:
    lines = text.strip().splitlines()
    tail = "\n".join(lines[-max_lines:]).strip()
    if len(tail) <= max_chars:
        return tail
    return tail[-max_chars:]


def _todo_active_items(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.lower().startswith("- [x]")]


def _run_verify_command(repo_root: Path, command: VerifyCommand) -> CheckResult:
    try:
        completed = subprocess.run(
            ["bash", "-lc", command.command],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=command.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=f"verify:{command.id}",
            passed=False,
            detail=f"timed out after {command.timeout_seconds}s",
            required=command.required,
        )
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    detail = f"exit={completed.returncode}"
    tail = _tail(output)
    if tail:
        detail = f"{detail}; tail:\n{tail}"
    return CheckResult(
        name=f"verify:{command.id}",
        passed=completed.returncode == 0,
        detail=detail,
        required=command.required,
    )


def run_deterministic_checks(repo_root: Path, contract: FinishContract) -> DeterministicReport:
    checks: list[CheckResult] = []

    todo_path = repo_root / ".plan" / "TODO.md"
    if contract.todo.require_file:
        checks.append(
            CheckResult(
                name="todo:file",
                passed=todo_path.exists(),
                detail="present" if todo_path.exists() else "missing",
            )
        )
    sections = parse_todo_sections(todo_path.read_text(encoding="utf-8")) if todo_path.exists() else {}

    if contract.todo.in_progress_must_be_empty:
        active = _todo_active_items(sections.get("In Progress", []))
        checks.append(
            CheckResult(
                name="todo:in-progress-empty",
                passed=not active,
                detail="empty" if not active else "; ".join(active),
            )
        )

    if contract.todo.blocked_must_be_empty:
        blocked = _todo_active_items(sections.get("Blocked", []))
        checks.append(
            CheckResult(
                name="todo:blocked-empty",
                passed=not blocked,
                detail="empty" if not blocked else "; ".join(blocked),
            )
        )

    work_lock_path = repo_root / ".plan" / "WORK_LOCK"
    if contract.work_lock.must_be_absent_for_complete:
        checks.append(
            CheckResult(
                name="work-lock:absent",
                passed=not work_lock_path.exists(),
                detail="absent" if not work_lock_path.exists() else "still present",
            )
        )

    for relative_path in contract.required_paths:
        path = repo_root / relative_path
        checks.append(
            CheckResult(
                name=f"path:required:{relative_path}",
                passed=path.exists(),
                detail="present" if path.exists() else "missing",
            )
        )

    for relative_path in contract.forbidden_paths:
        path = repo_root / relative_path
        checks.append(
            CheckResult(
                name=f"path:forbidden:{relative_path}",
                passed=not path.exists(),
                detail="absent" if not path.exists() else "present",
            )
        )

    for command in contract.verify_commands:
        checks.append(_run_verify_command(repo_root, command))

    passed = all(check.passed or not check.required for check in checks)
    return DeterministicReport(passed=passed, checks=checks)
