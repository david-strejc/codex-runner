from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_runner.checks import CheckResult, DeterministicReport
from codex_runner.config import init_plan
from codex_runner.runner import JudgeDecision, JudgeWatcher


class InteractiveWatcherTests(unittest.TestCase):
    def test_run_judge_uses_isolated_subprocess_and_never_injects_into_judge_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.logs_dir.mkdir(parents=True, exist_ok=True)
            watcher.tmux = Mock()
            watcher.tmux.send_keys.side_effect = AssertionError("judge pane should not receive automatic watcher injections")

            def fake_run(args: list[str], **kwargs):
                output_index = args.index("-o") + 1
                decision_path = Path(args[output_index])
                decision_path.write_text(
                    json.dumps(
                        {
                            "decision": "continue",
                            "summary": "keep going",
                            "reasons": ["still active"],
                            "instructions_for_worker": "continue",
                            "missing_checks": ["todo"],
                        }
                    ),
                    encoding="utf-8",
                )

                class Result:
                    returncode = 0
                    stdout = "auto judge ok"
                    stderr = ""

                return Result()

            with patch("codex_runner.runner.subprocess.run", side_effect=fake_run) as mock_run:
                decision, judge_exit = watcher._run_judge(
                    contract=type("Contract", (), {"task": "Finish the repo"})(),
                    finish_markdown="# Finish",
                    deterministic_report=DeterministicReport(
                        passed=False,
                        checks=[CheckResult(name="todo", passed=False, detail="active")],
                    ),
                    pane_text="worker idle",
                    state=type("State", (), {})(),
                )

            self.assertEqual(judge_exit, 0)
            self.assertEqual(decision.decision, "continue")
            self.assertEqual(mock_run.call_count, 1)
            command = mock_run.call_args.args[0]
            self.assertIn("exec", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("--output-schema", command)
            watcher.tmux.send_keys.assert_not_called()

    def test_run_clears_stale_watcher_pid_after_successful_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            (repo / ".plan" / "WORK_LOCK").unlink()
            (repo / ".plan" / "TODO.md").write_text(
                "# TODO\n\n## Backlog\n\n## In Progress\n\n## Blocked\n\n## Done\n- [x] Finish the repo\n",
                encoding="utf-8",
            )
            state_path = repo / ".plan" / "codex-runner" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "running",
                        "task": "Finish the repo",
                        "session_name": "demo",
                        "worker_session_id": None,
                        "current_round": 0,
                        "started_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "rounds": [],
                        "mode": "interactive",
                        "worker_pane": "%1",
                        "judge_pane": "%2",
                        "judge_session_id": None,
                        "watcher_pid": os.getpid(),
                        "window_target": "demo:runner",
                        "cleanup_scope": "session",
                    }
                ),
                encoding="utf-8",
            )

            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.tmux = Mock()
            watcher.tmux.capture_pane.return_value = "worker idle"
            watcher.tmux.pane_current_command.return_value = "codex"
            watcher._detect_worker_session_id = Mock(return_value=None)
            watcher._is_idle = Mock(return_value=True)
            watcher._signature = Mock(return_value="sig")
            watcher._run_judge = Mock(
                return_value=(
                    JudgeDecision(
                        decision="complete",
                        summary="done",
                        reasons=["all clear"],
                        instructions_for_worker="",
                        missing_checks=[],
                    ),
                    0,
                )
            )

            exit_code = watcher.run()

            self.assertEqual(exit_code, 0)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "complete")
            self.assertIsNone(saved["watcher_pid"])
            watcher.tmux.kill.assert_called_once()

    def test_should_shower_only_on_configured_continue_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )

            self.assertFalse(
                watcher._should_shower(
                    round_number=4,
                    decision=JudgeDecision("continue", "x", [], "continue", []),
                )
            )
            self.assertTrue(
                watcher._should_shower(
                    round_number=5,
                    decision=JudgeDecision("continue", "x", [], "continue", []),
                )
            )
            self.assertFalse(
                watcher._should_shower(
                    round_number=5,
                    decision=JudgeDecision("complete", "x", [], "", []),
                )
            )

    def test_perform_shower_reboots_worker_from_handoff_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.logs_dir.mkdir(parents=True, exist_ok=True)
            watcher.tmux = Mock()
            state = type("State", (), {"worker_session_id": "abc", "worker_reboots": 0})()

            def fake_send_keys(_pane: str, text: str, *, press_enter: bool = True) -> None:
                marker = "Write a concise but complete handoff summary to `"
                start = text.index(marker) + len(marker)
                end = text.index("` right now.", start)
                handoff_path = Path(text[start:end])
                handoff_path.write_text("summary from worker", encoding="utf-8")

            watcher.tmux.send_keys.side_effect = fake_send_keys

            summary = watcher._perform_shower(
                state=state,
                current_command="codex",
                pane_text="old pane text",
                instructions="keep going",
                round_number=5,
            )

            self.assertEqual(summary, "summary from worker")
            self.assertEqual(state.worker_session_id, None)
            self.assertEqual(state.worker_reboots, 1)
            watcher.tmux.run_script.assert_called_once()
            watcher.tmux.pipe_pane.assert_called_once()

    def test_cleanup_owned_session_skips_non_owned_window_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.tmux = Mock()
            state = type("State", (), {"cleanup_scope": "window", "session_name": "demo", "window_target": "demo:win"})()

            watcher._cleanup_owned_session_on_terminal(state)

            watcher.tmux.kill.assert_not_called()

    def test_run_skips_judging_while_worker_is_in_standby(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            state_path = repo / ".plan" / "codex-runner" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "running",
                        "task": "Finish the repo",
                        "session_name": "demo",
                        "worker_session_id": None,
                        "current_round": 0,
                        "started_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "rounds": [],
                        "mode": "interactive",
                        "worker_pane": "%1",
                        "judge_pane": "%2",
                        "judge_session_id": None,
                        "watcher_pid": 999999,
                        "window_target": "demo:runner",
                        "cleanup_scope": "session",
                        "worker_reboots": 0,
                        "standby": True,
                    }
                ),
                encoding="utf-8",
            )

            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.tmux = Mock()
            watcher.tmux.capture_pane.return_value = "standby"
            watcher.tmux.pane_current_command.return_value = "codex"
            watcher._detect_worker_session_id = Mock(return_value=None)
            watcher._run_judge = Mock()

            with patch("codex_runner.runner.time.sleep", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    watcher.run()

            watcher._run_judge.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["standby"])

    def test_no_task_session_resets_to_standby_after_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_plan(repo, "Finish the repo")
            (repo / ".plan" / "WORK_LOCK").unlink()
            (repo / ".plan" / "TODO.md").write_text(
                "# TODO\n\n## Backlog\n- [ ] Future slice\n\n## In Progress\n\n## Blocked\n\n## Done\n- [x] Finish the repo\n",
                encoding="utf-8",
            )
            state_path = repo / ".plan" / "codex-runner" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "running",
                        "task": "Finish the repo",
                        "session_name": "demo",
                        "worker_session_id": "abc",
                        "current_round": 0,
                        "started_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "rounds": [],
                        "mode": "interactive",
                        "worker_pane": "%1",
                        "judge_pane": "%2",
                        "judge_session_id": None,
                        "watcher_pid": os.getpid(),
                        "window_target": "demo:runner",
                        "cleanup_scope": "session",
                        "worker_reboots": 0,
                        "standby": False,
                        "started_without_task": True,
                    }
                ),
                encoding="utf-8",
            )

            watcher = JudgeWatcher(
                repo,
                session_name="demo",
                worker_pane="%1",
                judge_pane="%2",
                codex_bin="codex",
                tmux_bin="tmux",
                judge_model=None,
                judge_sandbox="danger-full-access",
                worker_model=None,
                worker_sandbox="danger-full-access",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.tmux = Mock()
            watcher.tmux.capture_pane.return_value = "worker idle"
            watcher.tmux.pane_current_command.return_value = "codex"
            watcher._detect_worker_session_id = Mock(return_value="abc")
            watcher._is_idle = Mock(return_value=True)
            watcher._signature = Mock(side_effect=["sig-complete", SystemExit(0)])
            watcher._run_judge = Mock(
                return_value=(
                    JudgeDecision(
                        decision="complete",
                        summary="done",
                        reasons=["all clear"],
                        instructions_for_worker="",
                        missing_checks=[],
                    ),
                    0,
                )
            )

            with patch("codex_runner.runner.time.sleep", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    watcher.run()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["standby"])
            self.assertTrue(saved["started_without_task"])
            self.assertEqual(saved["status"], "running")
            watcher.tmux.run_script.assert_called_once()
            watcher.tmux.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
