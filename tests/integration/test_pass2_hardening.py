"""Second-pass audit regressions (v0.6.3). Each test failed before its fix."""

import asyncio
import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from continuum.coding.repo import observe
from continuum.coding.service import CodingContinuity
from continuum.core.clock import SystemClock
from continuum.core.errors import ContinuumError, SpecValidationError
from continuum.core.events import ModelCallFinished, make_event
from continuum.core.ids import RandomIds
from continuum.core.redaction import Redactor
from continuum.core.sessions import estimate_cost, usage_payload
from continuum.core.state import RunStatus
from continuum.core.store import Store
from continuum.models.base import (
    Message,
    ModelEndpoint,
    ModelError,
    ModelResponse,
    ModelRouter,
    ToolProposal,
    Usage,
)
from continuum.models.fake import ScriptedModelAdapter, fake_profile
from tests.conftest import DSN

PY = sys.executable


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(repo), *args],
                   check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "d@e.x")
    _git(path, "config", "user.name", "d")
    (path / "a.txt").write_text("x\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def coding(store):
    return CodingContinuity(store, clock=SystemClock(), ids=RandomIds(),
                            redactor=Redactor.from_environment())


def _router(tool: str, args: dict) -> ModelRouter:
    resp = ModelResponse(message=Message(role="assistant", tool_proposals=[
        ToolProposal(provider_call_id="c1", tool_name=tool, arguments=args)]),
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd="0", source="reported"),
        finish_reason="tool_calls")
    return ModelRouter({a: ModelEndpoint(profile=fake_profile(a),
                                         adapter=ScriptedModelAdapter([resp]))
                        for a in ("A", "B", "C")})


def _rejections(lab, run_id):
    with lab.store.read() as q:
        return [e.payload["code"] for e in q.events(run_id) if e.type.value == "ActionRejected"]


# ----- Q1/Q2: model-controlled input is rejected, never crashes the step loop -------------

def test_q1_model_path_escape_in_bound_fields_is_rejected(make_lab):
    lab = make_lab("csv_validate_columns")
    rt = lab.new_runtime(router=_router("csv.normalize", {
        "source": "../../../../etc/passwd", "output": "artifacts/x.csv"}))
    result = rt.start(lab.definition.spec)
    assert result.status != RunStatus.RUNNING
    assert "path_not_permitted" in _rejections(lab, result.run_id)


def test_q2_nul_in_model_tool_args_is_rejected(make_lab):
    lab = make_lab("csv_validate_columns")
    rt = lab.new_runtime(router=_router("report.write", {"path": "artifacts/r.txt",
                                                         "content": "a\x00b"}))
    result = rt.start(lab.definition.spec)
    assert result.status != RunStatus.RUNNING
    assert "invalid_args" in _rejections(lab, result.run_id)


def test_q2_nul_in_coding_text_and_check_output_is_stored(coding, tmp_path):
    repo = _repo(tmp_path / "r")
    sid = coding.start(str(repo), "goal\x00x", agent="m")["session_id"]
    coding.note(sid, "context", "a\x00b", agent="m")
    coding.register_check(str(repo), "bin", [PY, "-c", "import sys; sys.stdout.write('ok\\0done')"],
                          actor="me", timeout_seconds=30)
    result = coding.run_check(sid, "bin", agent="m")
    assert result["passed"] and "\x00" not in result["output_tail"]
    assert coding.brief(sid)["agent_notes"]["context"][-1]["text"] == "a�b"


# ----- Q3/Q4: fingerprint sees submodules and non-UTF-8 names ---------------------------------

def test_q3_edits_inside_a_dirty_submodule_change_the_fingerprint(tmp_path):
    lib = _repo(tmp_path / "lib")
    app = _repo(tmp_path / "app")
    _git(app, "submodule", "add", "-q", str(lib), "lib")
    _git(app, "commit", "-qm", "sub")
    (app / "lib" / "a.txt").write_text("dirty 1\n")
    first = observe(app).fingerprint
    (app / "lib" / "a.txt").write_text("dirty 2\n")
    second = observe(app).fingerprint
    (app / "lib" / "new.txt").write_text("untracked in submodule\n")
    assert len({first, second, observe(app).fingerprint}) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX byte file names")
