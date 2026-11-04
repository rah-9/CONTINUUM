# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Continuum is proprietary: every source file says so, and the package can't be uploaded
to a public index by accident."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPDX = "SPDX-License-Identifier: LicenseRef-Continuum-Proprietary"


def test_every_source_file_carries_the_proprietary_notice():
    files = [p for d in ("src", "eval", "tests", "integrations") for p in (ROOT / d).rglob("*")
             if p.suffix in (".py", ".sh") and "__pycache__" not in p.parts]
    assert files
    missing = [str(p.relative_to(ROOT)) for p in files
               if SPDX not in p.read_text(encoding="utf-8")[:300]]
    assert not missing, missing


def test_package_metadata_is_proprietary_and_never_uploaded():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == {"file": "LICENSE"}
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert "All rights reserved" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    plugin = json.loads((ROOT / "integrations/claude-code/.claude-plugin/plugin.json")
                        .read_text(encoding="utf-8"))
    assert plugin["license"].startswith("Proprietary")


def test_one_version_everywhere_and_release_files_present():
    """The package, the plugin and the changelog agree on the version; the release has
    its security policy, upgrade notes and a private-release workflow (never PyPI)."""
    import continuum

    root = Path(__file__).resolve().parents[2]
    meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    plugin = json.loads((root / "integrations" / "claude-code" / ".claude-plugin"
                         / "plugin.json").read_text(encoding="utf-8"))
    assert meta["version"] == continuum.__version__ == plugin["version"]
    assert f"## {meta['version']}" in (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert (root / "SECURITY.md").is_file() and (root / "docs" / "UPGRADING.md").is_file()
    release = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "gh release create" in release
    assert "twine" not in release and "gh-action-pypi-publish" not in release
