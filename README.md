# agent-workflow-github

Run a Claude agent on this repository from the GitHub **Actions** tab. You type a
prompt, the agent edits the checkout, and the workflow commits and pushes the
result along with a Markdown report of the run.

The repository stores no Anthropic credential. The runner joins your Tailscale
tailnet as an ephemeral, tagged node and reaches Claude through a
[Tailscale Aperture](https://tailscale.com/docs/aperture/what-is-aperture)
gateway. Aperture identifies the node by its tag and injects the real API key
upstream.

> **Proof of concept.** Read [Security](#security) before pointing it at
> anything that matters.

## At a glance

- **Manual trigger only** (`workflow_dispatch`), one run per branch at a time.
- **The agent edits files; the workflow owns git.** `git commit`, `git push`,
  `git reset`, `git checkout`, and `gh` are blocked for the agent.
- **Every run leaves a report** in `agent-runs/`, including failed runs.

| Path | What it is |
| --- | --- |
| `.github/workflows/agent.yml` | The workflow. |
| `scripts/run_agent.py` | The Claude Agent SDK app. It streams the agent loop, writes the report, and exits non-zero if the run didn't finish cleanly. |
| `CLAUDE.md` | Project guidance the agent loads on every run. |
| `agent-runs/` | Generated run reports. Don't edit these by hand. |
| `requirements.txt` | Its only dependency: `claude-agent-sdk`. |

## How a run works

1. You start **Actions → Claude Agent → Run workflow** and enter a prompt.
2. The job checks out the repo with full history, so the agent can read `git log` and `git diff`.
3. The Tailscale action joins the tailnet as `tag:ai-workflow-machine` and pings
   Aperture. If Aperture can't be reached, the job fails at this step.
4. An informational step lists the models Aperture offers this node. It warns
   if the requested model isn't in that list.
5. `run_agent.py` runs with `ANTHROPIC_BASE_URL=http://<aperture-host>`. Aperture
   matches the node's tag to a grant and forwards the request with your key.
6. The agent reads, edits, and runs commands in the checkout until it finishes
   or reaches `max_turns`.
7. The workflow commits everything as `github-actions[bot]`, including the
   report, and pushes to the branch. It skips this step if you unchecked `commit`.

## Setup

### 1. Add a tag and a grant to your Tailscale policy

Replace `ai` with your Aperture hostname:

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

- The grant must include both `role` and `models`. If `role` is missing,
  Aperture returns **403**.
- Narrow `models` to limit which models CI can use, for example `"anthropic/claude-haiku-4.5"`.
- `src` must name the tag exactly. Globs such as `tag:ai-*` don't match.

### 2. Create a Tailscale OAuth client

In the admin console, go to **Settings → OAuth clients** and create a client with:

- the **`auth_keys`** scope, with write access
- the tag **`tag:ai-workflow-machine`**, the same tag as in the policy

The workflow uses this client to create an ephemeral node, which is removed
when the job ends.

### 3. Add GitHub secrets and variables

```bash
gh secret set TS_OAUTH_CLIENT_ID
gh secret set TS_OAUTH_SECRET
gh variable set APERTURE_HOST --body ai   # optional, defaults to "ai"
```

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `TS_OAUTH_CLIENT_ID` | secret | — (required) | Tailscale OAuth client ID. |
| `TS_OAUTH_SECRET` | secret | — (required) | Tailscale OAuth client secret. |
| `APERTURE_HOST` | variable or secret | `ai` | Aperture's hostname on the tailnet. |
| `TS_HOSTNAME` | variable | `ai-workflow` | Device name of the ephemeral node. |
| `TS_TAG` | variable | `tag:ai-workflow-machine` | Tag for the node. If you change it, also change the policy and the OAuth client. |
| `WORKFLOW_PAT` | secret | — | Optional PAT with the `workflow` scope. See [Pushing](#pushing). |

### 4. Put the workflow on the default branch

GitHub shows `workflow_dispatch` workflows in the Actions tab only after they
are on the default branch.

### 5. Run it

Go to **Actions → Claude Agent → Run workflow** and enter a prompt, for example:

- `Write a REPORT.md explaining what this repo does.`
- `Add type hints to scripts/run_agent.py.`
- `Append a CHANGELOG entry covering the last 10 commits.`

## Workflow inputs

| Input | Default | Description |
| --- | --- | --- |
| `prompt` | required | The task. The agent decides its own steps. |
| `model` | `anthropic/claude-haiku-4.5` | Model ID. Aperture must serve it, and it must match your grant's `models` glob. |
| `max_turns` | `25` | Maximum number of agent turns. |
| `commit` | `true` | Uncheck for a dry run. The agent still runs and the report appears in the job log, but nothing is pushed. |

The job times out after 30 minutes.

## The agent script

`scripts/run_agent.py` configures the SDK as follows:

- **Tools.** `Read`, `Write`, `Edit`, `Glob`, `Grep`, and `Bash` are
  auto-approved under `acceptEdits`, because no one is present to approve
  prompts. Git writes and `gh` are listed in `disallowed_tools`, so they are
  denied in every permission mode.
- **Settings.** `setting_sources=["project"]` loads `CLAUDE.md` and `.claude/`
  from the checkout and ignores any user-level config on the runner.
- **System prompt.** The script uses Claude Code's preset system prompt and
  appends a note that the agent is running non-interactively in CI.
- **Gateway mode.** When `ANTHROPIC_BASE_URL` is set, the script sets
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` so that update checks and
  telemetry don't go around the gateway. If no credential is present, it sets
  the placeholder `ANTHROPIC_API_KEY=-`, because Claude Code won't start without
  a key. Aperture supplies the real one.

The script streams the agent loop to stdout, so the Actions log reads as a
transcript. The report contains the task, the agent's final message, changed
files, cost, token usage, and a collapsed transcript. The script exits `1` on
any result other than `success`, for example `error_max_turns`,
`error_max_budget_usd`, or `error_during_execution`.

### Running locally

From a machine on the tailnet:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_BASE_URL=http://ai
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md"
```

If you don't use Aperture, set `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`
instead (get the token from `claude setup-token`). Run locally, the script
writes a report but never commits.

| Flag | Default | Description |
| --- | --- | --- |
| `--prompt TEXT` | — | The task, given inline. |
| `--prompt-env NAME` | `AGENT_PROMPT` | Read the task from this env var. CI uses this to keep prompt text off the command line. |
| `--model ID` | `$AGENT_MODEL`, else Claude Code's default | Model alias or full ID. |
| `--max-turns N` | `25` | Maximum number of agent turns. |
| `--max-budget-usd X` | none | Stop when the client-side cost estimate reaches this amount. |
| `--permission-mode MODE` | `acceptEdits` | Claude Code permission mode. |
| `--report-dir DIR` | `agent-runs` | Directory for the report. Pass `""` to skip writing one. |

## Pushing

- **The commit step runs even when the agent fails.** The failure report still
  reaches the repo. The commit subject is `agent: <first line of the prompt>`.
  The body holds the full prompt, the agent's status, and the run URL.
- **Retries cover only a moved remote.** If the push is rejected because the
  remote has new commits, the step rebases and retries, up to three times. Any
  other rejection fails immediately.
- **Workflow files need a PAT.** `GITHUB_TOKEN` can never push changes under
  `.github/workflows/`. To let the agent edit workflows, set `WORKFLOW_PAT` to
  a PAT with the `workflow` scope. Checkout uses it for every later git
  command. Without the PAT, the commit step detects this rejection and explains
  it in the log.
- **Pushes made with `GITHUB_TOKEN` don't trigger other workflows.** GitHub
  blocks this to prevent recursion. To have the agent's commits start CI, push
  with a PAT, such as `WORKFLOW_PAT`, or a GitHub App token.

## Security

- **Aperture uses plain `http://`.** The tailnet already encrypts and
  authenticates the connection, and Aperture doesn't serve TLS there. Don't
  change the URL to `https://`.
- **Access is deny-by-default.** Grants can only add access, and there is no
  deny rule. To revoke access, edit or remove the grant.
- **Prompts can't inject shell commands.** Every `${{ inputs.* }}` reaches
  steps through `env:`, never directly inside a `run:` script, so `$(...)` in a
  prompt isn't executed.
- **`Bash` gives the agent broad access.** Only git writes and `gh` are
  blocked. For a read-and-edit-only agent, remove `Bash` from
  `DEFAULT_ALLOWED_TOOLS` in `scripts/run_agent.py`.
- **Partial edits can land on the branch.** Failed runs are committed too. Use
  a scratch branch, or replace the push step with a pull-request action such
  as `peter-evans/create-pull-request`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| The **Join the tailnet** step fails on `ping` | `APERTURE_HOST` is wrong, the OAuth client's tag doesn't match `TS_TAG`, or the grant is missing `tcp:80`. |
| **403** from Aperture | The grant is missing `{ "role": "user" }`. |
| **404** from Aperture | No `models` pattern matches the requested model, or no provider serves it. Check the model list in the "Show what Aperture offers" step. |
| Push rejected and the commit touches `.github/workflows/` | `GITHUB_TOKEN` can't push workflow files. Set `WORKFLOW_PAT`. |
| The agent's push didn't trigger CI | Expected when pushing with `GITHUB_TOKEN`. Push with a PAT or a GitHub App token. |

## Variations

- **Federated OIDC instead of an OAuth secret.** Pass `audience:` to the
  Tailscale action in place of `oauth-secret`, and add `id-token: write` to the
  workflow's `permissions`.
- **No SDK wheel for your runner's platform.** Install Claude Code natively.
  The SDK finds it on `PATH`.

## Further reading

- **Aperture:** [What it is](https://tailscale.com/docs/aperture/what-is-aperture) · [How it works](https://tailscale.com/docs/aperture/how-aperture-works) · [Get started](https://tailscale.com/docs/aperture/get-started) · [Grants](https://tailscale.com/docs/aperture/how-grants-work)
- **Aperture with Anthropic:** [Setup](https://tailscale.com/docs/aperture/how-to/use-anthropic) · [Claude Code](https://tailscale.com/docs/aperture/how-to/use-claude-code) · [GitHub Action](https://tailscale.com/docs/aperture/how-to/use-claude-code-action)
- **Tools:** [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action) · [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) · [Claude Code env vars](https://code.claude.com/docs/en/env-vars)
