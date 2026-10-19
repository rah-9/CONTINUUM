# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The ``continuum`` command. App hooks (``continuum hook ...``) run before and after every
tool call, so they are dispatched before the CLI, the SDK and the database driver are
imported: a hook pays for Python's start and its own work, nothing else (D-042)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook"]:
        from continuum.coding.hooks import main as hook_main

        return hook_main(args[1:])
    from continuum.interfaces.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
