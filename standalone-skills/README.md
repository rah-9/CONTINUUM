# Standalone skills

Skills sold or shared **separately** from Continuum. They don't import or need the
`continuum` package, PostgreSQL or the MCP server: each folder is a complete skill that
installs on its own into any agent that supports `SKILL.md` skills.

| Skill | What it does |
|---|---|
| [`agent-continuity`](agent-continuity/) | File-based work journal for switching model, chat or app: the user's own words, check-verified tasks, and a ledger that stops a lost push or deploy from being repeated |

What ships to buyers is only `SKILL.md` and `scripts/`. `tests/` and the listing text stay
here. Build the upload:

```bash
cd standalone-skills && zip -r agent-continuity.zip agent-continuity/SKILL.md agent-continuity/scripts
```

Test: `python3 -m pytest standalone-skills/agent-continuity/tests`.
