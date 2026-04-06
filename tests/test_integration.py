from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from codex_runner.config import init_plan
from codex_runner.runner import CodexRunner, read_runner_status


FAKE_SESSION_ID = "00000000-0000-0000-0000-000000000001"


class IntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the integration test")
    def test_runner_continues_until_deterministic_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            repo = temp_root / "target"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            init_plan(repo, "Finish the target repo")

            finish_path = repo / ".plan" / "FINISH_CRITERIA.md"
            finish_path.write_text(
                textwrap.dedent(
                    """\
                    # Finish Criteria

                    ```json codex-runner
                    {
                      "version": 1,
                      "task": "Finish the target repo",
                      "done_when": ["done"],
                      "required_paths": ["done.txt"],
                      "forbidden_paths": [],
                      "verify_commands": [
                        {
                          "id": "done-file",
                          "command": "test -f done.txt",
                          "required": true,
                          "timeout_seconds": 10
                        }
                      ],
                      "todo": {
                        "require_file": true,
                        "in_progress_must_be_empty": true,
                        "blocked_must_be_empty": true
                      },
                      "work_lock": {
                        "must_be_absent_for_complete": true
                      }
                    }
                    ```
                    """
                ),
                encoding="utf-8",
            )

            fake_codex = temp_root / "fake-codex.py"
            script_lines = [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "from pathlib import Path",
                "args = sys.argv[1:]",
                "output_last = None",
                "cwd = Path('.')",
                "is_resume = len(args) > 1 and args[0] == 'exec' and args[1] == 'resume'",
                "for index, arg in enumerate(args):",
                "    if arg == '-o':",
                "        output_last = Path(args[index + 1])",
                "    elif arg == '-C':",
                "        cwd = Path(args[index + 1])",
                "prompt = sys.stdin.read()",
                "state_path = cwd / '.plan' / 'fake-codex-state.json'",
                "if state_path.exists():",
                "    state = json.loads(state_path.read_text(encoding='utf-8'))",
                "else:",
                "    state = {'worker_calls': 0, 'judge_calls': 0}",
                "def emit(payload):",
                "    text = json.dumps(payload)",
                "    sys.stdout.write(text)",
                "    sys.stdout.flush()",
                "    if output_last:",
                "        output_last.write_text(text, encoding='utf-8')",
                "if 'You are the independent work judge.' in prompt:",
                "    state['judge_calls'] += 1",
                "    emit({'decision': 'complete', 'summary': 'judge would complete', 'reasons': ['stubbed judge'], 'instructions_for_worker': 'Keep working until the deterministic gates pass.', 'missing_checks': []})",
                "else:",
                "    state['worker_calls'] += 1",
                f"    sys.stderr.write('session id: {FAKE_SESSION_ID}\\n')",
                "    if state['worker_calls'] == 1 and not is_resume:",
                "        emit({'status': 'done', 'summary': 'prematurely claimed done', 'changes_made': ['none'], 'remaining_work': [], 'blocking_reason': None, 'verification': []})",
                "    else:",
                "        todo_path = cwd / '.plan' / 'TODO.md'",
                "        todo_text = todo_path.read_text(encoding='utf-8')",
                "        todo_text = todo_text.replace('## In Progress\\n- [ ] Finish the target repo (owner: main)\\n', '## In Progress\\n\\n')",
                "        if '## Done' in todo_text:",
                "            todo_text = todo_text.replace('## Done\\n', '## Done\\n- [x] Finish the target repo\\n')",
                "        else:",
                "            todo_text = todo_text.rstrip() + '\\n\\n## Done\\n- [x] Finish the target repo\\n'",
                "        todo_path.write_text(todo_text, encoding='utf-8')",
                "        work_lock = cwd / '.plan' / 'WORK_LOCK'",
                "        if work_lock.exists():",
                "            work_lock.unlink()",
                "        (cwd / 'done.txt').write_text('done\\n', encoding='utf-8')",
                "        emit({'status': 'done', 'summary': 'actually done', 'changes_made': ['removed WORK_LOCK', 'updated TODO', 'created done.txt'], 'remaining_work': [], 'blocking_reason': None, 'verification': ['done.txt exists']})",
                "state_path.write_text(json.dumps(state), encoding='utf-8')",
            ]
            fake_codex.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            runner = CodexRunner(
                repo,
                task=None,
                worker_model=None,
                judge_model=None,
                max_rounds=4,
                keep_session=False,
                codex_bin=str(fake_codex),
                tmux_bin=shutil.which("tmux") or "tmux",
                worker_sandbox="workspace-write",
                judge_sandbox="read-only",
                bypass_approvals_and_sandbox=False,
            )
            exit_code = runner.run()
            self.assertEqual(exit_code, 0)

            state = read_runner_status(repo)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(len(state["rounds"]), 2)
            self.assertFalse((repo / ".plan" / "WORK_LOCK").exists())
            self.assertTrue((repo / "done.txt").exists())


if __name__ == "__main__":
    unittest.main()
