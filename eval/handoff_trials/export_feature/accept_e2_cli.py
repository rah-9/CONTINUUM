"""Check e2 (Part 2): the export CLI. Registered as a human check."""

import json

from trial_fixture import cli, session  # noqa: F401  (pytest fixture)


def test_cli_export_stdout_and_file(session, tmp_path):  # noqa: F811
    _, sid, schema = session
    out = cli(schema, "coding", "export", sid)
    assert out.returncode == 0, out.stderr + out.stdout
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    assert len(lines) >= 4
    assert lines[0]["type"] == "SessionStarted"
    target = tmp_path / "s.jsonl"
    out = cli(schema, "coding", "export", sid, "--out", str(target))
    assert out.returncode == 0, out.stderr + out.stdout
    assert len(target.read_text().splitlines()) == len(lines)
    assert "CUSTOMER-SECRET-ROW" not in target.read_text()
