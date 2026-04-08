# codex-runner

A lightweight supervisor for [OpenAI Codex CLI](https://github.com/openai/codex) that prevents premature task completion. It runs two independent Codex sessions -- a **worker** and a **judge** -- so the worker is never trusted to declare its own work done.

## The Problem

When using Codex for multi-step tasks, the worker frequently says "done" when the work is not actually done. The human ends up typing `continue`, `gogogo`, or `yes continue` over and over to keep it going. This is the single problem codex-runner solves.

## How It Works

codex-runner uses a **dual-agent architecture** inside tmux:

```
┌─────────────────────────────────────────────┐
│                 tmux window                  │
│                                              │
│  ┌─── WORKER ───┐    ┌──── JUDGE ────┐      │
│  │               │    │               │      │
│  │  Interactive  │    │  Interactive   │      │
│  │  Codex CLI    │    │  Codex CLI     │      │
│  │  session      │    │  session       │      │
│  │               │    │               │      │
│  └───────────────┘    └───────────────┘      │
│                                              │
│         ┌──── WATCHER (background) ────┐     │
│         │ Monitors worker idle state   │     │
│         │ Runs deterministic checks    │     │
│         │ Runs isolated auto-judge     │     │
│         │ Pushes worker when not done  │     │
│         └──────────────────────────────┘     │
└─────────────────────────────────────────────┘
```

1. The **worker** does the actual coding work in an interactive Codex session
2. A background **watcher** process monitors the worker pane for idle state (no output for N seconds)
3. When the worker appears idle, the watcher runs **deterministic checks** against finish criteria
4. The watcher launches an isolated one-off **auto-judge** Codex subprocess for the completion decision
5. The auto-judge decides: `continue`, `complete`, or `blocked`
6. If `continue`, the watcher sends the auto-judge's instructions back to the worker
7. In interactive mode, **shower mode** is enabled by default: after every 5 watcher cycles, the watcher forces the worker to write a handoff summary, then reboots the worker pane into a fresh Codex session seeded with that handoff
8. If `complete` AND all deterministic gates pass, the task is done
9. The auto-judge's `complete` is overridden if deterministic gates still fail

Both visible panes are still normal interactive Codex sessions. You can talk to the worker and the judge normally. Automatic watcher evaluations do **not** depend on the interactive judge pane, so user messages there cannot derail the completion loop.

## Completion Gates

Completion requires **both** deterministic gates passing **and** the independent judge choosing `complete`.

### Deterministic checks

- `.plan/TODO.md` has no active `In Progress` items
- `.plan/TODO.md` has no `Blocked` items
- `.plan/WORK_LOCK` is removed (present = work remains)
- Required files exist (configurable)
- Forbidden files do not exist (configurable)
- Verification commands pass (configurable shell commands)

### LLM judge checks

- Whether the repo state actually satisfies the task
- Whether the worker is pretending the task is done
- Whether the next step is `continue`, `complete`, or `blocked`
- The judge is instructed to prefer `continue` over `complete` when uncertain

## Installation

Requires Python 3.12+, [tmux](https://github.com/tmux/tmux), and [Codex CLI](https://github.com/openai/codex).

```bash
pip install -e .
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install -e .
```

## Quick Start

Run in the current directory with a task:

```bash
codex-runner run --task "Fix the flaky login tests"
```

Run for another repo:

```bash
codex-runner run /path/to/repo --task "Implement the auth middleware"
```

Start without a task (worker waits for your instructions):

```bash
codex-runner run
```

This creates a tmux window with two panes (worker and judge) and a background watcher. If you're outside tmux, it creates and attaches to a new tmux session. If you're inside tmux, it creates a new window in your current session.

## Commands

```bash
codex-runner init [repo] [--task "..."]     # Create .plan scaffolding only
codex-runner run [repo] [--task "..."]      # Start interactive worker/judge (default)
codex-runner run [repo] --batch [--task "..."]  # Old non-interactive batch loop
codex-runner status [repo]                  # Print last runner state
codex-runner stop [repo]                    # Terminate runner panes/window
```

### Flags

| Flag | Description |
|------|-------------|
| `--task "..."` | Initial task or task override |
| `--batch` | Use the non-interactive batch loop instead of interactive mode |
| `--attach` | When inside tmux, switch to the runner window immediately |
| `--no-attach` | Start session but don't switch/attach to it |
| `--idle-seconds N` | Worker idle time before judge evaluates (default: 8) |
| `--poll-seconds N` | Watcher polling interval (default: 3) |
| `--no-shower` | Disable automatic worker reboot handoffs in interactive mode |
| `--shower-interval N` | Reboot the worker after this many interactive judge cycles (default: 5) |
| `--shower-timeout-seconds N` | How long to wait for the worker handoff summary before forcing the reboot (default: 180) |
| `--worker-model MODEL` | Override Codex model for worker |
| `--judge-model MODEL` | Override Codex model for judge |
| `--safe` | Don't pass `--dangerously-bypass-approvals-and-sandbox` |
| `--worker-sandbox LEVEL` | Worker sandbox level (default: `danger-full-access`) |
| `--judge-sandbox LEVEL` | Judge sandbox level (default: `danger-full-access`) |
| `--codex-bin PATH` | Path to Codex binary (default: `codex`) |
| `--max-rounds N` | Max worker/judge rounds in batch mode (default: 12) |

## Plan Files

codex-runner expects or creates these files in the target repo:

| File | Purpose |
|------|---------|
| `.plan/TODO.md` | Kanban-style task board the worker maintains |
| `.plan/JUDGE_TODO.md` | Tiny judge-only scratchpad for evaluation notes |
| `.plan/FINISH_CRITERIA.md` | Human-readable criteria with embedded machine-readable JSON |
| `.plan/WORK_LOCK` | Present = work remains. Worker must remove when truly done |
| `.plan/codex-runner/` | Runtime data (state, logs, round artifacts) |

### FINISH_CRITERIA.md format

The finish criteria file contains a fenced JSON block that the runner parses:

````markdown
# Finish Criteria

```json codex-runner
{
  "version": 1,
  "task": "Fix the flaky login tests",
  "done_when": [
    "All login tests pass reliably",
    "No active work remains in TODO.md"
  ],
  "required_paths": ["tests/test_login.py"],
  "forbidden_paths": [],
  "verify_commands": [
    {
      "id": "tests",
      "command": "pytest tests/test_login.py -x",
      "required": true,
      "timeout_seconds": 120
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
````

## Interactive vs Batch Mode

**Interactive (default):** Both worker and judge run as normal interactive Codex sessions. You can talk to either pane. A small background watcher process handles the evaluation loop automatically, using an isolated non-interactive auto-judge subprocess so human interaction with the judge pane stays safe. Shower mode is enabled by default and reboots the worker into a fresh session after every 5 watcher cycles using a forced handoff summary. The worker starts in standby if no `--task` is given.

**Batch (`--batch`):** The old non-interactive loop. Uses `codex exec` and `codex exec resume`. The worker runs, produces structured JSON output, the judge evaluates it, and the loop repeats up to `--max-rounds` times. No human interaction during rounds.

## Defaults

By default, codex-runner launches Codex with:
- `--dangerously-bypass-approvals-and-sandbox`
- `-s danger-full-access`

Use `--safe` to opt out and set sandbox levels with `--worker-sandbox` / `--judge-sandbox`.

## Anti-Patterns The Judge Catches

The judge is specifically trained to reject these worker behaviors:

- "Would you like me to continue?" / "Should I proceed?" when work clearly remains
- Claiming `done` after completing one sub-task when the overall task has more steps
- Soft blockage claims (uncertainty, missing verification) that aren't real blockers
- Optional next-step menus when one next step is clearly required
- Leaving active TODO items, WORK_LOCK, or failing verification commands while claiming completion

## License

MIT
