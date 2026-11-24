# agent-workflow-github

Run a Claude agent against this repository from the GitHub **Actions** tab.
Type a prompt; the agent edits the checkout; the workflow commits and pushes the
result together with a Markdown report of the run.

No Anthropic credential is stored anywhere in the repo. The runner joins your
Tailscale tailnet as an ephemeral, tagged node and talks to Claude through a
[Tailscale Aperture](https://tailscale.com/docs/aperture/what-is-aperture)
gateway, which recognises the node by its tag and injects the real API key
upstream.

> **Proof of concept.** Read [Security](#security) before pointing it at
> anything that matters.

## Contents

- [What's in the repo](#whats-in-the-repo)
- [How a run works](#how-a-run-works)
- [Setup](#setup)
- [Running the workflow](#running-the-workflow)
- [The agent script](#the-agent-script)
- [Pushing](#pushing)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Variations](#variations)
- [Further reading](#further-reading)

## What's in the repo

| Path | Purpose |
| --- | --- |
| `.github/workflows/agent.yml` | The manually triggered (`workflow_dispatch`) workflow. |
| `scripts/run_agent.py` | The Claude Agent SDK app: streams the agent loop, writes the report, exits non-zero if the run didn't finish cleanly. |
| `CLAUDE.md` | Project guidance the agent loads on every run. |
| `agent-runs/` | One generated report per run. Don't edit these by hand. |
| `requirements.txt` | The only dependency, `claude-agent-sdk`, which bundles the Claude Code binary. |

## How a run works

1. You start **Actions → Claude Agent → Run workflow** and enter a prompt.
   Only one run per branch executes at a time.
2. The repo is checked out with full history, so the agent can use `git log`
   and `git diff`.
3. The runner joins the tailnet as `tag:ai-workflow-machine` and pings
   Aperture. If Aperture is unreachable the job stops here.
4. An informational step lists the models Aperture offers this node and warns
   if the requested model isn't among them.
5. `scripts/run_agent.py` runs with `ANTHROPIC_BASE_URL=http://<aperture-host>`.
   Aperture matches the node's tag to a grant and forwards each request with
   your key.
6. The agent reads, edits, and runs commands in the checkout until it finishes
   or hits `max_turns`. It cannot commit, push, reset, check out, or call `gh`.
7. The workflow commits everything, report included, as `github-actions[bot]`
   and pushes to the branch, unless you unchecked `commit`.

## Setup

### 1. Tailscale policy: a tag and a grant

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

- Include both `role` and `models`. Without `role`, Aperture returns **403**.
- Narrow `models` to restrict what CI can use, e.g. `"anthropic/claude-haiku-4.5"`.
- `src` must name the tag exactly; globs such as `tag:ai-*` don't match.

### 2. Tailscale OAuth client

Under **Settings → OAuth clients** in the admin console, create a client with:

- the **`auth_keys`** scope (write), and
- the tag **`tag:ai-workflow-machine`** — the same tag as in the policy.

The workflow uses it to mint an ephemeral node that disappears when the job ends.

### 3. GitHub secrets and variables

```bash
gh secret set TS_OAUTH_CLIENT_ID
gh secret set TS_OAUTH_SECRET
gh variable set APERTURE_HOST --body ai   # optional; defaults to "ai"
```

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `TS_OAUTH_CLIENT_ID` | secret | required | Tailscale OAuth client ID. |
| `TS_OAUTH_SECRET` | secret | required | Tailscale OAuth client secret. |
| `APERTURE_HOST` | variable or secret | `ai` | Aperture's hostname on the tailnet. |
| `TS_HOSTNAME` | variable | `ai-workflow` | Device name for the ephemeral node. |
| `TS_TAG` | variable | `tag:ai-workflow-machine` | Node tag. Change the policy and OAuth client to match. |
| `WORKFLOW_PAT` | secret | — | Optional PAT with the `workflow` scope. See [Pushing](#pushing). |

### 4. Merge the workflow to the default branch

GitHub lists `workflow_dispatch` workflows in the Actions tab only once they
exist on the default branch.

## Running the workflow

Go to **Actions → Claude Agent → Run workflow**, pick a branch, and fill in:

| Input | Default | Description |
| --- | --- | --- |
| `prompt` | required | The task. The agent plans its own steps. |
| `model` | `anthropic/claude-haiku-4.5` | Must be served by Aperture and match your grant's `models` glob. |
| `max_turns` | `25` | Cap on agent turns. |
| `commit` | `true` | Uncheck for a dry run: the agent runs and reports in the job log, but nothing is pushed. |

Example prompts:

- `Write a REPORT.md explaining what this repo does.`
- `Add type hints to scripts/run_agent.py.`
- `Append a CHANGELOG entry covering the last 10 commits.`

The job times out after 30 minutes. Besides the committed report, each run
writes a short summary (status, model, turns, cost, changed files, the agent's
closing message) to the job's summary page.

## The agent script

How `scripts/run_agent.py` configures the SDK:

- **Tools.** `Read`, `Write`, `Edit`, `Glob`, `Grep`, and `Bash` are
  auto-approved (permission mode `acceptEdits`) because nobody is there to
  answer prompts. `git commit`, `git push`, `git reset`, `git checkout`, and
  `gh` are in `disallowed_tools`, which applies in every permission mode.
- **Settings.** `setting_sources=["project"]` loads `CLAUDE.md` and `.claude/`
  from the checkout and ignores any user-level config on the runner.
- **System prompt.** Claude Code's preset prompt, plus a note that the agent is
  running non-interactively in CI and should end with a summary of its changes.
- **Gateway mode.** When `ANTHROPIC_BASE_URL` is set, the script sets
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` so update checks and telemetry
  don't bypass the gateway. If no credential is present it also sets the
  placeholder `ANTHROPIC_API_KEY=-`, since Claude Code won't start without one;
  Aperture supplies the real key.

The agent loop streams to stdout, so the Actions log reads as a transcript.
The report in `agent-runs/<timestamp>-<run id>.md` records the task, the
agent's closing message, changed files, any errors, cost, token usage, and a
collapsed transcript. The script exits `1` on any result other than `success`
(for example `error_max_turns` or `error_during_execution`).

### Running locally

From a machine on the tailnet:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_BASE_URL=http://ai
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md"
```

Without Aperture, set `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` from
`claude setup-token`, instead. Locally the script writes a report (named
`<timestamp>-local.md`) but never commits.

| Flag | Default | Description |
| --- | --- | --- |
| `--prompt TEXT` | — | The task, inline. |
| `--prompt-env NAME` | `AGENT_PROMPT` | Read the task from this env var instead. CI uses it to keep prompt text off the command line. |
| `--model ID` | `$AGENT_MODEL`, else Claude Code's default | Model alias or full ID. |
| `--max-turns N` | `25` | Cap on agent turns. |
| `--max-budget-usd X` | none | Stop once the client-side cost estimate reaches `X`. Ignored with a subscription token. |
| `--permission-mode MODE` | `acceptEdits` | Claude Code permission mode. |
| `--report-dir DIR` | `agent-runs` | Where to write the report. `""` skips it. |

`--prompt` and `--prompt-env` are mutually exclusive.

## Pushing

- **Failed runs are committed too**, so the failure report reaches the repo.
  The commit subject is `agent: <first line of the prompt>`; the body holds the
  full prompt, the agent's status, and the run URL. Runs that change nothing
  produce no commit.
- **Only a moved remote is retried.** If the push is rejected because someone
  else pushed meanwhile, the step rebases and retries, up to three times. Any
  other rejection fails immediately with the original message.
- **Workflow files need a PAT.** `GITHUB_TOKEN` can never push changes under
  `.github/workflows/`. To let the agent edit workflows, set `WORKFLOW_PAT` to a
  PAT with the `workflow` scope; checkout stores it for every later git
  command. Without it, the commit step recognises this rejection and explains
  it in the log.
- **`GITHUB_TOKEN` pushes don't trigger other workflows.** GitHub blocks this to
  prevent recursion. Push with a PAT (such as `WORKFLOW_PAT`) or a GitHub App
  token if the agent's commits should start CI.

## Security

- **Aperture is plain `http://` on purpose.** The tailnet already encrypts and
  authenticates the connection, and Aperture doesn't serve TLS there. Don't
  change the URL to `https://`.
- **Access is deny-by-default.** Grants only add access; there is no deny rule.
  To revoke CI's access, edit or remove the grant.
- **Prompts can't inject shell commands.** Every `${{ inputs.* }}` reaches steps
  through `env:`, never inside a `run:` script, so `$(...)` in a prompt is
  never executed.
- **The git block is a guardrail, not a sandbox.** The blocked commands are
  matched by prefix, and `actions/checkout` leaves the push token (including
  `WORKFLOW_PAT`, if set) in `.git/config`, which `Bash` can read. Treat
  anyone who can trigger the workflow as able to push to the repo. For a
  read-and-edit-only agent, remove `Bash` from `DEFAULT_ALLOWED_TOOLS` in
  `scripts/run_agent.py`.
- **Partial edits can land on the branch.** Run against a scratch branch, or
  replace the push step with a pull-request action such as
  `peter-evans/create-pull-request`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| **Join the tailnet** fails on `ping` | Wrong `APERTURE_HOST`, the OAuth client's tag doesn't match `TS_TAG`, or the grant lacks `tcp:80`. |
| **403** from Aperture | The grant is missing `{ "role": "user" }`. |
| **404** from Aperture | No `models` pattern matches the requested model, or no provider serves it. Check the list in **Show what Aperture offers this node**. |
| Push rejected on a commit touching `.github/workflows/` | `GITHUB_TOKEN` can't push workflow files. Set `WORKFLOW_PAT`. |
| The agent's push didn't trigger CI | Expected with `GITHUB_TOKEN`. Push with a PAT or GitHub App token. |
| Run ends with `error_max_turns` | Raise `max_turns`, or split the task into smaller prompts. |

## Variations

- **Federated OIDC instead of an OAuth secret.** Pass `audience:` to the
  Tailscale action in place of `oauth-secret`, and add `id-token: write` to the
  workflow's `permissions`.
- **No SDK wheel for your runner's platform.** Install Claude Code natively;
  the SDK picks it up from `PATH`.

## Further reading

- **Aperture:** [What it is](https://tailscale.com/docs/aperture/what-is-aperture) · [How it works](https://tailscale.com/docs/aperture/how-aperture-works) · [Get started](https://tailscale.com/docs/aperture/get-started) · [Grants](https://tailscale.com/docs/aperture/how-grants-work)
- **Aperture with Anthropic:** [Setup](https://tailscale.com/docs/aperture/how-to/use-anthropic) · [Claude Code](https://tailscale.com/docs/aperture/how-to/use-claude-code) · [GitHub Action](https://tailscale.com/docs/aperture/how-to/use-claude-code-action)
- **Tools:** [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action) · [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) · [Claude Code env vars](https://code.claude.com/docs/en/env-vars)
