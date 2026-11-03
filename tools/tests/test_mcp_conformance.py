"""The conformance runner, tested against servers that break exactly one rule each.

The broken variants reproduce defects this project actually shipped (Lookup without a
Host/Origin guard; a server that silently ignores unknown arguments), so these tests
are the proof that the runner would have caught them.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mcp_conformance as mc

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROBES = REPO / "tools" / "probes"
TOY = PROBES / "toy.toml"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(proc: subprocess.Popen, port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited early (code {proc.returncode})")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    proc.kill()
    pytest.fail("server never listened")


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def toy():
    """variant name -> url, started lazily and stopped at module end."""
    procs: dict[str, tuple[subprocess.Popen, int]] = {}

    def start(variant: str) -> str:
        if variant not in procs:
            port = free_port()
            p = subprocess.Popen([sys.executable, str(HERE / "toy_server.py"), variant, str(port)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_listening(p, port)
            procs[variant] = (p, port)
        return f"http://127.0.0.1:{procs[variant][1]}/mcp"

    yield start
    for p, _ in procs.values():
        stop(p)


async def check(url_or_cmd, probe_path=TOY) -> dict[str, mc.Result]:
    target = url_or_cmd if isinstance(url_or_cmd, list) else [url_or_cmd]
    return {r.check: r for r in await mc.run(target, mc.load_probe(probe_path))}


def statuses(results) -> dict[str, str]:
    return {k: r.status for k, r in results.items()}


# ── the compliant server ─────────────────────────────────────────────────────────


async def test_a_compliant_server_passes_every_check(toy):
    got = await check(toy("good"))
    assert {k: v for k, v in statuses(got).items() if v != mc.PASS} == {}, got
    assert set(got) == set(mc.CHECKS)


async def test_a_compliant_server_exits_zero(toy):
    results = list((await check(toy("good"))).values())
    assert mc.exit_code(results) == 0


# ── servers that each break one rule ─────────────────────────────────────────────


async def test_missing_host_and_origin_guard_is_caught(toy):
    got = statuses(await check(toy("no_guard")))
    assert got["forged_host"] == mc.FAIL
    assert got["foreign_origin"] == mc.FAIL
    assert got["legit_requests"] == mc.PASS      # not failing everything: the guard is what is missing
    assert got["unknown_argument"] == mc.PASS


async def test_silently_ignored_unknown_arguments_are_caught(toy):
    got = await check(toy("official_lax"))
    assert got["unknown_argument"].status == mc.FAIL
    assert "ACCEPTED" in got["unknown_argument"].detail
    assert got["closed_schemas"].status == mc.FAIL
    assert got["forged_host"].status == mc.PASS  # the official server guards Host by default


async def test_a_session_issuing_server_is_caught(toy):
    assert (await check(toy("stateful")))["stateless"].status == mc.FAIL


@pytest.mark.skipif(not mc._non_loopback_addresses(), reason="no non-loopback interface on this host")
async def test_a_wide_bind_is_caught(toy):
    got = await check(toy("wide_bind"))
    assert got["loopback_bind"].status == mc.FAIL
    assert "answers on" in got["loopback_bind"].detail


async def test_loopback_bind_is_unverifiable_without_another_interface(toy, monkeypatch):
    monkeypatch.setattr(mc, "_non_loopback_addresses", lambda: [])
    got = await check(toy("good"))
    assert got["loopback_bind"].status == mc.UNVERIFIABLE
    results = list(got.values())
    assert mc.exit_code(results) == 1                                   # fails closed
    assert mc.exit_code(results, frozenset({"loopback_bind"})) == 0     # unless allowed


# ── fail-closed behaviour ────────────────────────────────────────────────────────


async def test_an_unreachable_server_is_unverifiable_not_passing():
    got = statuses(await check(f"http://127.0.0.1:{free_port()}/mcp"))
    assert mc.PASS not in {v for k, v in got.items() if k != "loopback_bind"}
    assert mc.exit_code([mc.Result(k, v) for k, v in got.items()]) == 1


async def test_a_negative_check_needs_the_unmodified_call_to_be_accepted(toy, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(TOY.read_text().replace('args = { text = "hi" }', 'args = { text = "hi", mode = "z" }'))
    got = await check(toy("good"), bad)
    assert got["unknown_argument"].status == mc.UNVERIFIABLE
    assert "rejects its own unmodified call" in got["unknown_argument"].detail


def test_a_malformed_probe_is_an_error(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('name = "x"\n[ok]\ntool = "t"\n')          # no [strict]
    with pytest.raises(mc.ProbeError):
        mc.load_probe(p)
    with pytest.raises(mc.ProbeError):
        mc.load_probe(tmp_path / "missing.toml")


def test_a_waiver_needs_a_known_check_and_a_reason(tmp_path):
    for waive in ('[waive]\nnonsense = "why"\n', '[waive]\nstateless = "  "\n'):
        p = tmp_path / "p.toml"
        p.write_text(TOY.read_text() + waive)
        with pytest.raises(mc.ProbeError):
            mc.load_probe(p)


async def test_a_waiver_is_printed_and_does_not_fail_the_run(toy, tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(TOY.read_text() + '[waive]\nstateless = "upstream issues a session id; tracked in RFC 003"\n')
    got = await check(toy("stateful"), p)
    assert got["stateless"].status == mc.WAIVED
    assert "upstream issues a session id" in got["stateless"].detail
    assert mc.exit_code([r for k, r in got.items() if k == "stateless"]) == 0


def test_cli_exit_status_and_usage(toy, capsys):
    assert mc.main(["--probe", str(TOY), toy("good"), "--allow-unverifiable", "loopback_bind"]) == 0
    assert mc.main(["--probe", str(TOY), toy("no_guard")]) == 1
    assert mc.main(["--probe", str(HERE / "nope.toml"), toy("good")]) == 2
    with pytest.raises(SystemExit):
        mc.main(["--probe", str(TOY), toy("good"), "--allow-unverifiable", "nonsense"])
    capsys.readouterr()


# ── the real daemons ─────────────────────────────────────────────────────────────


def _start_daemon(args, env, port, tmp_path):
    log = open(tmp_path / f"daemon-{port}.log", "wb")
    p = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT)
    wait_listening(p, port)
    return p


async def test_lookup_conforms(tmp_path):
    sys.path.insert(0, str(REPO / "packages" / "lookup" / "tests"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("lk", REPO / "packages/lookup/tests/test_mcp_server.py")
    lk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lk)
    manifest = lk.write_corpus(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    port = free_port()
    env = {**os.environ, "HOME": str(home), "LOOKUP_NO_DOTENV": "1",
           "LOOKUP_DB": str(tmp_path / "index.db"), "LOOKUP_MANIFEST": str(manifest)}
    p = _start_daemon([sys.executable, "-m", "lookup.cli", "mcp", "--http", "--port", str(port),
                       "--reingest-interval", "0"], env, port, tmp_path)
    try:
        got = await check(f"http://127.0.0.1:{port}/mcp", PROBES / "lookup.toml")
    finally:
        stop(p)
    assert {k: v for k, v in statuses(got).items() if v != mc.PASS} == {}, got


async def test_memory_conforms(tmp_path):
    port = free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "COGNITIVE_MEMORY_ROOT": str(tmp_path / "mem"),
           "COGNITIVE_MEMORY_PORT": str(port)}
    p = _start_daemon([sys.executable, "-m", "cognitive_memory.cli", "serve"], env, port, tmp_path)
    try:
        got = await check(f"http://127.0.0.1:{port}/mcp", PROBES / "memory.toml")
    finally:
        stop(p)
    assert {k: v for k, v in statuses(got).items() if v != mc.PASS} == {}, got


JEV = os.environ.get("COGNITIVE_ENGINE_JEV_CHECKOUT")


@pytest.mark.skipif(not (JEV and shutil.which("bun")),
                    reason="set COGNITIVE_ENGINE_JEV_CHECKOUT to a checkout of the Track 1 branch (needs bun)")
async def test_jev_conforms(tmp_path):
    port = free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "JEV_CACHE_FILE": ":memory:", "JEV_HTTP_PORT": str(port),
           "JEV_CONFIG_FILE": str(tmp_path / "config.json")}
    for k in ("JEV_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        env.pop(k, None)
    p = subprocess.Popen(["bun", "run", "src/index.ts", "--http"], cwd=Path(JEV) / "packages" / "jev",
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_listening(p, port)
    try:
        got = await check(f"http://127.0.0.1:{port}/mcp", PROBES / "jev.toml")
    finally:
        stop(p)
    assert {k: v for k, v in statuses(got).items() if v != mc.PASS} == {}, got


async def test_harness2_conforms(tmp_path):
    port = free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "COGNITIVE_HARNESS2_PORT": str(port),
           "COGNITIVE_HARNESS2_EXPERIENCE_ROOT": str(tmp_path / "experiences")}
    p = _start_daemon([sys.executable, "-m", "cognitive_harness2.cli", "serve"], env, port, tmp_path)
    try:
        got = await check(f"http://127.0.0.1:{port}/mcp", PROBES / "harness2.toml")
    finally:
        stop(p)
    assert {k: v for k, v in statuses(got).items() if v != mc.PASS} == {}, got
    assert list(home.iterdir()) == [], "the daemon wrote into $HOME"
