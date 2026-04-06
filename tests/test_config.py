from __future__ import annotations

import os
import tempfile
from pathlib import Path
import unittest

from codex_runner.cli import main
from codex_runner.config import init_plan, load_finish_contract


class ConfigTests(unittest.TestCase):
    def test_init_plan_creates_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            created = init_plan(repo, "Fix the thing")
            created_names = {path.name for path in created}
            self.assertEqual(created_names, {"TODO.md", "JUDGE_TODO.md", "FINISH_CRITERIA.md", "WORK_LOCK"})

    def test_load_finish_contract_parses_markdown_json_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Ship the feature")
            contract, raw = load_finish_contract(repo)
            self.assertEqual(contract.task, "Ship the feature")
            self.assertIn("Finish Criteria", raw)

    def test_cli_init_defaults_to_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            previous = Path.cwd()
            os.chdir(repo)
            try:
                rc = main(["init", "--task", "Use cwd"])
            finally:
                os.chdir(previous)
            self.assertEqual(rc, 0)
            self.assertTrue((repo / ".plan" / "TODO.md").exists())
            self.assertTrue((repo / ".plan" / "JUDGE_TODO.md").exists())
            self.assertTrue((repo / ".plan" / "FINISH_CRITERIA.md").exists())


if __name__ == "__main__":
    unittest.main()
