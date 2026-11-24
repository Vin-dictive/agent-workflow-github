# agent-workflow-github

Run a Claude agent against this repository from the GitHub Actions tab, and
have the workflow commit whatever it changed.

- **Manual only.** One `workflow_dispatch` workflow; you type a prompt, it runs.
- **No Anthropic credential in the repo.** The runner joins your tailnet as an
  ephemeral tagged node and talks to Claude through
  [Tailscale Aperture](https://tailscale.com/docs/aperture/what-is-aperture),
  which identifies the node by its tag and injects the real provider key.
- **The agent edits files; the workflow owns git.** The agent can't commit,
  push, reset, check out, or call `gh`. The workflow commits and pushes its
  changes afterwards.
- **Every run leaves a report** in `agent-runs/`, including failed runs.

This is a proof of concept, not a hardened product. Read
[Security notes](#security-notes) before you point it at anything important.

## How it works

```
workflow_dispatch(prompt, model, max_turns, commit)
  └─ actions/checkout            full history, so the agent can read git log/diff
     └─ tailscale/github-action  ephemeral node "ai-workflow", tag:ai-workflow-machine
        │                        pings Aperture; fails fast if it can't reach it
        └─ curl /v1/models       lists the models this tag can use (warning only)
           └─ scripts/run_agent.py   ANTHROPIC_BASE_URL=http://<aperture-host>
              │   Aperture: WhoIs(source IP) → tag:ai-workflow-machine
              │             → grant match → injects the Anthropic key upstream
              ├─ agent reads and edits the checkout
              └─ writes agent-runs/<timestamp>-<run_id>.md
                 └─ workflow: git commit && git push   (skipped if commit=false)
```

| Path | What it is |
| --- | --- |
| `.github/workflows/agent.yml` | The workflow. |
| `scripts/run_agent.py` | The Agent SDK app. Streams the agent loop, writes the report, and exits non-zero if the run didn't finish cleanly. |
| `requirements.txt` | `claude-agent-sdk`. The platform wheel includes the Claude Code binary, so you don't need Node. |
| `agent-runs/` | Generated run reports. Don't hand-edit them. |
| `CLAUDE.md` | Project guidance the agent loads on every run. |

## Setup

### 1. Add a tag and a grant to your tailnet policy

In the Tailscale admin console policy file (replace `ai` with your Aperture
hostname):

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

- **You need both capability entries.** Without `role`, Aperture returns
  **403** even when the `models` glob matches.
- To limit what CI can call, narrow the glob, e.g.
  `"anthropic/claude-opus-5-5"`.
- `src` must match exactly. A glob like `tag:ai-*` won't match anything.
- An empty `tagOwners` list means admins and network admins can apply the tag.
  Policy changes take effect immediately.

### 2. Create a Tailscale OAuth client

Go to **Settings → OAuth clients** in the admin console. Give the client the
**`auth_keys`** write scope and tag it `tag:ai-workflow-machine`. The client's
tags must match the tags the workflow requests. Nodes created this way are
ephemeral: they're removed as soon as the job ends.

### 3. Add repository secrets and variables

```bash
gh secret set TS_OAUTH_CLIENT_ID
gh secret set TS_OAUTH_SECRET
gh variable set APERTURE_HOST --body ai     # your Aperture hostname
```

| Name | Kind | Required | Default |
| --- | --- | --- | --- |
| `TS_OAUTH_CLIENT_ID` | secret | yes | — |
| `TS_OAUTH_SECRET` | secret | yes | — |
| `APERTURE_HOST` | variable or secret | no | `ai` |
| `TS_HOSTNAME` | variable | no | `ai-workflow` |
| `TS_TAG` | variable | no | `tag:ai-workflow-machine` |
| `WORKFLOW_PAT` | secret | only to let the agent edit `.github/workflows/` | — |

If you change `TS_TAG`, change the policy and the OAuth client to match.

### 4. Push to the default branch

GitHub only shows a `workflow_dispatch` workflow in the Actions tab once the
file is on the default branch.

### 5. Run it

Go to **Actions → Claude Agent → Run workflow**. Some prompts to try:

- `Write a REPORT.md summarizing what this repo does and how the workflow is wired.`
- `Add type hints and docstrings to scripts/run_agent.py where they're missing.`
- `Read the last 20 commits and append a Keep-a-Changelog entry to CHANGELOG.md.`

## Workflow inputs

| Input | Default | Description |
| --- | --- | --- |
| `prompt` | _required_ | The task, in free form. The agent plans its own steps. |
| `model` | `anthropic/claude-haiku-4.5` | Must be served by Aperture **and** match your grant's `models` glob. It's free text because only your gateway knows which models it serves. |
| `max_turns` | `25` | Hard cap on agentic turns. |
| `commit` | `true` | Uncheck for a dry run: the agent still runs and reports, but nothing gets pushed. |

The job has a 30-minute timeout. Runs on the same ref are queued, not run in
parallel, so two agents can't race on the same push.

## Running locally

If your machine is already on the tailnet, you can point the script straight at
Aperture:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_BASE_URL=http://ai
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md"
```

Without Aperture, set `ANTHROPIC_API_KEY`, or set `CLAUDE_CODE_OAUTH_TOKEN`
(from `claude setup-token`), instead of `ANTHROPIC_BASE_URL`. The script won't
start unless one of these is set. It writes a report to `agent-runs/` and leaves
committing to you.

### `run_agent.py` options

| Flag | Default | Description |
| --- | --- | --- |
| `--prompt TEXT` | — | The task, inline. |
| `--prompt-env NAME` | `AGENT_PROMPT` | Read the task from this env var instead. CI uses this to keep prompt text out of the shell. |
| `--model ID` | `$AGENT_MODEL`, else Claude Code's default | Model alias or full ID. |
| `--max-turns N` | `25` | Hard cap on agentic turns. |
| `--max-budget-usd X` | none | Stop once the client-side cost estimate reaches this amount. |
| `--permission-mode MODE` | `acceptEdits` | Claude Code permission mode. |
| `--report-dir DIR` | `agent-runs` | Where reports go, relative to the repo root. Pass `""` to skip the report. |

## What the script configures

`run_agent.py` calls `query()` from `claude_agent_sdk` with:

- **`cwd`** set to the repo root, so tools operate on the checkout.
- **`permission_mode="acceptEdits"`** plus an `allowed_tools` allowlist
  (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`). Nobody is around in CI to
  answer a permission prompt.
- **`disallowed_tools`** blocking `git commit`, `git push`, `git reset`,
  `git checkout`, and `gh`. These rules apply in every permission mode.
- **`setting_sources=["project"]`**, so it loads `CLAUDE.md` and `.claude/` from
  the checkout and ignores any user-level config on the runner.
- **`system_prompt`**: the `claude_code` preset, with a short note about the CI
  context appended.
- **Gateway environment.** When `ANTHROPIC_BASE_URL` is set, the script:
  - sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, because update checks and
    telemetry go straight to `api.anthropic.com` and would bypass the gateway.
  - sets `ANTHROPIC_API_KEY=-` as a placeholder if no real credential is
    present. Claude Code won't start without some credential, and Aperture
    replaces it anyway. If you do supply a real token (Aperture
    `auth_mode: "passthrough"`), the placeholder is skipped.

The script streams the run to stdout, so the Actions log reads like a
transcript. It writes a step summary and a Markdown report covering the task,
the agent's closing message, changed files, cost and tokens, and a collapsed
transcript. It exits `1` on `error_during_execution`, `error_max_turns`, or
`error_max_budget_usd`.

The commit step runs as `github-actions[bot]` under `!cancelled()`, so a failed
run still commits its report. It uses the first line of the prompt as the commit
subject. If the push is rejected, it checks why: it rebases and retries, up to
three attempts, only when the remote moved on. Any other rejection fails
immediately with the remote's own message, because a rebase won't fix it.

**`GITHUB_TOKEN` can never push changes under `.github/workflows/`.** There is no
`workflows` key in the `permissions:` block, so no setting grants it — GitHub
blocks it by design, because a token issued to a run could otherwise rewrite the
automation that decides what runs with which secrets. The rejection arrives
server-side, sometimes as a bare `Internal Server Error`. If the agent needs to
edit workflow files, set `WORKFLOW_PAT` (a PAT with the `workflow` scope); it's
wired into `actions/checkout`, which is the only place that matters, because
checkout writes the credential into `.git/config` and every later git command
inherits it. Otherwise keep workflow edits out of agent runs — the commit step
detects this case and says so instead of retrying.

**Pushes made with `GITHUB_TOKEN` don't trigger other workflows.** Deliberate, on
GitHub's side, to stop recursive runs. If you want the agent's commit to kick off
CI, push with a GitHub App installation token or a PAT instead.

## Security notes

- **Aperture must be reached over `http://`.** Aperture doesn't serve TLS on
  the tailnet; the tailnet itself encrypts and authenticates the connection.
  The client never asserts its own identity, so a forged header gets an
  attacker nothing.
- **Access is deny-by-default and grants only add access.** There's no `deny`
  rule, so to tighten access you edit or remove a grant.
- **Prompt text never ends up in a `run:` string.** Every `${{ inputs.* }}`
  reference goes through `env:`. If it were interpolated straight into shell,
  `$(...)` in a prompt would execute on the runner.
- **The agent has `Bash`.** The deny rules block git writes and `gh`, but
  that's still a broad tool surface. For read-and-edit only, drop `Bash` from
  `DEFAULT_ALLOWED_TOOLS` in `scripts/run_agent.py`.
- **Failed runs are committed too**, so a partial edit can land on the branch.
  Point the workflow at a scratch branch, or replace the push with a PR (for
  example `peter-evans/create-pull-request`, or push to `agent/<run_id>` and
  open a PR).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| "Join the tailnet" fails on `ping` | Wrong `APERTURE_HOST`, the OAuth client's tag doesn't match `TS_TAG`, or the grant is missing `tcp:80`. |
| `/v1/models` warning, or **403** from Aperture | The grant has no `{ "role": "user" }` for the tag. |
| **404** from Aperture | There's a role, but no `models` pattern matches the requested model, or no configured provider serves it. |
| The agent's push didn't trigger CI | Expected. GitHub doesn't trigger other workflows from pushes made with `GITHUB_TOKEN`. To trigger CI, push with a GitHub App token or a PAT. |

## Variations

- **Federated OIDC instead of an OAuth secret:** pass
  `audience: ${{ secrets.TS_AUDIENCE }}` to the Tailscale action in place of
  `oauth-secret`, and add `id-token: write` to the job's permissions.
- **Platforms with no SDK wheel** (e.g. ARM64 Windows): install Claude Code
  natively. The SDK finds it on `PATH`.

## References

- Aperture: [What it is](https://tailscale.com/docs/aperture/what-is-aperture) · [How it works](https://tailscale.com/docs/aperture/how-aperture-works) · [Get started](https://tailscale.com/docs/aperture/get-started) · [How grants work](https://tailscale.com/docs/aperture/how-grants-work) · [Grant model access](https://tailscale.com/docs/aperture/how-to/grant-model-access)
- Aperture with Anthropic: [Provider setup](https://tailscale.com/docs/aperture/how-to/use-anthropic) · [Claude Code](https://tailscale.com/docs/aperture/how-to/use-claude-code) · [Claude Code Action](https://tailscale.com/docs/aperture/how-to/use-claude-code-action)
- [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action)
- [Claude Agent SDK: Python reference](https://code.claude.com/docs/en/agent-sdk/python) · [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
