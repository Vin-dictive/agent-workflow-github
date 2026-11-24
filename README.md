# agent-workflow-github

A GitHub Actions workflow that runs a Claude agent on your repo and commits the changes. Trigger it manually from the **Actions** tab, type a prompt, and watch the agent edit files and push the results.

**Key design:**
- **No Anthropic credential in this repo.** The runner joins your tailnet as an ephemeral node and talks to Claude through [Tailscale Aperture](https://tailscale.com/docs/aperture/what-is-aperture), which injects the real API key.
- **The agent can only edit files.** It can't commit, push, reset, check out, or run `gh`—the workflow owns git history.
- **Every run leaves a report** in `agent-runs/`, including failures.

This is a proof of concept. Read [Security notes](#security-notes) before you use it on anything sensitive.

## How it works

1. You trigger the workflow from GitHub's Actions tab with a prompt.
2. The job checks out your repo (full history, so the agent can read git log/diff).
3. It joins your tailnet as an ephemeral node tagged `tag:ai-workflow-machine`.
4. The agent SDK talks to Claude through Aperture at `http://<aperture-host>`.
   - Aperture sees the source IP, looks up the tag, checks the grant, and injects your Anthropic API key.
5. The agent reads and edits files in the checkout.
6. The workflow commits and pushes changes (or skips the push if you unchecked `commit`).
7. A Markdown report gets committed to `agent-runs/`.

**Key files:**
- `.github/workflows/agent.yml` — the workflow
- `scripts/run_agent.py` — the Agent SDK app; streams the loop, writes reports, exits non-zero on failure
- `CLAUDE.md` — project-level guidance the agent reads on every run
- `agent-runs/` — generated reports (don't hand-edit)

## Setup

### 1. Tailscale policy: add a tag and a grant

In the Tailscale admin console, edit the policy file. Replace `ai` with your Aperture hostname:

```json
{
  "tagOwners": {
    "tag:ai-workflow-machine": []
  },
  "grants": [
    {
      "src": ["tag:ai-workflow-machine"],
      "dst": ["ai"],
      "ip": ["tcp:80"],
      "app": {
        "tailscale.com/cap/aperture": [
          { "role": "user" },
          { "models": "anthropic/**" }
        ]
      }
    }
  ]
}
```

**Important:** Aperture needs *both* `role` and `models` in the capabilities. Without `role`, you'll get **403**. To limit CI to specific models, narrow the glob, e.g. `"anthropic/claude-opus-5-5"`. `src` must match exactly—globs like `tag:ai-*` won't work.

### 2. Create a Tailscale OAuth client

Go to **Settings → OAuth clients** in the admin console:
- Scope: `auth_keys` (write)
- Tag: `tag:ai-workflow-machine` (must match the tag in your policy)

The workflow will create an ephemeral node with this tag and remove it when the job ends.

### 3. Set GitHub secrets and variables

```bash
gh secret set TS_OAUTH_CLIENT_ID
gh secret set TS_OAUTH_SECRET
gh variable set APERTURE_HOST --body ai
```

| Name | Type | Required | Default |
| --- | --- | --- | --- |
| `TS_OAUTH_CLIENT_ID` | secret | ✓ | — |
| `TS_OAUTH_SECRET` | secret | ✓ | — |
| `APERTURE_HOST` | variable | | `ai` |
| `TS_HOSTNAME` | variable | | `ai-workflow` |
| `TS_TAG` | variable | | `tag:ai-workflow-machine` |
| `WORKFLOW_PAT` | secret | only for `.github/workflows/` edits | — |

**If you change `TS_TAG`**, update the policy and OAuth client tags to match.

### 4. Push the workflow to the default branch

GitHub only shows `workflow_dispatch` workflows in the Actions tab once they're on the default branch.

### 5. Run it

Go to **Actions → Claude Agent → Run workflow**. Try a prompt like:
- `Write a REPORT.md explaining what this repo does.`
- `Add type hints to scripts/run_agent.py.`
- `Append a changelog entry for the last 10 commits.`

## Workflow inputs

| Input | Default | Description |
| --- | --- | --- |
| `prompt` | required | The task. The agent plans its own steps. |
| `model` | `anthropic/claude-haiku-4.5` | Must be served by Aperture and match your grant's `models` glob. |
| `max_turns` | `25` | Hard limit on agentic turns. |
| `commit` | `true` | Uncheck for a dry run: the agent runs and reports, but doesn't push. |

The job has a 30-minute timeout. Runs on the same branch are queued sequentially.

## Running locally

If your machine is on the tailnet:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_BASE_URL=http://ai
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md"
```

Without Aperture, set `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) instead of `ANTHROPIC_BASE_URL`. The script writes a report to `agent-runs/` and doesn't commit.

**run_agent.py flags:**

| Flag | Default | Description |
| --- | --- | --- |
| `--prompt TEXT` | — | The task, inline. |
| `--prompt-env NAME` | `AGENT_PROMPT` | Read the task from this env var (CI uses this for shell safety). |
| `--model ID` | `$AGENT_MODEL` or Claude Code's default | Model alias or full ID. |
| `--max-turns N` | `25` | Hard limit on agentic turns. |
| `--max-budget-usd X` | none | Stop when client-side cost reaches this. |
| `--permission-mode MODE` | `acceptEdits` | Claude Code permission mode. |
| `--report-dir DIR` | `agent-runs` | Report output directory; pass `""` to skip. |

## How the script is configured

The Agent SDK is configured with:

- **`permission_mode="acceptEdits"`** and an allowed-tools list (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`). There's no human to approve prompts in CI.
- **`disallowed_tools`** that block `git commit`, `git push`, `git reset`, `git checkout`, and `gh` in every permission mode.
- **`setting_sources=["project"]`** so it loads `CLAUDE.md` and `.claude/` from the checkout, ignoring runner-level config.
- **Gateway setup:** When `ANTHROPIC_BASE_URL` is set, the script sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` to skip update checks and telemetry that would bypass the gateway. If no credential exists, it sets a placeholder; Aperture replaces it.

The script streams the loop to stdout (Actions logs are readable transcripts). It writes a Markdown report with the task, agent's message, changed files, cost, tokens, and a collapsed transcript. It exits `1` on `error_during_execution`, `error_max_turns`, or `error_max_budget_usd`.

**Commits:** The job commits as `github-actions[bot]` even if the run fails (under `!cancelled()`). It uses the prompt's first line as the commit subject. On push rejection, it checks why: it rebases and retries (up to 3 times) only if the remote moved on; other rejections fail immediately.

**Workflow edits:** `GITHUB_TOKEN` can't push to `.github/workflows/` by design—GitHub blocks it to prevent runs from rewriting themselves. If the agent needs to edit workflows, set `WORKFLOW_PAT` (with the `workflow` scope). Otherwise, the commit step detects this case and reports it instead of retrying.

**Triggering CI:** Pushes with `GITHUB_TOKEN` don't trigger other workflows (by design, to stop recursion). Use a GitHub App token or PAT if you want the agent's commit to kick off CI.

## Security notes

- **Aperture is `http://` only.** The tailnet encrypts and authenticates the connection; Aperture doesn't serve TLS on the tailnet.
- **Access is deny-by-default.** Grants only add access; there's no `deny` rule. To restrict access, edit or remove a grant.
- **Prompt text is shell-safe.** All `${{ inputs.* }}` go through `env:`, not interpolated into `run:`. Prompt-embedded `$(...)` won't execute.
- **The agent can run `Bash`.** It can't do git writes or `gh`, but that's still broad. To allow read-and-edit only, remove `Bash` from `DEFAULT_ALLOWED_TOOLS` in `scripts/run_agent.py`.
- **Failed runs are committed.** A partial edit can land on the branch. Use a scratch branch, or replace the push step with a PR creation action (e.g., `peter-evans/create-pull-request`).

## Troubleshooting

| Problem | Cause |
| --- | --- |
| "Join the tailnet" fails on `ping` | Wrong `APERTURE_HOST`, OAuth client tag doesn't match `TS_TAG`, or grant missing `tcp:80`. |
| **403** from Aperture | Grant missing `{ "role": "user" }`. Both `role` and `models` are required. |
| **404** from Aperture | No `models` pattern matches the requested model, or no provider serves it. |
| Agent's push didn't trigger CI | Expected with `GITHUB_TOKEN`. Use a GitHub App token or PAT to trigger downstream workflows. |

## Variations and optional setups

**Federated OIDC instead of an OAuth secret:** Pass `audience: ${{ secrets.TS_AUDIENCE }}` to the Tailscale action in place of `oauth-secret`, and add `id-token: write` to the job's permissions.

**No SDK wheel on your platform (e.g., ARM64):** Install Claude Code natively. The SDK finds it on `PATH`.

**GitHub App token instead of GITHUB_TOKEN:** Replace `token: ${{ secrets.GITHUB_TOKEN }}` in `actions/checkout` with `token: ${{ secrets.GITHUB_APP_TOKEN }}` to trigger downstream workflows on the agent's push. See GitHub's docs on creating app tokens.

## Learn more

- **Aperture:** [What it is](https://tailscale.com/docs/aperture/what-is-aperture) · [How it works](https://tailscale.com/docs/aperture/how-aperture-works) · [Get started](https://tailscale.com/docs/aperture/get-started) · [Grants](https://tailscale.com/docs/aperture/how-grants-work)
- **Aperture + Anthropic:** [Setup](https://tailscale.com/docs/aperture/how-to/use-anthropic) · [Claude Code](https://tailscale.com/docs/aperture/how-to/use-claude-code) · [GitHub Action](https://tailscale.com/docs/aperture/how-to/use-claude-code-action)
- **Tools:** [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action) · [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) · [Claude Code env vars](https://code.claude.com/docs/en/env-vars)
