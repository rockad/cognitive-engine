"""The contained proposer session runner (RFC 004 6.2.3, slice 3b).

One session is TWO containers on two networks, started and torn down together:

    session container   the agent CLI. Network: ONE `--internal` network (no default route, no egress, no
                        external DNS). Its HTTP(S)_PROXY is the proxy container; NO_PROXY is the session's OWN
                        loopback (127.0.0.1,localhost,::1), so a CLI that serves itself works.
    proxy container     `egress_proxy.py` from the same image. Attached to the internal network AND (for a hosted
                        channel only) an egress network. Allows exactly the channel profile's endpoint(s).
                        For the LOCAL channel it has no egress network at all and a read-only mount of the
                        host bridge's private directory; it relays to the bridge's unix socket.

Every docker call is `docker -H <verified socket> ...` with every DOCKER_* variable stripped (the DockerRunner
conventions). argv is built as a list, never a shell string; no credential is ever in an argv or an `-e`.

The runner FAILS CLOSED. `run` refuses before creating anything unless all of these hold:

    * the rootless gate passes now (`verify_rootless_docker`, against the pinned image id);
    * the channel decision (`channels.decide`, re-run here from zdr and classification) is allowed;
    * for a PROPOSER session on an opencode or claude_cli channel, the caller's `upstream_egress.Decision` is present
      and allowed (a SOLVER session, `role="solver"`, needs none: it is the same session, same hardening, same
      channel decision, and its command line is built here, not read from an upstream Proposer);
    * with a `sweep_budget.SweepBudget`, a session is reserved from it first, its timeout is clipped to the
      reservation, and it is released with how it ended;
    * the image is a pinned `ImageRef`; the wall clock is within the channel's cap;
    * argv[0] is the mounted CLI path; the workspace is a safe absolute directory; no secret is in argv or env.

Session container hardening: read-only root, tmpfs HOME and /tmp (noexec, nosuid, size-capped), --cap-drop ALL,
no-new-privileges, user 65534, pids/memory/cpu limits, the CLI bind-mounted read-only, the workspace bind-mounted
read-write at the SAME ABSOLUTE PATH as on the host, a credential file (if any) mounted read-only, nothing else.
Never the home directory, never the docker socket.

Credentials: written to a file with a random name in a private directory (a 0700 parent, so no other host user
can reach it), bind-mounted read-only at the path the caller names. The file is mode 0444, not 0400: under rootless
Docker the container user 65534 is not the file's owner, and 0400 would make the file unreadable to it. What keeps it
private is the 0700 parent on the host and the fact that only the session container mounts it. It is removed in
`finally`. The value is redacted from everything returned.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import channels, egress, sandbox, upstream_egress, workspace_harvest
from .egress_proxy import PROXY_PORT_DEFAULT, READY_LINE
from .host_bridge import HostBridge, validate_target
from .image import PROXY_PATH_IN_IMAGE, ImageRef
from .sweep_budget import ROLES, BudgetExhausted, SweepBudget

LABEL = "cognitive-harness2.session"
# The proxy container's network alias, the CLI's model host AND the `Host` header Ollama sees. A loopback Ollama answers 403
# to any Host that is not a loopback/private IP, `localhost`, its machine's name or a `.localhost`/`.local`/`.internal` name
# (DNS-rebinding guard, see `ollama_guard`), and this name is what the proxy forwards as Host. It was `harness2-proxy`: the
# second real stage-1 run died on a 403 from Ollama. Docker resolves a dotted alias on the --internal network (measured).
PROXY_ALIAS = "harness2-proxy.internal"
PROXY_PORT = PROXY_PORT_DEFAULT
CLI_DIR = "/opt/cli"
HOME = "/home/agent"
USER = "65534:65534"
BRIDGE_MOUNT = "/bridge"
PROXY_CONFIG_MOUNT = "/proxy"
MAX_ARGV_BYTES = 512 * 1024
MAX_ENV_VALUE = 4096
_MEM = re.compile(r"^[1-9][0-9]{0,5}[kmg]$")
_CPUS = re.compile(r"^(?:[1-9][0-9]?|0?\.[1-9][0-9]?|[1-9][0-9]?\.[0-9]{1,2})$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CLI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MOUNT_UNSAFE = re.compile(r'[,"\n\r\0=]')
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b.", re.DOTALL)
_BAD_WS_PREFIXES = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/boot",
                    "/root", "/var/run", "/run/docker", "/var/lib/docker", "/snap")
_FIXED_ENV = frozenset({"HOME", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY",
                        "TERM", "LANG", "NO_COLOR", "PATH", "USER", "LOGNAME", "HOSTNAME"})
_DOCKER_TIMEOUT_S = 60
_READY_TIMEOUT_S = 30.0
MAX_ACCESS_LOG_BYTES = 256 * 1024


class SessionRefused(RuntimeError):
    """A precondition failed and NOTHING was started (fail closed)."""


class SessionError(RuntimeError):
    """Something failed after the session began; cleanup was still attempted."""


class SessionCleanupError(RuntimeError):
    """Teardown left something behind. `.leftovers` names it; `.result` is what the session returned."""

    def __init__(self, leftovers: Sequence[str], result: "SessionResult | None" = None):
        super().__init__("session teardown left behind: " + ", ".join(leftovers))
        self.leftovers, self.result = tuple(leftovers), result


# ── specs ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Credential:
    """A secret for the session container only. The value never appears in repr, argv, env, logs or results."""
    container_path: str                       # where the file is mounted (read-only) inside the session container
    value: str = field(repr=False)
    env_var: str | None = None                # optional: set to the PATH of the file (never the value)

    def __post_init__(self) -> None:
        p = self.container_path
        if not (isinstance(p, str) and p.startswith("/") and not _MOUNT_UNSAFE.search(p)
                and ".." not in p.split("/") and len(p) < 400):
            raise SessionRefused("the credential path must be a clean absolute container path")
        if any(p == d or p.startswith(d + "/") for d in ("/proc", "/sys", "/dev", CLI_DIR, "/tmp")):
            raise SessionRefused("the credential path is in a reserved location")
        if not isinstance(self.value, str) or not self.value or len(self.value) > 65536 or "\0" in self.value:
            raise SessionRefused("the credential value must be a non-empty string")
        if self.env_var is not None and not (_ENV_NAME.fullmatch(self.env_var) and self.env_var not in _FIXED_ENV
                                             and not self.env_var.startswith("DOCKER_")):
            raise SessionRefused("the credential env_var name is not acceptable")

    def __str__(self) -> str:
        return f"Credential({self.container_path})"


@dataclass(frozen=True)
class SessionLimits:
    pids: int = 256
    memory: str = "2g"
    cpus: str = "2"
    tmpfs_size: str = "256m"
    max_output_bytes: int = 10 * 1024 * 1024
    proxy_memory: str = "128m"
    proxy_pids: int = 64
    file_bytes: int = 256 * 1024 * 1024        # RLIMIT_FSIZE: the largest single file the session can write

    def check(self) -> None:
        if not (isinstance(self.pids, int) and 8 <= self.pids <= 4096):
            raise SessionRefused("pids must be an integer from 8 to 4096")
        for name in ("memory", "tmpfs_size", "proxy_memory"):
            if not _MEM.fullmatch(getattr(self, name)):
                raise SessionRefused(f"{name} must look like 256m or 2g")
        if not _CPUS.fullmatch(self.cpus):
            raise SessionRefused("cpus must be a small decimal number such as 1 or 2")
        if not (isinstance(self.max_output_bytes, int) and 1024 <= self.max_output_bytes <= 1 << 28):
            raise SessionRefused("max_output_bytes must be from 1 KiB to 256 MiB")
        if not (isinstance(self.file_bytes, int) and 1 << 20 <= self.file_bytes <= 1 << 36):
            raise SessionRefused("file_bytes must be from 1 MiB to 64 GiB")


@dataclass(frozen=True)
class SessionSpec:
    argv: tuple[str, ...]                     # argv[0] must be the mounted CLI path (see `container_cli_path`)
    workspace: str                            # absolute host directory; mounted read-write at the same path
    cli: str                                  # host path of the CLI binary (a regular file); mounted read-only
    timeout_s: float
    env: Mapping[str, str] = field(default_factory=dict)
    credential: Credential | None = None
    stdin: bytes | None = None
    limits: SessionLimits = SessionLimits()
    tmpfs_dirs: tuple[str, ...] = ()          # extra user-owned tmpfs dirs (e.g. a CLI's state directory under HOME)
    tmp_exec: bool = False                    # allow executing from /tmp and HOME (some CLIs unpack native code there)
    reclaim_workspace: bool = True            # give the host user ownership of files the container user created
    harvest: workspace_harvest.HarvestLimits | None = workspace_harvest.HarvestLimits()


@dataclass(frozen=True)
class SessionResult:
    run_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_s: float
    egress_log: tuple[str, ...]               # the proxy's decisions: host:port and a rule code, nothing else
    harvest: workspace_harvest.HarvestReport | None = None
    reclaimed: bool | None = None
    leftovers: tuple[str, ...] = ()
    role: str = "proposer"                    # which kind of session ran: "proposer" or "solver"


def container_cli_path(cli_host_path: str) -> str:
    return f"{CLI_DIR}/{os.path.basename(cli_host_path)}"


# ── pure helpers ──────────────────────────────────────────────────────────────────

def sanitize_terminal(text: str) -> str:
    """Strip ANSI/OSC escape sequences and control characters (keeping newline and tab)."""
    text = _ANSI.sub("", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cs") and c != "\x7f")


def redact(text: str, secrets_: Sequence[str]) -> str:
    """Replace every occurrence of each secret, and drop a trailing partial secret left by a truncated stream."""
    for s in sorted({s for s in secrets_ if s}, key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
        for n in range(min(len(s) - 1, 64), 3, -1):
            if text.endswith(s[:n]):
                text = text[:-n] + "[REDACTED]"
                break
    return text


_ACCESS_LINE = re.compile(r"ACCESS decision=(allow|deny) host=(\S+) port=(\d+) reason=([a-z-]+)")
_MODEL_ROUTES = frozenset({"unix-route", "unix-listener"})      # the proxy's two ways to the host bridge (the local model)


def egress_summary(lines: Sequence[str]) -> dict[str, object]:
    """What the proxy decided during one session, as numbers and `host:port` strings. Nothing else of the log is kept.

    `model_requests` counts the connections the proxy opened to the host bridge, i.e. to the local model endpoint
    (`unix-route`, or the direct listener). It is connection-level evidence that the CLI got as far as the model's
    door: it says nothing about whether the model answered. `denied_hosts` is deduplicated, in first-seen order, capped."""
    model = allowed = denied = 0
    hosts: list[str] = []
    reasons: list[str] = []
    for line in lines:
        m = _ACCESS_LINE.fullmatch(line.strip())
        if not m:
            continue
        decision, host, port, reason = m.groups()
        if decision == "allow":
            allowed += 1
            model += reason in _MODEL_ROUTES
            continue
        denied += 1
        if f"{host}:{port}" not in hosts and len(hosts) < 10:
            hosts.append(f"{host}:{port}")
        if reason not in reasons and len(reasons) < 10:
            reasons.append(reason)
    return {"model_requests": model, "egress_allowed": allowed, "egress_denied": denied,
            "denied_hosts": hosts, "denied_reasons": sorted(reasons)}


def _overlap(a: str, b: str) -> bool:
    pa, pb = PurePosixPath(a), PurePosixPath(b)
    return pa == pb or pa in pb.parents or pb in pa.parents


def _mount(src: str, dst: str, *, ro: bool) -> list[str]:
    for p in (src, dst):
        if _MOUNT_UNSAFE.search(p):
            raise SessionRefused("a mount path contains a character that is not safe in a mount specification")
    return ["--mount", f"type=bind,src={src},dst={dst}" + (",readonly" if ro else "")]


def _tmpfs(dst: str, size: str, *, exec_: bool, owned: bool) -> list[str]:
    opts = f"rw,nosuid,{'exec' if exec_ else 'noexec'},size={size}"
    opts += ",uid=65534,gid=65534,mode=0700" if owned else ",mode=1777"
    return ["--tmpfs", f"{dst}:{opts}"]


@dataclass(frozen=True)
class Names:
    run_id: str

    @property
    def internal(self) -> str: return f"h2s-{self.run_id}-int"
    @property
    def egress(self) -> str: return f"h2s-{self.run_id}-egr"
    @property
    def proxy(self) -> str: return f"h2s-{self.run_id}-proxy"
    @property
    def session(self) -> str: return f"h2s-{self.run_id}-session"
    @property
    def reclaim(self) -> str: return f"h2s-{self.run_id}-reclaim"


@dataclass(frozen=True)
class Plan:
    """Everything validated; nothing here has touched docker."""
    spec: SessionSpec
    image: ImageRef
    workspace: str
    cli_host: str
    cli_container: str
    allow: tuple[tuple[str, int], ...]        # hosted: the exact (host, port) pairs the proxy permits
    bridge_target: tuple[str, int] | None     # local: the loopback address the host bridge exposes
    local_port: int | None
    egress_network: str | None                # an existing network to use for egress (else one is created)
    test_allow_cidrs: tuple[str, ...] = ()

    @property
    def local(self) -> bool:
        return self.bridge_target is not None


def proxy_config(plan: Plan) -> dict:
    cfg: dict = {"listen_host": "0.0.0.0", "listen_port": PROXY_PORT, "allow": [list(a) for a in plan.allow]}
    if plan.bridge_target is not None:
        sock = f"{BRIDGE_MOUNT}/ollama.sock"
        host, port = plan.bridge_target
        # Two names, one socket: the loopback address a forward-proxy client names (a CONNECT or absolute-URI request),
        # and the proxy alias the CLI's provider base URL uses (`runner.local_endpoint`), which a proxy-aware client sends
        # THROUGH the proxy now that the alias is not in NO_PROXY. A client that connects to the alias directly lands on
        # the listener below, which relays to the same socket. Nothing else is routed.
        cfg["unix_routes"] = [{"host": host, "port": port, "unix": sock},
                              {"host": PROXY_ALIAS, "port": port, "unix": sock}]
        cfg["listeners"] = [{"port": port, "unix": sock}]
    if plan.test_allow_cidrs:
        cfg["test_allow_cidrs"] = list(plan.test_allow_cidrs)
    return cfg


SESSION_NO_PROXY = "127.0.0.1,localhost,::1"


def session_env(plan: Plan) -> dict[str, str]:
    """The complete environment of the session container. Nothing is inherited from the host.

    NO_PROXY is the session container's OWN loopback and nothing else, set here and never by the caller (F3). A CLI such
    as `opencode run --standalone` starts a private HTTP server on a random 127.0.0.1 port and talks to it with a
    proxy-aware client; without this its own traffic went to the proxy and was denied (the first real stage-1 run).
    That loopback is empty of everything but what the session starts itself: the host's loopback is unreachable from
    the container, and the proxy, the model endpoint and every external name are NOT in the list, so they are reached
    only through the proxy (or not at all). For a local channel the model endpoint is `http://harness2-proxy.internal:<port>`:
    it is not bypassed, it goes THROUGH the proxy as a request for that exact name and port (see `proxy_config`)."""
    proxy = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    env = {"HOME": HOME, "TMPDIR": "/tmp", "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
           "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
           "NO_PROXY": SESSION_NO_PROXY, "no_proxy": SESSION_NO_PROXY,
           "NODE_USE_ENV_PROXY": "1", "TERM": "dumb", "NO_COLOR": "1", "LANG": "C.UTF-8",
           "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    cred = plan.spec.credential
    if cred is not None and cred.env_var:
        env[cred.env_var] = cred.container_path
    env.update(plan.spec.env)
    return env


def build_proxy_create(plan: Plan, names: Names, *, docker: str, host: str, proxy_dir: str,
                       bridge_dir: str | None) -> list[str]:
    lim = plan.spec.limits
    argv = [docker, "-H", host, "create", "--name", names.proxy, "--label", f"{LABEL}={names.run_id}",
            "--label", f"{LABEL}.component=proxy", "--pull", "never",
            "--network", names.internal, "--network-alias", PROXY_ALIAS,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(lim.proxy_pids), "--memory", lim.proxy_memory, "--memory-swap", lim.proxy_memory,
            "--cpus", "1", "--ulimit", "core=0",
            # The bridge socket is owned by the host user, which is container root in a rootless daemon; the
            # unprivileged user could not open it. Root with every capability dropped can, and can do nothing else.
            "--user", "0:0" if bridge_dir else USER,
            "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
            *_tmpfs("/tmp", "16m", exec_=False, owned=False),
            *_mount(proxy_dir, PROXY_CONFIG_MOUNT, ro=True)]
    if bridge_dir:
        argv += _mount(bridge_dir, BRIDGE_MOUNT, ro=True)
    argv += ["--entrypoint", "python3", plan.image.id, "-I", PROXY_PATH_IN_IMAGE, "--config",
             f"{PROXY_CONFIG_MOUNT}/proxy.json"]
    return argv


def build_session_run(plan: Plan, names: Names, *, docker: str, host: str, cred_file: str | None) -> list[str]:
    spec, lim = plan.spec, plan.spec.limits
    argv = [docker, "-H", host, "run", "--rm", "--name", names.session, "--label", f"{LABEL}={names.run_id}",
            "--label", f"{LABEL}.component=session", "--pull", "never"]
    if spec.stdin is not None:
        argv.append("-i")
    argv += ["--network", names.internal, "--dns", "127.0.0.1",
             "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--pids-limit", str(lim.pids), "--memory", lim.memory, "--memory-swap", lim.memory, "--cpus", lim.cpus,
             "--ulimit", "core=0", "--ulimit", "nofile=4096:4096",
             "--ulimit", f"fsize={lim.file_bytes}:{lim.file_bytes}", "--user", USER,
             *_tmpfs("/tmp", lim.tmpfs_size, exec_=spec.tmp_exec, owned=False),
             *_tmpfs(HOME, lim.tmpfs_size, exec_=spec.tmp_exec, owned=True)]
    for d in spec.tmpfs_dirs:
        argv += _tmpfs(d, lim.tmpfs_size, exec_=spec.tmp_exec, owned=True)
    argv += _mount(plan.cli_host, plan.cli_container, ro=True)
    argv += _mount(plan.workspace, plan.workspace, ro=False)
    if spec.credential is not None and cred_file is not None:
        argv += _mount(cred_file, spec.credential.container_path, ro=True)
    argv += ["--workdir", plan.workspace]
    for k, v in session_env(plan).items():
        argv += ["-e", f"{k}={v}"]
    argv += ["--entrypoint", "", plan.image.id, *spec.argv]
    return argv


RECLAIM_SCRIPT = ('set -eu; find "$1" -xdev ! -type l -exec chown 0:0 {} +; '
                  'find "$1" -xdev -type d -exec chmod u+rwx {} +; find "$1" -xdev -type f -exec chmod u+rw {} +')


def build_reclaim_run(plan: Plan, names: Names, *, docker: str, host: str) -> list[str]:
    """A one-shot container that gives the host user the files the container user created in the workspace."""
    return [docker, "-H", host, "run", "--rm", "--name", names.reclaim, "--label", f"{LABEL}={names.run_id}",
            "--label", f"{LABEL}.component=reclaim", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
            "--user", "0:0", *_mount(plan.workspace, plan.workspace, ro=False),
            "--entrypoint", "sh", plan.image.id, "-c", RECLAIM_SCRIPT, "sh", plan.workspace]


# ── validation ────────────────────────────────────────────────────────────────────

def _check_workspace(ws: object, roots: Sequence[str], cli_container: str, cred: Credential | None,
                     tmpfs_dirs: Sequence[str]) -> str:
    if not isinstance(ws, str) or not ws or "\0" in ws:
        raise SessionRefused("the workspace must be a path string")
    why = channels.bad_directory(ws)
    if why:
        raise SessionRefused(f"the workspace {why}")
    if _MOUNT_UNSAFE.search(ws):
        raise SessionRefused("the workspace path contains a character that is not safe in a mount specification")
    norm = os.path.normpath(ws)
    if norm != ws:
        raise SessionRefused("the workspace path must be normalised (no '..', '.', or doubled slashes)")
    if os.path.realpath(ws) != ws:
        raise SessionRefused("the workspace path contains a symlink; it is mounted at the same path in the container")
    if not os.path.isdir(ws):
        raise SessionRefused("the workspace is not an existing directory")
    if any(ws == p or ws.startswith(p + "/") for p in _BAD_WS_PREFIXES):
        raise SessionRefused("the workspace is in a system location")
    reserved = ["/tmp", HOME, CLI_DIR, cli_container, *tmpfs_dirs] + ([cred.container_path] if cred else [])
    if any(_overlap(ws, r) and not (ws.startswith("/tmp/") and r == "/tmp") for r in reserved if r):
        raise SessionRefused("the workspace overlaps a path the session container mounts for itself")
    if roots and not any(ws == r.rstrip("/") or ws.startswith(r.rstrip("/") + "/") for r in roots):
        raise SessionRefused("the workspace is not under any workspace root of the channel profile")
    return ws


def _check_cli(cli: object) -> tuple[str, str]:
    if not isinstance(cli, str) or not os.path.isabs(cli) or "\0" in cli:
        raise SessionRefused("the CLI path must be an absolute host path")
    real = os.path.realpath(cli)
    try:
        st = os.stat(real)
    except OSError:
        raise SessionRefused("the CLI does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise SessionRefused("the CLI must be a regular file (a symlink to one is resolved)")
    name = os.path.basename(cli)
    if not _CLI_NAME.fullmatch(name):
        raise SessionRefused("the CLI file name has characters outside letters, digits and . _ -")
    if "docker.sock" in real or _MOUNT_UNSAFE.search(real):
        raise SessionRefused("the CLI path is not acceptable")
    return real, container_cli_path(cli)


def _check_argv(argv: object, cli_container: str, secrets_: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(a, str) for a in argv):
        raise SessionRefused("argv must be a non-empty sequence of strings")
    if argv[0] != cli_container:
        raise SessionRefused(f"argv[0] must be the mounted CLI path {cli_container}; the session runs nothing else")
    if any("\0" in a for a in argv) or sum(len(a.encode("utf-8", "replace")) for a in argv) > MAX_ARGV_BYTES:
        raise SessionRefused("argv contains a NUL byte or is too large")
    if any(s in a for s in secrets_ for a in argv):
        raise SessionRefused("argv would carry a credential value; credentials go through the credential file only")
    return tuple(argv)


def _check_env(env: object, secrets_: Sequence[str]) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise SessionRefused("env must be a mapping")
    out: dict[str, str] = {}
    for k, v in env.items():
        if not (isinstance(k, str) and isinstance(v, str) and _ENV_NAME.fullmatch(k)):
            raise SessionRefused("env names must be UPPER_SNAKE strings and values strings")
        if k in _FIXED_ENV or k.upper().endswith("_PROXY") or k.startswith("DOCKER_"):
            raise SessionRefused(f"env {k} is set by the runner (or is a proxy/docker variable) and cannot be overridden")
        if upstream_egress._is_secret_name(k):
            raise SessionRefused(f"env {k} looks like a credential; credentials go through the credential file only")
        if len(v) > MAX_ENV_VALUE or any(c in v for c in "\0\n\r") or any(s in v for s in secrets_):
            raise SessionRefused(f"env {k} has an unacceptable value")
        out[k] = v
    return out


# ── the runner ────────────────────────────────────────────────────────────────────

class ProposerSessionRunner:
    def __init__(self, verified: sandbox.VerifiedDocker, image: ImageRef, channel: channels.Channel, *,
                 zdr: str, classification: str | None,
                 proposer_decision: upstream_egress.Decision | None = None,
                 workspace_roots: Sequence[str] = (), egress_network: str | None = None,
                 role: str = "proposer", budget: SweepBudget | None = None,
                 observer: Callable[[str, Mapping[str, object]], None] | None = None,
                 _test_allow_cidrs: Sequence[str] = ()):
        """`role` is "proposer" (the default, every existing caller) or "solver". A solver session is the same
        contained session with the same hardening and the same channel decision; it differs only in that no
        `upstream_egress.Decision` is demanded (that decision vets an upstream Proposer's configuration, and a
        solver's command line is built by this code, not read from a Proposer). `budget`, when given, is the
        sweep's shared `SweepBudget`: every `run` reserves from it first and records how it ended.
        `observer(event, fields)`, when given, is told what happened to each session (`session_refused`,
        `session_reserved`, `session_start`, `session_end`) for an audit trail. The fields are numbers, short words
        and the session's run id: never an argv, an environment, a prompt or a model's output. An observer that
        raises never affects a session (the failure is counted in `observer_errors`)."""
        if role not in ROLES:
            raise SessionRefused(f"the session role must be one of {ROLES}")
        if budget is not None and not isinstance(budget, SweepBudget):
            raise SessionRefused("budget must be a SweepBudget")
        if observer is not None and not callable(observer):
            raise SessionRefused("observer must be a callable (event, fields)")
        if not isinstance(verified, sandbox.VerifiedDocker) or not sandbox._is_local_unix(verified.host):
            raise SessionRefused("the runner needs the endpoint the rootless gate verified (a local unix socket)")
        if not isinstance(image, ImageRef):
            raise SessionRefused("the image must be a pinned ImageRef (see cognitive_harness2.image.resolve_image)")
        if not isinstance(channel, channels.Channel):
            raise SessionRefused("the channel must be a declared channels.Channel")
        if egress_network is not None and (not _NET_NAME.fullmatch(egress_network)
                                           or egress_network in ("host", "none", "bridge")):
            raise SessionRefused("egress_network must name an existing user-defined network")
        self.verified, self.image, self.channel = verified, image, channel
        self.zdr, self.classification, self.proposer_decision = zdr, classification, proposer_decision
        self.workspace_roots, self.egress_network = tuple(workspace_roots), egress_network
        self._test_allow_cidrs = tuple(_test_allow_cidrs)
        self.role, self.budget = role, budget
        self.observer, self.observer_errors = observer, 0

    def _notify(self, event: str, **fields: object) -> None:
        if self.observer is None:
            return
        try:
            self.observer(event, {"role": self.role, **fields})
        except Exception:  # noqa: BLE001 - an audit failure never changes what a session does
            self.observer_errors += 1

    # ── authorisation: everything that can be checked without touching docker, then the gate ─────────

    @property
    def local_endpoint(self) -> str | None:
        """For a local channel, the base URL the session reaches the model at (the proxy's listener)."""
        keys = [k for k in self.channel.endpoint_keys if k[0] in egress.LOOPBACK]
        return f"http://{PROXY_ALIAS}:{keys[0][1]}" if keys else None

    def authorize(self, spec: SessionSpec) -> Plan:
        """Raise `SessionRefused` unless this session may start. Starts nothing; the docker gate is separate."""
        if not isinstance(spec, SessionSpec):
            raise SessionRefused("spec must be a SessionSpec")
        ch = self.channel
        verdict = channels.decide(ch, zdr=self.zdr, classification=self.classification)
        if not verdict.allowed:
            raise SessionRefused("the channel decision is a refusal: " + "; ".join(verdict.reasons))
        if self.role == "solver":
            d = self.proposer_decision
            if d is not None and not (isinstance(d, upstream_egress.Decision) and d.allowed is True):
                raise SessionRefused("the proposer decision is a refusal")
        elif ch.kind in upstream_egress.KINDS:
            d = self.proposer_decision
            if not isinstance(d, upstream_egress.Decision) or d.allowed is not True:
                raise SessionRefused("the proposer decision (upstream_egress.check_proposer) is missing or a refusal")
        elif self.proposer_decision is not None and self.proposer_decision.allowed is not True:
            raise SessionRefused("the proposer decision is a refusal")
        t = spec.timeout_s
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= ch.caps.wall_s:
            raise SessionRefused(f"timeout_s must be above 0 and within the channel's wall_s cap ({ch.caps.wall_s})")
        spec.limits.check()

        cli_host, cli_container = _check_cli(spec.cli)
        secrets_ = [spec.credential.value] if spec.credential else []
        _check_argv(spec.argv, cli_container, secrets_)
        _check_env(spec.env, secrets_)
        for d in spec.tmpfs_dirs:
            if not (isinstance(d, str) and d.startswith("/") and not _MOUNT_UNSAFE.search(d) and ".." not in d.split("/")
                    and d.startswith(HOME + "/")):
                raise SessionRefused("tmpfs_dirs must be clean absolute paths under the session HOME")
        ws = _check_workspace(spec.workspace, self.workspace_roots, cli_container, spec.credential, spec.tmpfs_dirs)
        if _overlap(ws, cli_host):
            raise SessionRefused("the CLI lives inside the workspace")

        keys = sorted(ch.endpoint_keys)
        loop = [k for k in keys if k[0] in egress.LOOPBACK]
        bridge: tuple[str, int] | None = None
        allow: tuple[tuple[str, int], ...] = ()
        if loop:
            if len(loop) != len(keys) or len(loop) != 1:
                raise SessionRefused("a channel mixing loopback and remote endpoints, or several loopback ones, is not supported")
            host = "127.0.0.1" if loop[0][0] == "localhost" else loop[0][0]
            try:
                bridge = validate_target(host, loop[0][1])
            except ValueError as e:
                raise SessionRefused(f"the local endpoint cannot be bridged: {e}") from None
        else:
            allow = tuple(keys)
        return Plan(spec=spec, image=self.image, workspace=ws, cli_host=cli_host, cli_container=cli_container,
                    allow=allow, bridge_target=bridge, local_port=loop[0][1] if loop else None,
                    egress_network=self.egress_network, test_allow_cidrs=self._test_allow_cidrs)

    # ── docker plumbing ───────────────────────────────────────────────────────────

    def _docker(self, args: Sequence[str], *, timeout: float = _DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess:
        return subprocess.run([self.verified.path, "-H", self.verified.host, *args], capture_output=True, text=True,
                              timeout=timeout, env=sandbox._pinned_env(), stdin=subprocess.DEVNULL)

    def _must(self, args: Sequence[str]) -> str:
        try:
            p = self._docker(args)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise SessionError(f"docker {args[0]} failed: {type(e).__name__}") from None
        if p.returncode != 0:
            raise SessionError(f"docker {' '.join(args[:2])} failed: {sandbox._first_line(p.stderr)}")
        return p.stdout

    def _quiet(self, args: Sequence[str]) -> None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            self._docker(args)

    def _argv_for(self, argv: list[str]) -> list[str]:
        """The `create`/`run` argv from the pure builders, minus the leading docker, -H, host."""
        return argv[3:]

    def _ids(self, what: str, run_id: str) -> list[str]:
        cmd = ["ps", "-a", "-q"] if what == "containers" else ["network", "ls", "-q"]
        try:
            p = self._docker([*cmd, "--filter", f"label={LABEL}={run_id}"])
        except (subprocess.TimeoutExpired, OSError):
            return [f"{what}: could not be listed"]
        return p.stdout.split()

    def _wait_ready(self, names: Names) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        tail = ""
        while time.monotonic() < deadline:
            try:
                logs = self._docker(["logs", names.proxy])
                tail = (logs.stdout + logs.stderr)
                if READY_LINE in tail:
                    return
                state = self._docker(["inspect", "--format", "{{.State.Running}}", names.proxy]).stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                state = "true"
            if state == "false":
                raise SessionError("the proxy container exited before it was ready: "
                                   + sandbox._first_line(tail.splitlines()[-1] if tail.strip() else ""))
            time.sleep(0.2)
        raise SessionError("the proxy container did not become ready in time")

    def _access_log(self, names: Names) -> tuple[str, ...]:
        try:
            p = self._docker(["logs", names.proxy])
        except (subprocess.TimeoutExpired, OSError):
            return ()
        lines, size = [], 0
        for line in (p.stdout + p.stderr).splitlines():
            if line.startswith("ACCESS "):
                size += len(line)
                if size > MAX_ACCESS_LOG_BYTES:
                    break
                lines.append(sanitize_terminal(line))
        return tuple(lines)

    def _gate(self) -> None:
        sandbox.verify_rootless_docker(lambda _: self.verified.path, image=self.image.id, host=self.verified.host)

    # ── a session ─────────────────────────────────────────────────────────────────

    def run(self, spec: SessionSpec) -> SessionResult:
        """Authorise, reserve from the sweep budget (if any), pass the docker gate, then run one session.

        Raises `SessionRefused` (or `sweep_budget.BudgetExhausted`) before anything is created when a
        precondition fails. With a budget, `spec.timeout_s` is clipped to the reservation's, so a session that
        starts before the sweep's deadline also ends by it; the reservation is released however the session ends,
        and refunded only if it never started (the gate failed).
        """
        try:
            plan = self.authorize(spec)
        except SessionRefused as e:
            self._notify("session_refused", reason="refused: " + sanitize_terminal(str(e))[:160])
            raise
        reservation = None
        if self.budget is not None:
            try:
                reservation = self.budget.reserve_session(self.role)
            except BudgetExhausted as e:
                self._notify("session_refused", reason=f"budget:{e.reason}")
                raise
            clipped = min(spec.timeout_s, reservation.timeout_s)
            plan = dataclasses.replace(plan, spec=dataclasses.replace(spec, timeout_s=clipped))
            self._notify("session_reserved", reservation=reservation.id, timeout_s=clipped)
        started, outcome = False, "error"
        try:
            # The gate runs again for every session: the daemon can be stopped or swapped between runs. It is checked
            # against the pinned image id, and pins every later call to the endpoint it verified.
            try:
                self._gate()
            except BaseException as e:
                self._notify("session_refused", reason=f"gate:{type(e).__name__}")
                raise
            started = True
            result = self._execute(plan)
            outcome = "timed_out" if result.timed_out else "ok"
            return result
        except SessionCleanupError:
            outcome = "leftovers"
            raise
        except BaseException:
            if not started:
                outcome = "refused"
            raise
        finally:
            if reservation is not None:
                self.budget.release(reservation, started=started, outcome=outcome)  # type: ignore[union-attr]

    def _execute(self, plan: Plan) -> SessionResult:
        spec = plan.spec
        run_id = secrets.token_hex(6)
        names = Names(run_id)
        secrets_ = [spec.credential.value] if spec.credential else []
        base = Path(tempfile.mkdtemp(prefix="h2s-", dir=_runtime_base_dir())).resolve()
        result: SessionResult | None = None
        error: BaseException | None = None
        created_networks: list[str] = []
        reclaimed: bool | None = None
        stack = contextlib.ExitStack()
        t0 = time.monotonic()
        self._notify("session_start", session=run_id, timeout_s=spec.timeout_s, tmp_exec=spec.tmp_exec)
        try:
            os.chmod(base, 0o700)
            proxy_dir = base / "proxy"
            proxy_dir.mkdir(mode=0o755)
            os.chmod(proxy_dir, 0o755)           # the container user is not the owner; the 0700 parent hides it
            cfg_file = proxy_dir / "proxy.json"
            cfg_file.write_text(json.dumps(proxy_config(plan)))
            os.chmod(cfg_file, 0o444)
            cred_path: str | None = None
            if spec.credential is not None:
                cred_dir = base / "cred"
                cred_dir.mkdir(mode=0o711)
                os.chmod(cred_dir, 0o711)
                f = cred_dir / secrets.token_hex(12)
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
                with os.fdopen(fd, "w") as fh:
                    fh.write(spec.credential.value)
                os.chmod(f, 0o444)
                cred_path = str(f)
            bridge_dir: str | None = None
            if plan.local:
                bridge = stack.enter_context(HostBridge(*plan.bridge_target))
                bridge_dir = str(bridge.directory)

            self._must(["network", "create", "--internal", "--label", f"{LABEL}={run_id}", names.internal])
            created_networks.append(names.internal)
            egress_net = plan.egress_network
            if not plan.local and egress_net is None:
                self._must(["network", "create", "--label", f"{LABEL}={run_id}", names.egress])
                created_networks.append(names.egress)
                egress_net = names.egress
            self._must(self._argv_for(build_proxy_create(plan, names, docker=self.verified.path, host=self.verified.host,
                                                         proxy_dir=str(proxy_dir), bridge_dir=bridge_dir)))
            if not plan.local:
                self._must(["network", "connect", egress_net, names.proxy])
            self._must(["start", names.proxy])
            self._wait_ready(names)

            argv = build_session_run(plan, names, docker=self.verified.path, host=self.verified.host, cred_file=cred_path)
            out, err, cut = bytearray(), bytearray(), [False]
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if spec.stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sandbox._pinned_env())
            try:
                threads = [threading.Thread(target=sandbox._drain, args=(s, spec.limits.max_output_bytes, b, cut), daemon=True)
                           for s, b in ((proc.stdout, out), (proc.stderr, err))]
                if spec.stdin is not None:
                    def feed() -> None:
                        with contextlib.suppress(OSError):
                            proc.stdin.write(spec.stdin)  # type: ignore[union-attr]
                            proc.stdin.close()            # type: ignore[union-attr]
                    threads.append(threading.Thread(target=feed, daemon=True))
                for th in threads:
                    th.start()
                timed_out = False
                try:
                    code: int | None = proc.wait(timeout=spec.timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out, code = True, None
                    self._quiet(["kill", names.session])
                    self._quiet(["rm", "-f", names.session])
                    try:
                        proc.wait(timeout=_DOCKER_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                for th in threads:
                    th.join(timeout=_DOCKER_TIMEOUT_S)
            finally:
                if proc.poll() is None:
                    proc.kill()
            log = self._access_log(names)
            note = f"\n[output truncated at {spec.limits.max_output_bytes} bytes]\n" if cut[0] else ""
            stdout = redact(sanitize_terminal(out.decode("utf-8", "replace")), secrets_) + note
            stderr = redact(sanitize_terminal(err.decode("utf-8", "replace")), secrets_)
            result = SessionResult(run_id, code, stdout, "timed out\n" + stderr if timed_out else stderr, timed_out,
                                   cut[0], time.monotonic() - t0, log)
        except BaseException as e:   # noqa: BLE001 - recorded, cleanup runs, then re-raised
            error = e
        finally:
            self._teardown(names, base, created_networks, stack)
            if spec.reclaim_workspace:
                reclaimed = self._reclaim(plan, names)
            leftovers = tuple(self._leftovers(names, base))
        self._notify("session_end", session=run_id, exit_code=None if result is None else result.exit_code,
                     timed_out=None if result is None else result.timed_out,
                     truncated=None if result is None else result.truncated,
                     duration_s=round(time.monotonic() - t0, 3), reclaimed=reclaimed, leftovers=len(leftovers),
                     error=None if error is None else type(error).__name__,
                     **(egress_summary(result.egress_log) if result is not None else {}))
        if error is not None:
            if leftovers:
                raise SessionCleanupError(leftovers) from error
            raise error
        assert result is not None
        report = None
        if spec.harvest is not None:
            report = workspace_harvest.inspect_workspace(plan.workspace, spec.harvest, credentials=secrets_)
        result = SessionResult(**{**result.__dict__, "harvest": report, "reclaimed": reclaimed, "leftovers": leftovers,
                                        "role": self.role})
        if leftovers:
            raise SessionCleanupError(leftovers, result)
        return result

    # ── teardown ──────────────────────────────────────────────────────────────────

    def _teardown(self, names: Names, base: Path, networks: list[str], stack: contextlib.ExitStack) -> None:
        for c in (names.session, names.proxy):
            self._quiet(["kill", c])
            self._quiet(["rm", "-f", c])
        for n in networks:
            for _ in range(10):
                try:
                    if self._docker(["network", "rm", n]).returncode == 0:
                        break
                except (subprocess.TimeoutExpired, OSError):
                    pass
                time.sleep(0.5)
        with contextlib.suppress(Exception):
            stack.close()
        shutil.rmtree(base, ignore_errors=True)

    def _reclaim(self, plan: Plan, names: Names) -> bool:
        try:
            p = self._docker(build_reclaim_run(plan, names, docker=self.verified.path, host=self.verified.host)[3:],
                             timeout=120)
            ok = p.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
        self._quiet(["rm", "-f", names.reclaim])
        return ok

    def _leftovers(self, names: Names, base: Path) -> list[str]:
        left = [f"container {i}" for i in self._ids("containers", names.run_id)]
        left += [f"network {i}" for i in self._ids("networks", names.run_id)]
        if base.exists():
            left.append(f"directory {base}")
        return left

    # ── housekeeping for crashes ──────────────────────────────────────────────────

    def reap_all(self) -> list[str]:
        """Remove every container and network carrying the session label (after a crash of an earlier process)."""
        removed: list[str] = []
        ids = self._docker(["ps", "-a", "-q", "--filter", f"label={LABEL}"]).stdout.split()
        for i in ids:
            self._quiet(["rm", "-f", i])
            removed.append(f"container {i}")
        for n in self._docker(["network", "ls", "-q", "--filter", f"label={LABEL}"]).stdout.split():
            self._quiet(["network", "rm", n])
            removed.append(f"network {n}")
        return removed


def _runtime_base_dir() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return xdg if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK) else tempfile.gettempdir()
