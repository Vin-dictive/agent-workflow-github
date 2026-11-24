#!/usr/bin/env python3
"""Run a one-shot Claude Agent SDK task against this repository.

The agent works directly in the checkout: it can read, write, and edit files and
run shell commands. When the run finishes this script writes a Markdown report
under ``agent-runs/`` so every run leaves a reviewable artifact, even when the
agent decides no source changes are needed.

Committing and pushing is deliberately *not* done here. The GitHub Actions
workflow owns that, which keeps the agent itself from pushing to the repo.

Auth comes from the environment. Three shapes work:

* ``ANTHROPIC_BASE_URL`` - route through an LLM gateway such as Tailscale
  Aperture, which resolves the caller's identity from the tailnet and injects
  the real provider key. No credential lives in the repo at all.
* ``CLAUDE_CODE_OAUTH_TOKEN`` - long-lived subscription token from
  ``claude setup-token``
* ``ANTHROPIC_API_KEY`` - Claude API key
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

# Auto-approved so the run never stalls waiting for a prompt that nobody can answer.
DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# Denied in every permission mode. The workflow owns history and remotes; the
# agent only produces the working-tree changes.
DEFAULT_DISALLOWED_TOOLS = [
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git reset:*)",
    "Bash(git checkout:*)",
    "Bash(gh:*)",
]

SYSTEM_PROMPT_APPEND = """
You are running non-interactively inside a GitHub Actions job, in a fresh
checkout of the repository. Nobody can answer questions mid-run.

- Make the changes the task asks for by editing files in the working tree.
- Everything you change will be committed and pushed by the workflow, so keep
  diffs focused and leave the tree in a state you would open a PR with.
- Do not run `git commit`, `git push`, `git reset`, or `gh`; those are blocked.
  Read-only git (`git log`, `git diff`, `git status`) is fine and useful.
- End your final message with a short summary of what you changed and why. That
  text is what a human reviewer reads first.
""".strip()

TRANSCRIPT_ENTRY_LIMIT = 400
TRANSCRIPT_MAX_ENTRIES = 200


@dataclass
class Config:
    prompt: str
    model: str | None
    max_turns: int | None
    max_budget_usd: float | None
    permission_mode: str
    report_dir: Path | None
    repo_root: Path
    base_url: str | None = None

    @property
    def via_gateway(self) -> bool:
        return self.base_url is not None


@dataclass
class Outcome:
    result: ResultMessage | None
    transcript: list[str] = field(default_factory=list)
    final_text: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.subtype == "success" and not self.result.is_error


def log(line: str = "") -> None:
    print(line, flush=True)


def git(repo_root: Path, *args: str, strip: bool = True) -> str:
    """Run a read-only git command, returning stdout (empty string on failure).

    Pass ``strip=False`` for output whose leading whitespace is significant,
    such as ``status --porcelain`` status codes.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return done.stdout.strip() if strip else done.stdout


def describe_tool_call(block: object) -> str:
    """One-line description of a tool-use block, for the log and transcript."""
    name = getattr(block, "name", "tool")
    raw = getattr(block, "input", None)
    args = raw if isinstance(raw, dict) else {}

    for key in ("file_path", "path", "command", "pattern", "url", "prompt"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            if len(detail) > 160:
                detail = detail[:157] + "..."
            return f"{name}: {detail}"
    return name


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--prompt", help="Task for the agent, passed inline.")
    source.add_argument(
        "--prompt-env",
        default="AGENT_PROMPT",
        metavar="NAME",
        help="Env var holding the task (default: AGENT_PROMPT). Preferred in CI: "
        "it keeps untrusted prompt text out of the shell command line.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENT_MODEL") or None,
        help="Model alias or full ID, e.g. claude-opus-5. Omit for Claude Code's default.",
    )
    parser.add_argument("--max-turns", type=int, default=25, help="Hard cap on agentic turns (default: 25).")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="Stop once the client-side cost estimate reaches this value. "
        "Ignored when authenticating with a subscription token.",
    )
    parser.add_argument(
        "--permission-mode",
        default="acceptEdits",
        choices=["default", "acceptEdits", "dontAsk", "bypassPermissions", "plan"],
        help="Claude Code permission mode (default: acceptEdits).",
    )
    parser.add_argument(
        "--report-dir",
        default="agent-runs",
        help="Directory for the Markdown run report, relative to the repo root. "
        "Pass an empty string to skip the report.",
    )
    args = parser.parse_args(argv)

    prompt = args.prompt if args.prompt is not None else os.environ.get(args.prompt_env, "")
    prompt = prompt.strip()
    if not prompt:
        where = "--prompt" if args.prompt is not None else f"${args.prompt_env}"
        parser.error(f"no task given ({where} was empty)")

    base_url = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip() or None
    has_credential = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    if not base_url and not has_credential:
        parser.error(
            "no way to reach Claude: set ANTHROPIC_BASE_URL to an LLM gateway "
            "(e.g. http://ai for Tailscale Aperture), or set CLAUDE_CODE_OAUTH_TOKEN "
            "(from `claude setup-token`) or ANTHROPIC_API_KEY"
        )

    repo_root = Path(__file__).resolve().parent.parent
    return Config(
        prompt=prompt,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        permission_mode=args.permission_mode,
        report_dir=(repo_root / args.report_dir) if args.report_dir else None,
        repo_root=repo_root,
        base_url=base_url,
    )