def test_q4_non_utf8_file_names_are_fingerprinted_and_never_crash(tmp_path):
    repo = _repo(tmp_path / "g")
    name = os.path.join(os.fsencode(repo), b"caf\xe9.txt")
    with open(name, "wb") as fh:
        fh.write(b"one")
    before = observe(repo).fingerprint
    with open(name, "wb") as fh:
        fh.write(b"two")
    assert observe(repo).fingerprint != before  # was a blind spot
    os.symlink(b"/tmp/t\xe9", os.path.join(os.fsencode(repo), b"lnk"))
    observe(repo)  # was a UnicodeEncodeError
    plain = tmp_path / "plain"
    plain.mkdir()
    with open(os.path.join(os.fsencode(plain), b"caf\xe9.txt"), "wb") as fh:
        fh.write(b"x")
    assert observe(plain).vcs == "files"  # was a UnicodeEncodeError


# ----- Q5: cost precision keeps projection == replay ------------------------------------------

def test_q5_float_costs_do_not_break_projection_equivalence(make_lab):
    lab = make_lab("csv_validate_columns")
    ctx = lab.open_run()
    u = estimate_cost(fake_profile("A"), Usage(input_tokens=1234, output_tokens=56,
                                               cost_usd="3.0000000000000004e-06",
                                               source="estimated"))
    lab.store.append(ctx.run_id, ctx.lease.fence, [make_event(ModelCallFinished(
        session_id="s", call_no=1, response_ref="blob:sha256:" + "0" * 64,
        finish_reason="stop", proposal_count=0, usage=usage_payload(u)), actor="runtime")])
    assert lab.store.projection_diff(ctx.run_id) == []


# ----- Q6: terminal runs stay terminal -----------------------------------------------------

def test_q6_abort_after_cancel_keeps_the_run_cancelled(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    [action_id] = result.needs_human
    assert lab.runtime.cancel(result.run_id, actor="ops", reason="stop").status == \
        RunStatus.CANCELLED
    out = lab.runtime.resolve_action(result.run_id, action_id, "abort", actor="ops2",
                                     reason="later")
    assert out.status == RunStatus.CANCELLED
    with lab.store.read() as q:
        assert q.action(action_id).status.value == "skipped"


# ----- Q7/Q8/Q9: running checks --------------------------------------------------------------

def test_q7_checks_get_no_continuum_credentials_and_dsn_passwords_are_redacted(
        store, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUUM_DATABASE_URL", "postgresql://u:S3cretPw0rd@db/x")
    monkeypatch.setenv("OTHER_DB_URL", "postgresql://u:An0therPass@db/y")
    coding = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    repo = _repo(tmp_path / "r")
    sid = coding.start(str(repo), "goal", agent="m")["session_id"]
    code = ("import os; print(os.environ.get('CONTINUUM_DATABASE_URL', 'absent')); "
            "print(os.environ['OTHER_DB_URL'])")
    coding.register_check(str(repo), "env", [PY, "-c", code], actor="me", timeout_seconds=30)
    out = coding.run_check(sid, "env", agent="m")["output_tail"]
    assert "absent" in out and "An0therPass" not in out and "S3cretPw0rd" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_q8_check_timeout_kills_the_whole_process_tree(coding, tmp_path):
    repo = _repo(tmp_path / "r")
    marker = tmp_path / "grandchild-ran"
    grandchild = (f"import time, pathlib; time.sleep(2); "
                  f"pathlib.Path({str(marker)!r}).write_text('x')")
    code = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
            f"{grandchild!r}]); time.sleep(30)")
    coding.register_check(str(repo), "slow", [PY, "-c", code], actor="me", timeout_seconds=1)
    sid = coding.start(str(repo), "goal", agent="m")["session_id"]
    result = coding.run_check(sid, "slow", agent="m")
    assert result["timed_out"] and not result["passed"]
    time.sleep(3)
    assert not marker.exists()


def test_q9_check_commands_are_resolved_like_a_shell_would(coding, tmp_path, monkeypatch):
    """On Windows `npm test` is npm.cmd: argv[0] is resolved with PATH/PATHEXT."""
    import continuum.coding.runner as runner

    repo = _repo(tmp_path / "r")
    monkeypatch.setattr(runner.shutil, "which",
                        lambda cmd, path=None: PY if cmd == "npmlike" else None)
    coding.register_check(str(repo), "t", ["npmlike", "-c", "pass"], actor="me",
                          timeout_seconds=30)
    sid = coding.start(str(repo), "goal", agent="m")["session_id"]
    assert coding.run_check(sid, "t", agent="m")["passed"]


# ----- Q10-Q14: configuration and identities ------------------------------------------------

def test_q10_undecodable_or_duplicate_task_files_are_reported(schema, tmp_path):
    from continuum.interfaces.registry import TaskRegistry

    tasks = tmp_path / "cfg" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "a.yaml").write_text("task_id: t1\nfactory: m:f\n", encoding="utf-8")
    (tasks / "b.yaml").write_text("task_id: t1\nfactory: m:f\n", encoding="utf-8")
    (tasks / "c.yaml").write_bytes(b"task_id: t2\ndescription: caf\xe9\n")
    reg = TaskRegistry(tmp_path / "cfg", tmp_path / "home")
    assert reg.task_ids() == ["t1"]
    assert sorted(p.name for p in reg.errors) == ["b.yaml", "c.yaml"]


