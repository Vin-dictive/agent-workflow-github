# agent-workflow-github

A proof of concept: a **Claude Agent SDK** application that runs as a
**manually-triggered GitHub Actions workflow**, authenticates with a
**Claude Code OAuth token**, and **commits its output back to the repository**.

You type a task into the "Run workflow" form. The agent gets a full checkout, a
real tool loop (read / write / edit / grep / bash), does the work, and the job
pushes the resulting diff plus a run report to the branch you launched from.

```
workflow_dispatch(prompt)
  └─ checkout (full history)
     └─ pip install claude-agent-sdk
        └─ scripts/run_agent.py  ──> edits files, writes agent-runs/<ts>-<run>.md
           └─ git commit && git push  (the workflow, never the agent)
```

## Setup

### 1. Generate the OAuth token

On your machine, with Claude Code installed and logged in:

```bash
claude setup-token
```

This mints a long-lived token tied to your Claude subscription (Pro, Max, Ultra,
Team, or Enterprise). Copy the output.

### 2. Store it as a repository secret

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN
# paste the token, then Ctrl-D
```

Or: **Settings → Secrets and variables → Actions → New repository secret**, named
`CLAUDE_CODE_OAUTH_TOKEN`.

### 3. Push to GitHub

The workflow only appears in the Actions tab once `.github/workflows/agent.yml`
is on the **default branch** — that's a GitHub rule for `workflow_dispatch`.

```bash
git remote add origin git@github.com:<you>/agent-workflow-github.git
git push -u origin main
```

### 4. Run it

**Actions → Claude Agent → Run workflow**, fill in the prompt, and go. Try:

- `Write a REPORT.md summarizing what this repo does, its layout, and how the workflow is wired.`
- `Add type hints and docstrings to scripts/run_agent.py where they're missing.`
- `Read the last 20 commits and append a Keep-a-Changelog entry to CHANGELOG.md.`

## Running it locally

Same code path, no Actions involved:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CLAUDE_CODE_OAUTH_TOKEN=...   # or ANTHROPIC_API_KEY=...
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md" --model claude-opus-5
```

The script refuses to start if neither credential is set. It writes a report to
`agent-runs/` and leaves committing to you.

## Workflow inputs

| Input | Default | What it does |
| --- | --- | --- |
| `prompt` | _required_ | The task. Free-form; the agent plans its own steps. |
| `model` | `claude-opus-5` | `claude-sonnet-5` and `claude-haiku-4-5` are cheaper/faster. |
| `max_turns` | `25` | Hard cap on agentic turns, so a confused run can't spin. |
| `commit` | `true` | Uncheck for a dry run: the agent still works and reports, nothing is pushed. |

## How the pieces fit

**`scripts/run_agent.py`** — calls `query()` from `claude_agent_sdk` with:

- `cwd` pinned to the repo root, so tools operate on the checkout
- `permission_mode="acceptEdits"` plus an `allowed_tools` allowlist, because a CI
  job has nobody to answer a permission prompt
- `disallowed_tools` denying `git commit`, `git push`, `git reset`,
  `git checkout`, and `gh` — these hold in **every** permission mode, so the
  agent can't touch history or the remote even if it tries
- `setting_sources=["project"]`, so `CLAUDE.md` and `.claude/` from the checkout
  load but the runner's user-level config doesn't
- `system_prompt` = the `claude_code` preset with CI context appended, keeping
  Claude Code's own tool guidance intact

It streams the loop to stdout (so the Actions log reads like a transcript), writes
a GitHub step summary, and exits non-zero on `error_during_execution`,
`error_max_turns`, or `error_max_budget_usd`.

**`.github/workflows/agent.yml`** — one job, `permissions: contents: write`,
`concurrency` keyed on the ref so two runs can't race the same branch. The commit
step uses the `github-actions[bot]` identity, retries the push with a rebase up
to three times, and runs under `!cancelled()` so a **failed** run still commits
its report.

## Things worth knowing

**Pushes made with `GITHUB_TOKEN` don't trigger other workflows.** Deliberate, on
GitHub's side, to stop recursive runs. If you want the agent's commit to kick off
CI, push with a GitHub App installation token or a PAT instead of the default
token.

**A failed run still gets committed** when `commit` is checked, so the report
explaining the failure lands in the repo — but that also means a partial edit can
land. Review before merging, or point the workflow at a scratch branch. For
production you'd open a PR instead of pushing: swap the commit step for
`peter-evans/create-pull-request`, or `git push origin HEAD:agent/<run_id>` plus
`gh pr create`.

**Prompt text never enters a `run:` string.** Every `${{ inputs.* }}` reference
goes through `env:`. Interpolating a dispatch input directly into shell would let
`$(...)` in the prompt execute on the runner.

**The agent can run `Bash`.** It's auto-approved so the run doesn't stall. The
deny rules block git-write and `gh`, but this is still a broad tool surface —
tighten `DEFAULT_ALLOWED_TOOLS` in `scripts/run_agent.py` if you want
read-and-edit only (`["Read", "Write", "Edit", "Glob", "Grep"]`).

**OAuth tokens are per-person.** The token is tied to whoever ran
`claude setup-token`, and runs bill against their subscription. For an
org-wide or shared setup, use `ANTHROPIC_API_KEY` instead — change one line in
the workflow's `env:` block. Anthropic's SDK docs also note that offering
claude.ai login or subscription rate limits *to your own product's end users*
needs prior approval; a token you minted for your own repo's CI is the same thing
`claude-code-action` does by default.

**No Node.js step needed.** The `claude-agent-sdk` platform wheel bundles the
Claude Code binary. On platforms where pip falls back to the source distribution
(e.g. ARM64 Windows) you'd install Claude Code natively and let the SDK find it
on `PATH`.
