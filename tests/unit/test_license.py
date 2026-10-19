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
    missing = [str(p.relative_to(ROOT)) for p in files if SPDX not in p.read_text()[:300]]
    assert not missing, missing


def test_package_metadata_is_proprietary_and_never_uploaded():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == {"file": "LICENSE"}
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert "All rights reserved" in (ROOT / "LICENSE").read_text()
    plugin = json.loads((ROOT / "integrations/claude-code/.claude-plugin/plugin.json")
                        .read_text())
    assert plugin["license"].startswith("Proprietary")
