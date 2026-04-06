from __future__ import annotations

import unittest

from codex_runner.runner import (
    _build_interactive_judge_standby_prompt,
    _build_interactive_worker_prompt,
    _build_interactive_worker_standby_prompt,
    _build_judge_prompt,
    _build_worker_prompt,
    WorkerResult,
)
from codex_runner.checks import DeterministicReport, CheckResult


class PromptTests(unittest.TestCase):
    def test_worker_prompt_bans_optional_next_step_language(self) -> None:
        prompt = _build_worker_prompt(
            task="Ship the fix",
            finish_contract_markdown="# Finish",
            todo_markdown="# TODO",
            judge_feedback="Keep going",
            round_number=1,
        )
        self.assertIn("Do not say `would you like me to continue`", prompt)
        self.assertIn("Treat follow-ups like `continue`, `gogogo`, `yes continue`", prompt)
        self.assertIn("Ignore `.plan/JUDGE_TODO.md`", prompt)

    def test_judge_prompt_explicitly_rejects_premature_done_patterns(self) -> None:
        prompt = _build_judge_prompt(
            task="Ship the fix",
            finish_contract_markdown="# Finish",
            deterministic_report=DeterministicReport(
                passed=False,
                checks=[CheckResult(name="todo:in-progress-empty", passed=False, detail="still active")],
            ),
            worker_result=WorkerResult(
                status="done",
                summary="done",
                changes_made=[],
                remaining_work=[],
                blocking_reason=None,
                verification=[],
            ),
            worker_exit_code=0,
            worker_console_tail="Done. If you want, I can continue.",
            repo_context={"status": "", "diff_stat": "", "diff_names": ""},
            round_number=1,
        )
        self.assertIn("Treat these worker phrases as red flags", prompt)
        self.assertIn("The user's session history contains repeated `continue` / `gogogo` follow-ups", prompt)
        self.assertIn("Reject soft blockage", prompt)
        self.assertIn("Maintain only a tiny scratchpad in `.plan/JUDGE_TODO.md`", prompt)

    def test_interactive_worker_prompt_remembers_continue_annoyance(self) -> None:
        prompt = _build_interactive_worker_prompt(
            task="Ship the fix",
            finish_contract_markdown="# Finish",
            todo_markdown="# TODO",
        )
        self.assertIn("Never ask `would you like me to continue`", prompt)
        self.assertIn("Treat human messages like `continue`, `gogogo`, `yes continue`", prompt)
        self.assertIn("Ignore `.plan/JUDGE_TODO.md`", prompt)


    def test_interactive_worker_standby_prompt_waits_for_user(self) -> None:
        prompt = _build_interactive_worker_standby_prompt()
        self.assertIn("Wait for direct user instructions", prompt)
        self.assertIn("Do not start working yet", prompt)

    def test_interactive_judge_standby_prompt_waits_for_requests(self) -> None:
        prompt = _build_interactive_judge_standby_prompt()
        self.assertIn("Wait for injected evaluation requests", prompt)
        self.assertIn("Do not start evaluating anything yet", prompt)


    def test_judge_request_prompt_writes_to_file_path(self) -> None:
        from pathlib import Path
        from codex_runner.runner import _build_judge_request_prompt
        prompt = _build_judge_request_prompt(
            task="Ship the fix",
            finish_contract_markdown="# Finish",
            deterministic_report=DeterministicReport(passed=False, checks=[CheckResult(name="todo", passed=False, detail="active")]),
            worker_console_tail="worker stopped",
            repo_context={"status": "M a", "diff_stat": "1 file", "diff_names": "a.py"},
            output_path=Path('/tmp/decision.json'),
        )
        self.assertIn('Write EXACT JSON to `/tmp/decision.json`', prompt)
        self.assertIn('Use a shell command to write the JSON file', prompt)


if __name__ == "__main__":
    unittest.main()
