# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The canonical DDL in docs/03 must equal the executable schema.sql."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_docs_ddl_matches_schema_sql():
    doc = (ROOT / "docs" / "03-STATE-SCHEMA.md").read_text()
    section = doc[doc.index("## 3. PostgreSQL DDL"):]
    block = re.search(r"```sql\n(.*?)\n```", section, re.S)
    assert block is not None
    sql = (ROOT / "src" / "continuum" / "core" / "schema.sql").read_text().rstrip("\n")
    assert block.group(1) == sql
