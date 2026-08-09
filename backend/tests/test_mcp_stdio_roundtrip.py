"""Round-trip through a real stdio MCP server subprocess with Snowflake faked out.

The server subprocess imports copilot.snowflake_client for real, so we point it at a
stub via env: COPILOT_FAKE_SNOWFLAKE=1 makes server.py use an in-process fake.
"""
import subprocess
import threading
import time

import pytest

from copilot.mcp_client import McpError, McpExecutor


@pytest.fixture(scope="module")
def executor(monkeypatch_module_env):
    ex = McpExecutor()
    yield ex
    ex.close()


@pytest.fixture(scope="module")
def monkeypatch_module_env():
    import os

    os.environ["COPILOT_FAKE_SNOWFLAKE"] = "1"
    yield
    os.environ.pop("COPILOT_FAKE_SNOWFLAKE", None)


def test_round_trip_query(executor):
    cols, rows = executor.run_query("SELECT model FROM GOLD.DIM_MACHINE")
    assert cols == ["MODEL"] and rows == [["TrueBeam"], ["Halcyon"]]


def test_round_trip_guard_rejection(executor):
    with pytest.raises(McpError, match="rejected by SQL guard"):
        executor.run_query("DROP TABLE GOLD.DIM_MACHINE")


# --- Finding 1: close() must be idempotent -- a second call used to raise
# RuntimeError: Event loop is closed (call_soon_threadsafe on the loop that
# the first close() had already closed).


def test_close_is_idempotent(monkeypatch_module_env):
    ex = McpExecutor()
    ex.close()
    ex.close()  # must be a no-op, not RuntimeError: Event loop is closed


# --- Finding 2: a construction that never reaches readiness (subprocess spawns
# but the handshake hangs forever) must not leak the background thread or the
# spawned subprocess. `ready_timeout`/`server_args` are test-only seams on
# McpExecutor -- ready_timeout keeps this fast (no real 60s wait), server_args
# points the transport at a script that spawns fine but never speaks MCP, so
# `session.initialize()` hangs exactly like the reported repro.


def _wait_until(predicate, timeout=5, interval=0.05):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(interval)
        ok = predicate()
    return ok


def test_failed_construction_leaves_no_thread_or_subprocess(tmp_path):
    hang_script = tmp_path / "hang.py"
    hang_script.write_text("import time\ntime.sleep(9999)\n")

    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}

    with pytest.raises(McpError, match="timed out"):
        McpExecutor(ready_timeout=2, server_args=[str(hang_script)])

    def no_leaked_threads():
        leaked = [t for t in threading.enumerate()
                  if t.ident not in threads_before and t.is_alive()]
        return not leaked

    assert _wait_until(no_leaked_threads), (
        "background thread(s) survived a failed McpExecutor construction: "
        + repr([t.name for t in threading.enumerate() if t.ident not in threads_before]))

    def no_orphan_subprocess():
        out = subprocess.run(
            ["pgrep", "-f", str(hang_script)], capture_output=True, text=True, check=False).stdout
        return out.strip() == ""

    assert _wait_until(no_orphan_subprocess), (
        "the hang-script subprocess survived a failed McpExecutor construction")


# --- Finding 3: a construction that fails fast (not via timeout -- e.g. the
# subprocess spawns but exits immediately) must also close the event loop, not
# just the thread. Pointing args at a nonexistent script makes the subprocess's
# own interpreter fail immediately ("can't open file ..."), which closes the
# stdio streams and makes ClientSession's handshake raise quickly -- exercising
# _main()'s `except Exception` / `_start_error` path rather than the
# ready_timeout path exercised above.


def test_failed_construction_via_bad_script_closes_loop_no_warning(recwarn):
    bad_args = ["/nonexistent/path/to/mcp-copilot-test-script.py"]

    with pytest.raises(McpError):
        McpExecutor(ready_timeout=10, server_args=bad_args)

    import gc

    gc.collect()
    resource_warnings = [w for w in recwarn.list if issubclass(w.category, ResourceWarning)]
    assert not resource_warnings, f"leaked resources: {[str(w.message) for w in resource_warnings]}"
