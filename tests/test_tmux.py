from __future__ import annotations

import unittest
from unittest.mock import patch

from codex_runner.tmux import TmuxSession


class TmuxTests(unittest.TestCase):
    def test_send_keys_uses_buffered_paste_for_multiline_text(self) -> None:
        session = TmuxSession("demo")
        recorded: list[tuple[str, ...]] = []

        def fake_run(*args: str, capture: bool = False):
            recorded.append(args)
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        with patch.object(session, "_run", side_effect=fake_run), patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            session.send_keys("%12", "line1\nline2", press_enter=True)

        self.assertEqual(recorded[0], ("set-buffer", "-b", "codex-runner-12", "--", "line1\nline2"))
        self.assertEqual(recorded[1], ("paste-buffer", "-d", "-p", "-t", "%12", "-b", "codex-runner-12"))
        self.assertEqual(recorded[2], ("send-keys", "-t", "%12", "Enter"))
        self.assertTrue(mock_run.called)


if __name__ == "__main__":
    unittest.main()
