from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from codex_runner.checks import parse_todo_sections, run_deterministic_checks
from codex_runner.config import init_plan, load_finish_contract


class CheckTests(unittest.TestCase):
    def test_parse_todo_sections(self) -> None:
        sections = parse_todo_sections(
            "# TODO\n\n## In Progress\n- [ ] Ship it\n\n## Done\n- [x] Old item\n"
        )
        self.assertEqual(sections["In Progress"], ["- [ ] Ship it"])
        self.assertEqual(sections["Done"], ["- [x] Old item"])

    def test_deterministic_checks_fail_when_work_lock_and_in_progress_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Fix the bug")
            contract, _ = load_finish_contract(repo)
            report = run_deterministic_checks(repo, contract)
            self.assertFalse(report.passed)
            failures = {check.name for check in report.checks if not check.passed}
            self.assertIn("todo:in-progress-empty", failures)
            self.assertIn("work-lock:absent", failures)


if __name__ == "__main__":
    unittest.main()
