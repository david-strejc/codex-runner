from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


@dataclass(slots=True)
class PaneLayout:
    session_name: str
    worker_pane: str
    judge_pane: str
    window_target: str
    cleanup_scope: str


class TmuxError(RuntimeError):
    pass


class TmuxSession:
    def __init__(self, session_name: str, *, tmux_bin: str = "tmux") -> None:
        self.session_name = session_name
        self.tmux_bin = tmux_bin

    def _window_name(self) -> str:
        suffix = self.session_name[-18:] if len(self.session_name) > 18 else self.session_name
        return f"cr-{suffix}"

    def _run(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [self.tmux_bin, *args],
            check=False,
            capture_output=capture,
            text=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() if completed.stderr else ""
            raise TmuxError(stderr or f"tmux {' '.join(args)} failed")
        return completed

    def create(self) -> PaneLayout:
        if os.environ.get("TMUX"):
            current_session = self._run("display-message", "-p", "#S", capture=True).stdout.strip()
            if not current_session:
                raise TmuxError("failed to resolve current tmux session")
            window_name = self._window_name()
            window_target = f"{current_session}:{window_name}"
            self._run("new-window", "-d", "-t", current_session, "-n", window_name)
            cleanup_scope = "window"
            session_name = current_session
        else:
            self._run("new-session", "-d", "-s", self.session_name, "-n", "runner")
            window_target = f"{self.session_name}:runner"
            cleanup_scope = "session"
            session_name = self.session_name

        self._run("split-window", "-h", "-t", window_target)
        self._run("set-window-option", "-t", window_target, "remain-on-exit", "on")
        panes = self._run("list-panes", "-t", window_target, "-F", "#{pane_index} #{pane_id}", capture=True).stdout.splitlines()
        pane_map: dict[str, str] = {}
        for line in panes:
            parts = line.split()
            if len(parts) == 2:
                pane_map[parts[0]] = parts[1]
        if "0" not in pane_map or "1" not in pane_map:
            raise TmuxError(f"Expected two panes in session {self.session_name}")
        worker = pane_map["0"]
        judge = pane_map["1"]
        self._run("select-pane", "-t", worker, "-T", "worker")
        self._run("select-pane", "-t", judge, "-T", "judge")
        self._run("set-option", "-p", "-t", worker, "@codex_runner_role", "WORKER")
        self._run("set-option", "-p", "-t", judge, "@codex_runner_role", "JUDGE")
        self._run("set-window-option", "-t", window_target, "pane-border-status", "top")
        self._run("set-window-option", "-t", window_target, "pane-border-format", "#{@codex_runner_role}")
        self._run("select-pane", "-t", worker, "-P", "fg=green")
        self._run("select-pane", "-t", judge, "-P", "fg=yellow")
        self._run("select-pane", "-t", worker, "-T", "WORKER")
        self._run("select-pane", "-t", judge, "-T", "JUDGE")
        return PaneLayout(
            session_name=session_name,
            worker_pane=worker,
            judge_pane=judge,
            window_target=window_target,
            cleanup_scope=cleanup_scope,
        )

    def run_script(self, pane_id: str, script_path: str) -> None:
        self._run("respawn-pane", "-k", "-t", pane_id, f"bash {script_path}")

    def send_keys(self, pane_id: str, text: str, *, press_enter: bool = True) -> None:
        buffer_name = f"codex-runner-{pane_id.strip('%')}"
        self._run("set-buffer", "-b", buffer_name, "--", text)
        try:
            self._run("paste-buffer", "-d", "-p", "-t", pane_id, "-b", buffer_name)
        finally:
            completed = subprocess.run(
                [self.tmux_bin, "delete-buffer", "-b", buffer_name],
                check=False,
                capture_output=True,
                text=True,
            )
        if press_enter:
            self._run("send-keys", "-t", pane_id, "Enter")

    def capture_pane(self, pane_id: str, *, lines: int = 200) -> str:
        result = self._run("capture-pane", "-p", "-S", f"-{lines}", "-t", pane_id, capture=True)
        return result.stdout

    def pane_current_command(self, pane_id: str) -> str:
        return self._run("display-message", "-p", "-t", pane_id, "#{pane_current_command}", capture=True).stdout.strip()

    def pipe_pane(self, pane_id: str, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = f"cat >> {str(log_path)}"
        self._run("pipe-pane", "-o", "-t", pane_id, command)

    def attach_or_switch(self, layout: PaneLayout, *, force_inside_tmux: bool = False) -> None:
        if os.environ.get("TMUX"):
            if force_inside_tmux:
                self._run("select-window", "-t", layout.window_target)
            return
        self._run("attach-session", "-t", layout.session_name)

    def kill(self, layout: PaneLayout) -> None:
        if layout.cleanup_scope == "window":
            completed = subprocess.run(
                [self.tmux_bin, "kill-window", "-t", layout.window_target],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode not in (0, 1):
                stderr = completed.stderr.strip() if completed.stderr else ""
                raise TmuxError(stderr or f"failed to kill tmux window {layout.window_target}")
            return

        completed = subprocess.run(
            [self.tmux_bin, "kill-session", "-t", layout.session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.strip() if completed.stderr else ""
            raise TmuxError(stderr or f"failed to kill tmux session {layout.session_name}")


    def terminate_pane(self, pane_id: str) -> None:
        completed = subprocess.run(
            [self.tmux_bin, "kill-pane", "-t", pane_id],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.strip() if completed.stderr else ""
            raise TmuxError(stderr or f"failed to kill tmux pane {pane_id}")
