import subprocess

from continuum.coding.repo import observe


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_git_fingerprint_tracks_edits_untracked_and_respects_gitignore(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-qm", "i")
    clean = observe(tmp_path)
    assert clean.vcs == "git" and not clean.dirty and clean.head
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_text("artifact")
    assert observe(tmp_path).fingerprint == clean.fingerprint  # ignored output
    (tmp_path / "new.py").write_text("y = 2\n")
    untracked = observe(tmp_path)
    assert untracked.fingerprint != clean.fingerprint and "new.py" in untracked.changed_files
    (tmp_path / "new.py").write_text("y = 3\n")
    assert observe(tmp_path).fingerprint != untracked.fingerprint  # content, not just names


def test_running_tests_does_not_change_the_fingerprint(tmp_path):
    """Regression: caches written by test runs (no .gitignore) must not count as edits."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "app.py").write_text("x = 1\n")
    before = observe(tmp_path)
    for rel in ["__pycache__/app.cpython-312.pyc", ".pytest_cache/v/cache/lastfailed",
                "node_modules/.cache/x.json", "pkg/__pycache__/m.pyc"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cache")
    after = observe(tmp_path)
    assert after.fingerprint == before.fingerprint
    assert after.changed_files == before.changed_files
