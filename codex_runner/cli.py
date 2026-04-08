from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import init_plan
from .runner import CodexRunner, InteractiveRunner, JudgeWatcher, read_runner_status, stop_runner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-runner", description="Run a worker/judge Codex loop until finish criteria pass.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create .plan scaffolding in a target repository")
    init_parser.add_argument("repo", nargs="?", type=Path, default=Path("."))
    init_parser.add_argument("--task", default=None, help="Initial task for the target repository")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing plan files")

    run_parser = subparsers.add_parser("run", help="Run the worker/judge loop")
    run_parser.add_argument("repo", nargs="?", type=Path, default=Path("."))
    run_parser.add_argument("--task", help="Task override or initial task if .plan files do not exist")
    run_parser.add_argument("--worker-model", default=None)
    run_parser.add_argument("--judge-model", default=None)
    run_parser.add_argument("--max-rounds", type=int, default=12)
    run_parser.add_argument("--keep-session", action="store_true")
    run_parser.add_argument("--codex-bin", default="codex")
    run_parser.add_argument("--tmux-bin", default="tmux")
    run_parser.add_argument("--worker-sandbox", default="danger-full-access")
    run_parser.add_argument("--judge-sandbox", default="danger-full-access")
    run_parser.add_argument("--safe", dest="no_bypass_approvals_and_sandbox", action="store_true", help="Do not pass --dangerously-bypass-approvals-and-sandbox")
    run_parser.add_argument("--no-bypass-approvals-and-sandbox", dest="no_bypass_approvals_and_sandbox", action="store_true", help="Do not pass --dangerously-bypass-approvals-and-sandbox")
    run_parser.add_argument("--batch", action="store_true", help="Use the old non-interactive worker/judge batch loop")
    run_parser.add_argument("--attach", action="store_true", help="When already inside tmux, switch to the runner window immediately")
    run_parser.add_argument("--no-attach", action="store_true", help="Do not attach or switch to the new tmux session")
    run_parser.add_argument("--idle-seconds", type=int, default=8)
    run_parser.add_argument("--poll-seconds", type=int, default=3)
    run_parser.add_argument("--no-shower", action="store_true", help="Disable automatic worker reboot handoffs in interactive mode")
    run_parser.add_argument("--shower-interval", type=int, default=5, help="Reboot the worker after this many interactive judge cycles (default: 5)")
    run_parser.add_argument("--shower-timeout-seconds", type=int, default=180, help="How long to wait for the worker handoff summary before forcing a reboot (default: 180)")

    status_parser = subparsers.add_parser("status", help="Print the last runner state")
    status_parser.add_argument("repo", nargs="?", type=Path, default=Path("."))

    stop_parser = subparsers.add_parser("stop", help="Stop the active runner panes/window")
    stop_parser.add_argument("repo", nargs="?", type=Path, default=Path("."))
    stop_parser.add_argument("--tmux-bin", default="tmux")

    watch_parser = subparsers.add_parser("watch", help="Internal judge watcher command")
    watch_parser.add_argument("repo", type=Path)
    watch_parser.add_argument("--session-name", required=True)
    watch_parser.add_argument("--worker-pane", required=True)
    watch_parser.add_argument("--judge-pane", required=True)
    watch_parser.add_argument("--codex-bin", default="codex")
    watch_parser.add_argument("--tmux-bin", default="tmux")
    watch_parser.add_argument("--judge-model", default=None)
    watch_parser.add_argument("--judge-sandbox", default="danger-full-access")
    watch_parser.add_argument("--worker-model", default=None)
    watch_parser.add_argument("--worker-sandbox", default="danger-full-access")
    watch_parser.add_argument("--dangerous", action="store_true")
    watch_parser.add_argument("--idle-seconds", type=int, default=8)
    watch_parser.add_argument("--poll-seconds", type=int, default=3)
    watch_parser.add_argument("--shower-enabled", action="store_true")
    watch_parser.add_argument("--shower-interval", type=int, default=5)
    watch_parser.add_argument("--shower-timeout-seconds", type=int, default=180)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        repo = args.repo.resolve()
        repo.mkdir(parents=True, exist_ok=True)
        created = init_plan(repo, args.task or "Define the task and finish criteria for this repository.", force=args.force)
        print(f"initialized plan files in {repo}")
        for path in created:
            print(f"  - {path}")
        return 0

    if args.command == "run":
        repo = args.repo.resolve()
        bypass = not args.no_bypass_approvals_and_sandbox
        if args.batch:
            runner = CodexRunner(
                repo,
                task=args.task,
                worker_model=args.worker_model,
                judge_model=args.judge_model,
                max_rounds=args.max_rounds,
                keep_session=args.keep_session,
                codex_bin=args.codex_bin,
                tmux_bin=args.tmux_bin,
                worker_sandbox=args.worker_sandbox,
                judge_sandbox=args.judge_sandbox,
                bypass_approvals_and_sandbox=bypass,
            )
            return runner.run()

        attach = args.attach or (not args.no_attach and not os.environ.get("TMUX"))
        runner = InteractiveRunner(
            repo,
            task=args.task,
            worker_model=args.worker_model,
            judge_model=args.judge_model,
            codex_bin=args.codex_bin,
            tmux_bin=args.tmux_bin,
            worker_sandbox=args.worker_sandbox,
            judge_sandbox=args.judge_sandbox,
            bypass_approvals_and_sandbox=bypass,
            attach=attach,
            idle_seconds=args.idle_seconds,
            poll_seconds=args.poll_seconds,
            shower_enabled=not args.no_shower,
            shower_interval=args.shower_interval,
            shower_timeout_seconds=args.shower_timeout_seconds,
        )
        return runner.start()

    if args.command == "status":
        status = read_runner_status(args.repo.resolve())
        for key, value in status.items():
            print(f"{key}: {value}")
        return 0

    if args.command == "stop":
        return stop_runner(args.repo.resolve(), tmux_bin=args.tmux_bin)

    if args.command == "watch":
        watcher = JudgeWatcher(
            args.repo.resolve(),
            session_name=args.session_name,
            worker_pane=args.worker_pane,
            judge_pane=args.judge_pane,
            codex_bin=args.codex_bin,
            tmux_bin=args.tmux_bin,
            judge_model=args.judge_model,
            judge_sandbox=args.judge_sandbox,
            worker_model=args.worker_model,
            worker_sandbox=args.worker_sandbox,
            bypass_approvals_and_sandbox=args.dangerous,
            idle_seconds=args.idle_seconds,
            poll_seconds=args.poll_seconds,
            shower_enabled=args.shower_enabled,
            shower_interval=args.shower_interval,
            shower_timeout_seconds=args.shower_timeout_seconds,
        )
        return watcher.run()

    parser.error(f"Unknown command: {args.command}")
    return 1
