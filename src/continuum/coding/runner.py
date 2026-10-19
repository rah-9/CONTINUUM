# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Run one human-registered check: argv without a shell, bounded in time and output.

* The command is resolved like a shell would (``shutil.which`` honours PATHEXT, so
  ``npm test`` finds ``npm.cmd`` on Windows) but is never passed to a shell.
* It runs in its own process group/session; on timeout the whole tree is killed, so
  grandchildren (``npm`` -> ``jest``) cannot keep changing the tree afterwards.
* Output goes to a temporary file, not pipes: no pipe-inheritance hangs, and memory is
  bounded (only the tail is read back).
* Continuum's own settings and credentials (``CONTINUUM_*``) are removed from the
  environment: code under test must not reach the journal's database.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_READ = 1 << 20  # bytes of output tail kept in memory
_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class CheckProcessResult:
    output: bytes
    exit_code: int | None
    timed_out: bool


def check_environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("CONTINUUM_")}


def resolve_command(argv: list[str], env: dict[str, str]) -> list[str]:
    exe = argv[0]
    if os.path.dirname(exe):  # an explicit path is used as given
        return argv
    found = shutil.which(exe, path=env.get("PATH"))
    return [found, *argv[1:]] if found else argv


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        if _WINDOWS:
            # constant argv; only the pid varies
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S603,S607
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()


def run_check_process(argv: list[str], cwd: Path, timeout: float) -> CheckProcessResult:
    env = check_environment()
    command = resolve_command(argv, env)
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(  # noqa: S603 - human-registered argv, no shell
                command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=not _WINDOWS,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except FileNotFoundError as exc:
            return CheckProcessResult(str(exc).encode(), 127, False)
        except OSError as exc:  # not executable, a directory, argv too long, ...
            return CheckProcessResult(f"{type(exc).__name__}: {exc}".encode(), 126, False)
        timed_out = False
        try:
            exit_code: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
            _kill_tree(proc)
            proc.wait()
        if not _WINDOWS and not timed_out:
            # the check finished; stragglers it left behind must not keep running
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - MAX_READ))
        return CheckProcessResult(out.read(), exit_code, timed_out)
