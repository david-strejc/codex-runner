from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_runner.checks import CheckResult, DeterministicReport
from codex_runner.config import init_plan
from codex_runner.runner import (
    JudgeDecision,
    JudgeWatcher,
    _auto_accept_trust_prompt,
    _base_codex_args,
    _worker_maybe_claims_completion,
)


class InteractiveWatcherTests(unittest.TestCase):
    def test_base_codex_args_include_approval_policy_when_not_bypassing(self) -> None:
        self.assertEqual(
            _base_codex_args(sandbox="danger-full-access", bypass=False, approval_policy="never"),
            ["-a", "never", "-s", "danger-full-access"],
        )

    def test_auto_accept_trust_prompt_sends_confirmation_once(self) -> None:
        tmux = Mock()
        tmux.capture_pane.side_effect = [
            "Do you trust the contents of this directory?\n1. Yes, continue",
            "OpenAI Codex\nStanding by",
        ]

        accepted = _auto_accept_trust_prompt(tmux, "%1", timeout_seconds=1)

        self.assertTrue(accepted)
        tmux.press_enter.assert_called_once_with("%1")

    def test_worker_completion_heuristic_ignores_incomplete_summary(self) -> None:
        event = {
            "last_assistant_message": "The branch is still not complete; the remaining open item is RelationLoader.",
        }
        self.assertFalse(_worker_maybe_claims_completion(event))

    def test_worker_completion_heuristic_detects_completion_summary(self) -> None:
        event = {
            "last_assistant_message": "The cleanup closeout criteria are currently satisfied and no further work remains.",
        }
        self.assertTrue(_worker_maybe_claims_completion(event))

    def test_run_judge_uses_visible_judge_pane_and_waits_for_decision_file(self) -> None:
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
                bypass_approvals_and_sandbox=False,
                idle_seconds=1,
                poll_seconds=1,
                shower_enabled=True,
                shower_interval=5,
                shower_timeout_seconds=30,
            )
            watcher.logs_dir.mkdir(parents=True, exist_ok=True)
            watcher.tmux = Mock()
            watcher.tmux.capture_pane.return_value = ""
            watcher.tmux.pane_current_command.return_value = "codex"
            state = type("State", (), {"judge_session_id": "judge-1"})()

            def fake_send_keys(_pane: str, text: str, *, press_enter: bool = True) -> None:
                marker = "Write the JSON decision to "
                start = text.index(marker) + len(marker)
                end = text.index(". Do not ask the human anything.", start)
                decision_path = Path(text[start:end])
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

            watcher.tmux.send_keys.side_effect = fake_send_keys
            with patch.object(watcher, "_ensure_judge_session", return_value="judge-1") as ensure_mock:
                decision, judge_exit = watcher._run_judge(
                    contract=type("Contract", (), {"task": "Finish the repo"})(),
                    finish_markdown="# Finish",
                    deterministic_report=DeterministicReport(
                        passed=False,
                        checks=[CheckResult(name="todo", passed=False, detail="active")],
                    ),
                    worker_turn_summary="worker turn summary",
                    state=state,
                )

            self.assertEqual(judge_exit, 0)
            self.assertEqual(decision.decision, "continue")
            ensure_mock.assert_called_once()
            watcher.tmux.send_keys.assert_called_once()

    def test_run_keeps_worker_session_after_successful_completion(self) -> None:
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
            watcher._worker_turn_events_path().parent.mkdir(parents=True, exist_ok=True)
            watcher._worker_turn_events_path().write_text(
                json.dumps({"role": "worker", "turn_id": "turn-1", "input_messages": ["do it"], "last_assistant_message": "done"}) + "\n",
                encoding="utf-8",
            )

            with patch("codex_runner.runner.time.sleep", side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    watcher.run()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "running")
            self.assertFalse(saved.get("standby", False))
            self.assertIsNone(saved["worker_session_id"])
            watcher.tmux.run_script.assert_not_called()
            watcher.tmux.kill.assert_not_called()

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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
                marker = "Write the handoff summary to "
                start = text.index(marker) + len(marker)
                end = text.index(". After the file is written, stop and wait.", start)
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

    def test_perform_shower_falls_back_after_turn_complete_without_handoff_file(self) -> None:
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
            watcher._worker_turn_events_path().write_text("", encoding="utf-8")

            with patch.object(
                watcher,
                "_wait_for_worker_handoff",
                return_value=(False, True),
            ) as wait_mock:
                summary = watcher._perform_shower(
                    state=state,
                    current_command="codex",
                    pane_text="old pane text",
                    instructions="keep going",
                    round_number=5,
                )

            self.assertIn("Fallback handoff summary", summary)
            self.assertEqual(state.worker_session_id, None)
            self.assertEqual(state.worker_reboots, 1)
            wait_mock.assert_called_once()
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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

    def test_no_task_session_switches_to_normal_mode_after_new_prompt_activation(self) -> None:
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
                        "standby": True,
                        "started_without_task": True,
                        "awaiting_worker_change_after_complete": False,
                        "completion_latch_hash": None,
                        "standby_prompt_count": None,
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
                judge_approval_policy="never",
                worker_model=None,
                worker_sandbox="danger-full-access",
                worker_approval_policy="never",
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
            watcher._run_judge = Mock(
                return_value=(
                    JudgeDecision(
                        decision="continue",
                        summary="keep going",
                        reasons=["needs more"],
                        instructions_for_worker="",
                        missing_checks=[],
                    ),
                    0,
                )
            )
            watcher._worker_turn_events_path().parent.mkdir(parents=True, exist_ok=True)
            watcher._worker_turn_events_path().write_text(
                "\n".join(
                    [
                        json.dumps({"role": "worker", "turn_id": "turn-startup", "input_messages": ["standby"], "last_assistant_message": "standing by"}),
                        json.dumps({"role": "worker", "turn_id": "turn-user", "input_messages": ["real task"], "last_assistant_message": "working"}),
                    ]
                ) + "\n",
                encoding="utf-8",
            )

            sleep_calls = {"count": 0}

            def fake_sleep(_seconds: float) -> None:
                sleep_calls["count"] += 1
                if sleep_calls["count"] >= 2:
                    raise SystemExit(0)

            with patch("codex_runner.runner.time.sleep", side_effect=fake_sleep):
                with self.assertRaises(SystemExit):
                    watcher.run()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(saved["standby"])
            self.assertFalse(saved["started_without_task"])
            self.assertEqual(saved["status"], "running")
            watcher._run_judge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
