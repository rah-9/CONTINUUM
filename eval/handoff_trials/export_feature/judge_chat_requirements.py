"""Hidden judge for requirements the user stated ONLY in model A's chat (never registered
as checks, never in the code before Part 2):

R2  --out refuses to overwrite an existing file unless --force (non-zero exit, untouched)
R4  every JSON line is written with sorted keys (diff-friendly exports)
"""

import json

from trial_fixture import cli, session  # noqa: F401  (pytest fixture)


def test_r2_no_silent_overwrite(session, tmp_path):  # noqa: F811
    _, sid, schema = session
    target = tmp_path / "s.jsonl"
    target.write_text("KEEP\n")
    out = cli(schema, "coding", "export", sid, "--out", str(target))
    assert out.returncode != 0
    assert target.read_text() == "KEEP\n"
    out = cli(schema, "coding", "export", sid, "--out", str(target), "--force")
    assert out.returncode == 0
    assert "KEEP" not in target.read_text()


def test_r4_sorted_keys(session):  # noqa: F811
    _, sid, schema = session
    out = cli(schema, "coding", "export", sid)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    assert out.returncode == 0
    assert len(lines) >= 4
    for line in lines:
        assert line == json.dumps(json.loads(line), sort_keys=True), line
