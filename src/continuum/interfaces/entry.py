# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The ``continuum`` command. App hooks (``continuum hook ...``) run before and after every
tool call, so they are dispatched before the CLI, the SDK and the database driver are
imported: a hook pays for Python's start and its own work, nothing else (D-042)."""

from __future__ import annotations

import sys


def _utf8_output() -> None:
    """Apps read our output through pipes. On Windows a pipe defaults to the ANSI code
    page (e.g. cp1252), which cannot encode the brief's symbols: the brief would never
    reach the new chat (found by Windows CI on the hook path)."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _utf8_output()
    if args[:1] == ["hook"]:
        from continuum.coding.hooks import main as hook_main

        return hook_main(args[1:])
    from continuum.interfaces.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