def gateway_env(cfg: Config) -> dict[str, str]:
    """Extra environment for the Claude Code subprocess when routing via a gateway."""
    if not cfg.via_gateway:
        return {}
    env = {
        # Update checks, telemetry, and feature-flag fetches talk to
        # api.anthropic.com directly and would bypass the gateway entirely.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        # Aperture derives the caller from the tailnet and injects the real
        # provider key, so no client credential is needed -- but Claude Code
        # still refuses to start with none at all. A dash is the placeholder
        # Tailscale's own Claude Code and claude-code-action docs use.
        env["ANTHROPIC_API_KEY"] = "-"
    return env


async def run_agent(cfg: Config) -> Outcome:
    options = ClaudeAgentOptions(
        cwd=str(cfg.repo_root),
        model=cfg.model,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        permission_mode=cfg.permission_mode,
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
        # Keep Claude Code's own system prompt and tool guidance, with CI context appended.
        system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_PROMPT_APPEND},
        # Load CLAUDE.md and .claude/settings.json from the checkout; ignore whatever
        # user-level config happens to exist on the runner.
        setting_sources=["project"],
        env=gateway_env(cfg),
        stderr=lambda line: print(f"[cli] {line}", file=sys.stderr, flush=True),
    )

    outcome = Outcome(result=None)

    async for message in query(prompt=cfg.prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if not text:
                        continue
                    log(text)
                    outcome.final_text = text
                    outcome.transcript.append(f"**Claude:** {text[:TRANSCRIPT_ENTRY_LIMIT]}")
                elif hasattr(block, "name"):
                    detail = describe_tool_call(block)
                    log(f"  -> {detail}")
                    outcome.transcript.append(f"`{detail[:TRANSCRIPT_ENTRY_LIMIT]}`")
        elif isinstance(message, ResultMessage):
            outcome.result = message

    del outcome.transcript[TRANSCRIPT_MAX_ENTRIES:]
    return outcome


def fence(body: str, char: str = "~") -> str:
    """Fence a block of text, widening the fence past anything inside it."""
    longest = 0
    run = 0
    for ch in body:
        run = run + 1 if ch == char else 0
        longest = max(longest, run)
    marker = char * max(3, longest + 1)
    return f"{marker}\n{body}\n{marker}"


def build_report(cfg: Config, outcome: Outcome, changed: list[str], started_at: datetime) -> str:
    result = outcome.result
    status = result.subtype if result else "no-result"
    cost = f"${result.total_cost_usd:.4f}" if result and result.total_cost_usd is not None else "n/a"
    usage = result.usage if result and result.usage else {}

    rows = [
        ("Started", started_at.strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("Status", f"`{status}`"),
        ("Model", f"`{cfg.model or 'default'}`"),
        ("Endpoint", f"`{cfg.base_url}`" if cfg.via_gateway else "`api.anthropic.com`"),
        ("Turns", str(result.num_turns) if result else "n/a"),
        ("Duration", f"{result.duration_ms / 1000:.1f}s" if result else "n/a"),
        ("Cost", cost),
        ("Input tokens", str(usage.get("input_tokens", "n/a"))),
        ("Output tokens", str(usage.get("output_tokens", "n/a"))),
    ]
    if run_url := os.environ.get("AGENT_RUN_URL"):
        rows.append(("Workflow run", f"[{os.environ.get('GITHUB_RUN_ID', 'log')}]({run_url})"))
    if actor := os.environ.get("GITHUB_ACTOR"):
        rows.append(("Triggered by", f"@{actor}"))

    parts = [
        f"# Agent run {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "| | |",
        "| --- | --- |",
        *(f"| {label} | {value} |" for label, value in rows),
        "",
        "## Task",
        "",
        fence(cfg.prompt),
        "",
        "## What the agent reported",
        "",
        outcome.final_text or "_The agent produced no closing message._",
        "",
        "## Files changed",
        "",
    ]
    parts += [f"- `{path}`" for path in changed] if changed else ["_No files were modified._"]

    if result and result.errors:
        parts += ["", "## Errors", ""] + [f"- {err}" for err in result.errors]

    parts += ["", "## Transcript", "", "<details>", "<summary>Tool calls and messages</summary>", ""]
    parts += [f"- {entry}" for entry in outcome.transcript] or ["_empty_"]
    parts += ["", "</details>", ""]
    return "\n".join(parts)


def changed_files(repo_root: Path) -> list[str]:
    """Paths git sees as modified, added, or deleted, before the report is written."""
    # Status codes occupy the first two columns, so the output must not be
    # stripped; quotePath=false keeps non-ASCII paths readable.
    porcelain = git(repo_root, "-c", "core.quotePath=false", "status", "--porcelain", strip=False)
    paths = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: "old" -> "new"
            path = path.split(" -> ", 1)[1]
        if path:
            paths.append(path.strip('"'))
    return sorted(paths)


def write_step_summary(cfg: Config, outcome: Outcome, changed: list[str], report_path: Path | None) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    result = outcome.result
    lines = [
        f"### {'✅' if outcome.ok else '❌'} Claude agent run",
        "",
        f"**Task:** {' '.join(cfg.prompt.split())[:300]}",
        "",
        f"- Status: `{result.subtype if result else 'no-result'}`",
        f"- Model: `{cfg.model or 'default'}`",
        f"- Turns: {result.num_turns if result else 'n/a'}",
        f"- Files changed: {len(changed)}",
    ]
    if result and result.total_cost_usd is not None:
        lines.append(f"- Cost: ${result.total_cost_usd:.4f}")
    if report_path:
        lines.append(f"- Report: `{report_path.name}`")
    if changed:
        lines += ["", "<details><summary>Changed files</summary>", ""]
        lines += [f"- `{path}`" for path in changed]
        lines += ["", "</details>"]
    if outcome.final_text:
        lines += ["", "---", "", outcome.final_text]
    with open(summary_file, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    started_at = datetime.now(timezone.utc)

    log(f"Repo:   {cfg.repo_root}")
    log(f"Model:  {cfg.model or 'Claude Code default'}")
    log(f"Via:    {cfg.base_url + ' (gateway)' if cfg.via_gateway else 'api.anthropic.com'}")
    log(f"Mode:   {cfg.permission_mode} (max {cfg.max_turns} turns)")
    log(f"Task:   {' '.join(cfg.prompt.split())[:200]}")
    log("-" * 72)

    outcome = asyncio.run(run_agent(cfg))

    log("-" * 72)
    # Collect this before writing the report so the report itself isn't in the list.
    changed = changed_files(cfg.repo_root)

    report_path = None
    if cfg.report_dir:
        cfg.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        report_path = cfg.report_dir / f"{stamp}-{run_id}.md"
        report_path.write_text(build_report(cfg, outcome, changed, started_at), encoding="utf-8")
        log(f"Report: {report_path.relative_to(cfg.repo_root)}")

    write_step_summary(cfg, outcome, changed, report_path)

    result = outcome.result
    log(f"Status: {result.subtype if result else 'no result message'}")
    log(f"Changed {len(changed)} file(s): {', '.join(changed) or 'none'}")
    if result and result.total_cost_usd is not None:
        log(f"Cost:   ${result.total_cost_usd:.4f} over {result.num_turns} turn(s)")

    if not outcome.ok:
        detail = json.dumps(
            {
                "subtype": result.subtype if result else None,
                "is_error": result.is_error if result else None,
                "terminal_reason": result.terminal_reason if result else None,
                "api_error_status": result.api_error_status if result else None,
                "errors": result.errors if result else None,
            },
            indent=2,
        )
        print(f"agent run did not complete cleanly:\n{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
