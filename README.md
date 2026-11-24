# agent-workflow-github

A proof of concept: a **Claude Agent SDK** application that runs as a
**manually-triggered GitHub Actions workflow**, reaches Claude through
**Tailscale Aperture** over your tailnet, and **commits its output back to the
repository**.

No Anthropic API key or OAuth token lives in this repo. The runner joins your
tailnet as an ephemeral tagged node; Aperture identifies it by that tag and
injects the real provider key upstream.

```
workflow_dispatch(prompt)
  └─ checkout (full history)
     └─ tailscale/github-action  ──> ephemeral node "ai-workflow", tag:ai-workflow
        └─ pip install claude-agent-sdk
           └─ scripts/run_agent.py   ANTHROPIC_BASE_URL=http://ai
              │                      └─ Aperture: WhoIs(src IP) -> tag:ai-workflow
              │                         -> grant match -> injects Anthropic key
              └─ edits files, writes agent-runs/<ts>-<run>.md
                 └─ git commit && git push   (the workflow, never the agent)
```

## How the auth actually works

Aperture is a reverse proxy that runs as a node on your tailnet. Every tailnet
connection carries cryptographic proof of identity, so when a request arrives
Aperture asks Tailscale who owns the source IP and gets back a login name, a
device ID, and any tags. **The client never asserts its own identity** — a
forged header buys nothing.

A tagged node has no user account, so Aperture builds a synthetic identity from
its tags: it sorts them alphabetically and joins them with commas. Our runner
carries exactly one tag, so it shows up as `tag:ai-workflow`.

Access is **deny-by-default** and grants are **purely additive** — there is no
`deny` rule, so tightening access means editing or removing a grant. Network
reachability alone grants nothing: you need a `role` capability *and* a matching
`models` glob.

Three consequences for this workflow:

- **`ANTHROPIC_BASE_URL` is the only wiring.** Point it at `http://<aperture-host>`.
  It must be `http://`, not `https://` — Aperture serves plain HTTP on the tailnet.
- **A placeholder credential is still needed.** Aperture injects the real key, but
  the Claude Code binary refuses to start with no credential at all. `run_agent.py`
  sets `ANTHROPIC_API_KEY=-` for the subprocess automatically when a base URL is
  set and nothing else is — the same dash Tailscale's own docs use. If you *do*
  supply a real token (Aperture `auth_mode: "passthrough"`), the placeholder is
  skipped so your credential reaches upstream intact.
