# Project guidance

This repo is a proof of concept: a manually-triggered GitHub Actions workflow
runs a Claude Agent SDK application against the checkout, and the job commits
and pushes whatever the agent changed.

## Layout

- `scripts/run_agent.py` — the SDK application. Streams the agent loop, writes a
  Markdown report, exits non-zero when the run didn't complete cleanly.
- `.github/workflows/agent.yml` — the `workflow_dispatch` workflow.
- `agent-runs/` — one Markdown report per run, committed by the workflow.

## Conventions

- Reports under `agent-runs/` are generated output. Don't hand-edit them, and
  don't reformat past reports.
- Pass untrusted input (prompts, event payload fields) to workflow steps through
  `env:`, never interpolated into a `run:` string.
- Keep `scripts/run_agent.py` dependency-free beyond `claude-agent-sdk` and the
  standard library.

## When running as the agent in CI

You are in a throwaway checkout. Edit files freely, but `git commit`, `git push`,
`git reset`, `git checkout`, and `gh` are blocked — the workflow owns history and
the remote. Read-only git is available and useful for understanding context.