def test_q11_schema_names_must_be_plain_identifiers():
    from continuum.core.clock import FakeClock
    from continuum.core.ids import SequentialIds

    with pytest.raises(SpecValidationError):
        Store(DSN, schema="AuditTest", clock=FakeClock(), ids=SequentialIds("x"))


def test_q12_cli_never_prints_a_raw_traceback(monkeypatch, capsys):
    from continuum.interfaces import cli

    def broken(*a, **k):
        raise OSError("disk unplugged")

    monkeypatch.setattr(cli, "Continuum", broken)
    assert cli.main(["runs"]) == 2
    assert '"code": "internal"' in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--log-level", "verbose", "runs"])  # argparse usage error, not a traceback
    assert exit_.value.code == 2


def test_q13_lab_services_import_without_fcntl():
    code = ("import sys; sys.modules['fcntl'] = None; "
            "import continuum_lab.mock_services.base")
    subprocess.run([PY, "-c", code], check=True, capture_output=True)


def test_q14_default_owner_ids_are_unique_even_with_same_host_and_pid(monkeypatch):
    import continuum.interfaces.settings as settings

    monkeypatch.delenv("CONTINUUM_OWNER_ID", raising=False)
    monkeypatch.setattr(os, "getpid", lambda: 7)
    first = importlib.reload(settings).Settings().owner_id
    second = importlib.reload(settings).Settings().owner_id
    assert first != second


# ----- Q15-Q17: budgets, adapters, verifier messages ----------------------------------------

def test_q15_recovery_work_counts_as_active_time(make_lab):
    lab = make_lab("notify_opaque")
    lab.env.messages.script("send", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    with lab.store.read() as q:
        before = q.usage(result.run_id).active_elapsed_ms

    class SlowRecovery:
        def checkpoint(self, boundary, **ctx):
            if boundary == "after_lease_acquired":
                lab.clock.advance(20)  # under the 30 s lease TTL

    lab.store.faults = SlowRecovery()
    lab.runtime.resume(result.run_id)
    with lab.store.read() as q:
        assert q.usage(result.run_id).active_elapsed_ms - before >= 20_000


def test_q16_missing_litellm_is_a_model_error(monkeypatch):
    from continuum.models.litellm_adapter import LiteLLMAdapter

    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(ModelError) as err:
        asyncio.run(LiteLLMAdapter("openai/gpt-6.1").complete(
            messages=[Message(role="user", content="hi")], tools=[], max_output_tokens=10,
            timeout_seconds=5))
    assert err.value.category == "unsupported_capability"


def test_q17_verifier_failures_are_redacted_and_bounded(make_lab, monkeypatch):
    lab = make_lab("csv_validate_columns")
    ctx = lab.open_run()
    secret = "sk-live-VERIFIERLEAK-123456"
    verification = lab.runtime.verification
    verification.redactor = Redactor([secret])

    def explode(*a, **k):
        raise RuntimeError(secret + "x" * 2000)

    monkeypatch.setattr(verification, "evaluate", explode)
    outcome = verification.verify(ctx, ctx.spec.milestones[0].id)
    assert secret not in outcome.message and len(outcome.message) <= 500
    with lab.store.read() as q:
        msgs = [e.payload["message"] for e in q.events(ctx.run_id)
                if e.type.value == "VerifierError"]
    assert msgs and secret not in msgs[0] and len(msgs[0]) <= 500


def test_store_rejects_bad_schema_is_typed():
    assert issubclass(SpecValidationError, ContinuumError)
