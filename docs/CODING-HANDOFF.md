# Coding handoff: switch models mid-work without losing the coding

You are coding with one model and want to continue with another, for example:

- Claude Opus 4.8 → Claude Opus 5.5 in Claude Code
- GPT-5.6 → GPT-6.1 in the Codex shell
- Claude → GPT, or one chat to a brand-new chat the next day

Chat history does not survive the switch, and it shouldn't be trusted anyway. Continuum keeps the **state of the coding work** outside the chat, in PostgreSQL, and hands the next model a brief built from facts it can check.

## What carries over

| In the brief | Where it comes from | Trust |
|---|---|---|
| Current code: git HEAD, branch, changed files, content fingerprint | observed by Continuum **at resume time** | fact |
| **Code touched in this session:** files, +/- lines, enclosing functions, names defined | observed from git since the session began | fact |
| Commits since the last checkpoint | observed from git | fact |
| Which checks passed, on which code, with which definition | `CheckRan` events, compared with the code now | fact |
| Tasks, and whether each is **verified** or needs re-verification | evidence fingerprints vs the code now | fact |
| Checks registered by a human, and whether any changed this session | `coding_checks` | fact |
| Decisions, context, blockers and next steps | notes recorded by previous agents, kept whole | claim |
| Checkpoints (what was being done, by which model) | agent summary plus the observed state | claim plus fact |

The code itself is already on disk (or in git); Continuum doesn't copy it. What would normally be lost is *why* the code looks like this, what is proven, what is half-done and what comes next. That is what Continuum keeps. The code map tells the next model **where** the session's work is, so it reads those files instead of exploring the repository.

Continuum calls no LLM. The brief is the agent's own notes plus facts observed from git and check runs, assembled by plain code, so a handoff costs only the next model reading about 1–3k tokens.

It works for any kind of coding, not only backend work: a frontend, data pipelines, infrastructure, docs, notebooks, a game or a firmware project. Inside git it fingerprints the **content** of the working copy (tracked files on disk plus untracked, non-ignored files), so a commit of the same content is not a change but any edit is. Outside git it hashes the files. Checks are any command you would run yourself: `pytest -q`, `npm test`, `cargo test`, `terraform validate`, `make lint`.

## One-time setup (per repository, by a human)

```bash
continuum coding checks add . tests --actor alice -- pytest -q
continuum coding checks add . lint  --actor alice -- ruff check .
continuum coding checks list .
```

Only people register checks. There is no MCP tool for it, so a model can't loosen the bar for "done". If a check's definition changes later, every task it verified needs re-verification, and the brief flags the change.

Then add the rules from [`integrations/AGENT_RULES.md`](../integrations/AGENT_RULES.md) to your repository's `AGENTS.md` (Codex, Cursor and others) or `CLAUDE.md` (Claude Code). The rules tell every model to resume first and leave a trail.

## While coding (the agent does this, following the rules)

| Step | MCP tool | CLI equivalent (shell-only agents) |
|---|---|---|
| Start of every chat | `coding_resume(repo_path, agent)` | `continuum coding resume --repo . --agent <model>` |
| No session yet | `coding_start(repo_path, goal, agent, tasks)` | `continuum coding start . --goal "…" --agent <model>` |
| Plan work | `coding_add_task(…, checks=["tests"])` | `continuum coding task add SESSION "title" --check tests --agent <model>` |
| Record why | `coding_note(kind=decision\|context\|blocker\|next_step)` | `continuum coding note SESSION decision "…" --agent <model>` |
| After meaningful edits | `coding_checkpoint(summary)` | `continuum coding checkpoint SESSION --summary "…" --agent <model>` |
| Prove it | `coding_run_check(name)` | `continuum coding check SESSION tests --agent <model>` |
| Claim done | `coding_complete_task(task_id)` | `continuum coding task done SESSION TASK --agent <model>` |
| Before stopping | `coding_note(kind=next_step)` | `continuum coding note SESSION next_step "…" --agent <model>` |

`coding_complete_task` is refused unless each of the task's checks passed **on the current code** with **its current definition**. If the code changed after the check, the agent must run it again.

## Switching: Opus 4.8 → Opus 5.5 in Claude Code

1. In the old chat (optional but useful): *"Record a next_step and checkpoint in Continuum."*
2. Switch the model (`/model`) or open a new chat with Opus 5.5.
3. Say *"continue"*. Following the rules, the agent calls `coding_resume(repo_path=".", agent="claude-opus-5-5")`.

The new model sees that it joined, which tasks are verified, the latest check results on the current code, the decisions made, and the next step. Then it carries on.

## Switching: GPT-5.6 → GPT-6.1 in Codex

Same protocol. Codex reads `AGENTS.md`, calls `coding_resume` over MCP (or `continuum coding resume --repo . --agent gpt-6.1` from its shell) and continues. The session is found **by repository**, so a new process, a new chat or even a new machine pointing at the same database picks it up.

MCP tools return a **compact** brief: Markdown plus task ids, about 1–3k tokens however long the session is. Pass `detail="full"` to `coding_brief` for every field: check output tails, registered commands, checkpoint file lists. The brief keeps the newest notes and counts the omitted ones. The journal keeps everything. It also lists the commits made since the last checkpoint and the code map, both observed from git.

## Measured

In real Sonnet → Opus handoffs on this repository ([`HANDOFF-TRIALS.md`](HANDOFF-TRIALS.md)), the next model with Continuum delivered the correct result 4/4 times in both runs and kept both requirements that existed only in the earlier chat, using ~14–17k work tokens and reading 3–4 files. Without it, the next model scored 1/4 in both runs and never learned about the remaining work, after ~21–29k work tokens and 12+ files.

## Inspecting it yourself

```bash
continuum coding brief --repo .          # Markdown brief, the same thing the model sees
continuum coding brief --repo . --json   # machine-readable
continuum coding sessions --repo .
continuum coding close SESSION --reason "merged" --actor alice
```

## Guarantees and limits

- The journal is append-only (enforced by a database trigger), with one gapless sequence per session, even with two agents writing at once. A closed session rejects writes.
- Secrets in notes and check output are redacted before they are stored. Check output is kept only as a tail plus a digest.
- Checks run as argv lists without a shell, in the repository root, under a timeout (default 600 s). A closed session never runs one.
- Repository paths are contained: never `/` or your home directory itself. Set `CONTINUUM_CODING_ROOTS=/path/to/projects` (`:`-separated; `;` on Windows) to restrict agents to your project directories.
- If git can't be read (lock, timeout, broken config), Continuum refuses to fingerprint instead of guessing, so stale evidence never counts.
- **Notes are claims.** A previous model can be wrong. The brief labels notes as claims, and the rules tell the next model to confirm anything critical against the code.
- **Tamper evidence, not prevention.** An agent with shell access could run `continuum coding checks add` itself, because Continuum can't tell a person from a process on the same machine. Every registration records who and when, the brief flags checks (re)registered during the session, and changed definitions invalidate verification. Review those flags (D-029).
- Continuum doesn't edit code, doesn't drive the agent and doesn't store the chat. It is the agent-independent record of the work.
