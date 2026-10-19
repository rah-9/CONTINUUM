# Upgrading Continuum

Continuum migrates its database schema itself, on the first command after an upgrade
(`ensure_schema`), under an advisory lock so that concurrent starts are safe. Migrations
are additive and idempotent. A database written by a newer Continuum is never downgraded:
an older installation stops with `schema_too_new`.

## Every upgrade

1. **Back up** the database (`pg_dump`). Schema changes cannot be rolled back by an older Continuum.
2. **Install** the new wheel: `uv tool install --force continuum-<version>-py3-none-any.whl` (or `pip install --upgrade`). Check its SHA-256 against `SHA256SUMS` from the release.
3. **Migrate** with the schema owner's login: `continuum db-init`. It is idempotent, and safe to run while agents are idle.
4. **Check** with `continuum doctor`. It must report `"ok": true`.
5. **Upgrade every machine** that uses the same database. An older Continuum refuses to start on the newer schema instead of writing to it.

## 0.9.x → 1.0.0

Schema v4 → v5. Automatic; no data is rewritten.

| Change | What you do |
|---|---|
| **Erasure and retention** (`continuum coding forget`, `coding erasures`; D-039). The journal trigger now admits a DELETE only inside an erasure transaction. | Optional: schedule retention, e.g. a daily `continuum coding forget --older-than-days 90 --actor ops --reason "retention policy" --yes`. |
| **Per-person isolation** (row-level security; D-040). Sessions, checks and tombstones gain `owner_role`. Existing rows get the role that runs the migration. | Single user: nothing. Several people: give each one a PostgreSQL login, `continuum admin grant <role>`, and set their `CONTINUUM_DATABASE_URL`. If an existing session belongs to someone else: `continuum admin reassign <old-role> <new-role>`. |
| **Checks are per person.** The primary key of `coding_checks` now includes `owner_role`. | Nothing. People re-register checks they want for themselves. |
| **The agent's actions** (tool calls) are captured from transcripts as `ChatCaptured` events with role `action` (D-041). | Nothing. Older sessions show them from the next transcript import onward. |
| **`continuum doctor`** no longer fails when no runtime tasks are registered. | Nothing. |
| **Plugin version** `1.0.0rc1` (Claude Code). | Re-install or update the plugin from the marketplace, then restart Claude Code. |

**If Continuum connects as a role that does not own the schema** (for example, the tables were
created by `postgres` and Continuum logs in as `continuum`): run the migration with the owner's
login, then give the existing sessions to the role Continuum uses, or they will look empty to it:

```bash
CONTINUUM_DATABASE_URL=postgresql://postgres@db/continuum continuum db-init
CONTINUUM_DATABASE_URL=postgresql://postgres@db/continuum continuum admin reassign postgres continuum
CONTINUUM_DATABASE_URL=postgresql://postgres@db/continuum continuum admin grant continuum
```

Re-running `continuum coding setup` in a repository is not required. It is safe (idempotent)
and refreshes the agent rules in `AGENTS.md`.

## Older versions

The schema DDL is cumulative and idempotent, so any version from 0.6 on migrates directly to
1.0. Upgrading a v3 database in place is tested (`test_s11_a_v3_database_upgrades_in_place`).

| From | Schema | Added since |
|---|---|---|
| 0.9.x | v4 | the 1.0 changes above |
| 0.8.x | v4 | 0.9's consolidated memory (no schema change), then 1.0 |
| 0.6.x–0.7.x | v3 | chat capture and repository identity (v4), then 1.0 |

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
