# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Every requirement keeps at least one real test (the matrix cannot rot silently)."""

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_PREFIXES = {
    **{f"R{i}": 1 for i in range(1, 17)}, **{f"U{i}": 1 for i in range(1, 10)},
    **{f"I{i}": 1 for i in range(1, 9)}, **{f"NF{i}": 1 for i in range(1, 10)},
    **{f"G{i}": 1 for i in range(1, 7)},
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            and n.name.startswith("test_")}


def test_every_requirement_is_traced_to_existing_tests():
    matrix = yaml.safe_load((ROOT / "tests" / "traceability.yaml").read_text())
    ids = {key.split("_", 1)[0] for key in matrix}
    assert ids == set(REQUIRED_PREFIXES), sorted(set(REQUIRED_PREFIXES) ^ ids)
    for requirement, nodes in matrix.items():
        assert nodes, f"{requirement} has no tests"
        for node in nodes:
            file, _, name = node.partition("::")
            path = ROOT / file
            assert path.is_file(), f"{requirement}: missing {file}"
            assert name in _test_names(path), f"{requirement}: missing test {node}"
