"""Portability regressions: any agent, any IDE, any working directory, any OS console."""

import json
import os
import subprocess
import sys
from pathlib import Path

from continuum.interfaces.settings import Settings
from tests.conftest import DSN

PY = sys.executable
EXE = str(Path(sys.executable).parent / "continuum")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_p1_cli_output_survives_a_non_utf8_pipe(schema, tmp_path):
    """Windows pipes default to the ANSI code page; the brief's symbols crashed the CLI."""
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "cp1252"}

    def cli(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([EXE, *args], env=env, capture_output=True, cwd=tmp_path)

    assert cli("coding", "checks", "add", str(repo), "unit", "--actor", "op", "--",
               PY, "-c", "pass").returncode == 0
    sid = json.loads(cli("coding", "start", str(repo), "--goal", "g", "--agent",
                         "gpt-5.6-codex").stdout)["session_id"]
    tid = json.loads(cli("coding", "task", "add", sid, "t", "--check", "unit", "--agent",
                         "m").stdout)["task_id"]
    assert cli("coding", "check", sid, "unit", "--agent", "m").returncode == 0
    assert cli("coding", "task", "done", sid, tid, "--agent", "m").returncode == 0
    out = cli("coding", "brief", "--session", sid)
    assert out.returncode == 0, out.stderr.decode(errors="replace")
    assert "✅ verified" in out.stdout.decode("utf-8")


def test_p2_state_never_lands_in_the_launch_directory(tmp_path, monkeypatch):
    """IDEs start `continuum mcp` from arbitrary (even read-only) directories."""
    for var in ("CONTINUUM_HOME", "CONTINUUM_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.home == tmp_path / "user" / ".continuum"
    assert s.config_dir == tmp_path / "user" / ".continuum" / "config"
    (tmp_path / "config" / "tasks").mkdir(parents=True)  # running from a checkout
    assert Settings().config_dir == Path("config")
    monkeypatch.setenv("CONTINUUM_HOME", "~/elsewhere")
    assert Settings().home == tmp_path / "user" / "elsewhere"