- **Nonessential traffic is disabled.** Update checks, telemetry, and feature-flag
  fetches go straight to `api.anthropic.com` and would bypass the gateway, so the
  app sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` in gateway mode.

## Setup

### 1. Create the tag and the grant in your tailnet policy

In the Tailscale admin console policy file:

```json
{
  "tagOwners": {
    "tag:ai-workflow": []
  },
  "grants": [
    {
      "src": ["tag:ai-workflow"],
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

Replace `ai` with your Aperture hostname. Both capability entries are required:
without `role`, Aperture returns **403** even when the `models` glob matches.
Narrow the glob (e.g. `"anthropic/claude-opus-5-5"`) to limit what CI can call.
`src` matching is exact — globs like `tag:ai-*` do not match. Policy changes
apply immediately.

> An empty `tagOwners` list means admins and network admins may apply the tag.

### 2. Create a Tailscale OAuth client

Admin console → **Settings → OAuth clients**. It needs the **`auth_keys`** write
scope, and **every tag on the client must match the tags the workflow requests**.
Tag it `tag:ai-workflow`.

Nodes created through OAuth are ephemeral: the action logs the node out as soon
as the job finishes and the coordination server deletes it.

### 3. Add the repository secrets and variables

```bash
gh secret set TS_OAUTH_CLIENT_ID
gh secret set TS_OAUTH_SECRET
gh variable set APERTURE_HOST --body ai      # your Aperture hostname
```

| Name | Kind | Required | Default |
| --- | --- | --- | --- |
| `TS_OAUTH_CLIENT_ID` | secret | yes | — |
| `TS_OAUTH_SECRET` | secret | yes | — |
| `APERTURE_HOST` | variable **or** secret | no | `ai` |
| `TS_HOSTNAME` | variable | no | `ai-workflow` |
| `TS_TAG` | variable | no | `tag:ai-workflow` |

`APERTURE_HOST` is read as `vars.APERTURE_HOST || secrets.APERTURE_HOST || 'ai'`,
so either kind works.

### 4. Push to GitHub

`workflow_dispatch` workflows only appear in the Actions tab once the file is on
the **default branch**.

```bash
git remote add origin git@github.com:<you>/agent-workflow-github.git
git push -u origin main
```

### 5. Run it

**Actions → Claude Agent → Run workflow**. Try:

- `Write a REPORT.md summarizing what this repo does, its layout, and how the workflow is wired.`
- `Add type hints and docstrings to scripts/run_agent.py where they're missing.`
- `Read the last 20 commits and append a Keep-a-Changelog entry to CHANGELOG.md.`

## Workflow inputs

| Input | Default | What it does |
| --- | --- | --- |
| `prompt` | _required_ | The task. Free-form; the agent plans its own steps. |
| `model` | `claude-opus-5-5` | Must be in Aperture's provider `models` list **and** match your grant's glob. Free text, because only your gateway knows what it serves. |
| `max_turns` | `25` | Hard cap on agentic turns, so a confused run can't spin. |
| `commit` | `true` | Uncheck for a dry run: the agent still works and reports, nothing is pushed. |

The **"Show what Aperture offers this node"** step curls `/v1/models` and prints
every model your tag can reach, then warns if the requested one isn't in the
list. It never fails the job — reachability is already proven by the action's
`ping` input, so a failure there is almost always a missing grant.

## Running it locally

Same code path, no Actions and no tailnet gymnastics — if your workstation is
already on the tailnet, point it at Aperture directly:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_BASE_URL=http://ai          # or: export ANTHROPIC_API_KEY=...
python scripts/run_agent.py --prompt "Summarize this repo in REPORT.md" --model claude-opus-5-5
```

The script refuses to start unless it has either a base URL or a credential. It
writes a report to `agent-runs/` and leaves committing to you.

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
- `env` carrying the gateway placeholders described above
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

**Model IDs are gateway-scoped.** Aperture resolves aliases and then checks the
requested model against your grant's glob. A **403** means no `role` grant; a
**404** means a role exists but no `models` pattern matched or no configured
provider serves that model.

**Federated OIDC removes the stored secret.** Instead of `oauth-secret`, pass
`audience: ${{ secrets.TS_AUDIENCE }}` and add `id-token: write` to the job's
permissions; the action then exchanges the workflow's GitHub OIDC token. Nodes
created that way are also pre-approved where device approval is in effect.

**No Node.js step needed.** The `claude-agent-sdk` platform wheel bundles the
Claude Code binary. On platforms where pip falls back to the source distribution
(e.g. ARM64 Windows) you'd install Claude Code natively and let the SDK find it
on `PATH`.

## References

- [What is Aperture?](https://tailscale.com/docs/aperture/what-is-aperture) · [How Aperture works](https://tailscale.com/docs/aperture/how-aperture-works) · [Get started](https://tailscale.com/docs/aperture/get-started)
- [Use Claude Code with Aperture](https://tailscale.com/docs/aperture/how-to/use-claude-code) · [Use Claude Code Action with Aperture](https://tailscale.com/docs/aperture/how-to/use-claude-code-action)
- [Set up Anthropic as a provider](https://tailscale.com/docs/aperture/how-to/use-anthropic) · [Grant model access](https://tailscale.com/docs/aperture/how-to/grant-model-access) · [How grants work](https://tailscale.com/docs/aperture/how-grants-work)
- [Tailscale GitHub Action](https://tailscale.com/kb/1276/tailscale-github-action)
- [Claude Agent SDK — Python reference](https://code.claude.com/docs/en/agent-sdk/python) · [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
