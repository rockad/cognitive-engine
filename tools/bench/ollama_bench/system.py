"""Host measurements: load average, available RAM, GPU memory sampling, and the noise gate."""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .scoring import AVAIL_RAM_MIN_GB, LOAD_AVG_MAX, NOISE_POLL_S, NOISE_WAIT_MAX_S


def load_state() -> dict[str, float]:
    """1/5/15-minute load average and MemAvailable in GB, from /proc."""
    la = Path("/proc/loadavg").read_text().split()
    avail_kb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
    return {"load1": float(la[0]), "load5": float(la[1]), "load15": float(la[2]),
            "mem_available_gb": round(avail_kb / 1024 / 1024, 2)}


def gpu_used_mib() -> int | None:
    """Total GPU memory in use (MiB), or None when nvidia-smi is unavailable."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def gpu_total_mib() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class GpuSampler:
    """Samples GPU memory every `interval_s` on a thread; `mark(name)` keeps a per-phase peak."""

    def __init__(self, read: Callable[[], int | None] = gpu_used_mib, interval_s: float = 1.0):
        self._read, self._interval = read, interval_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.samples: list[tuple[float, int]] = []
        self.phase_peaks: dict[str, int] = {}
        self._phase = "init"

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            v = self._read()
            if v is not None:
                with self._lock:
                    self.samples.append((time.time(), v))
                    self.phase_peaks[self._phase] = max(self.phase_peaks.get(self._phase, 0), v)
            self._stop.wait(self._interval)

    def mark(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            vals = [v for _, v in self.samples]
            return {"n_samples": len(vals), "peak_mib": max(vals) if vals else None,
                    "min_mib": min(vals) if vals else None, "phase_peaks_mib": dict(self.phase_peaks)}


@dataclass
class NoiseReport:
    before: dict[str, float]
    waited_s: float = 0.0
    polls: list[dict[str, float]] = field(default_factory=list)
    settled: bool = True            # False when the limit was still exceeded after the maximum wait


def quiet(state: dict[str, float]) -> bool:
    return state["load1"] <= LOAD_AVG_MAX and state["mem_available_gb"] >= AVAIL_RAM_MIN_GB


def wait_for_quiet(read: Callable[[], dict[str, float]] = load_state, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic, max_wait_s: float = NOISE_WAIT_MAX_S,
                   poll_s: float = NOISE_POLL_S) -> NoiseReport:
    """Wait up to `max_wait_s` for load average <= 4 and >= 3 GB available RAM; then go on and say so."""
    first = read()
    rep = NoiseReport(before=first)
    if quiet(first):
        return rep
    t0 = clock()
    while clock() - t0 < max_wait_s:
        sleep(poll_s)
        st = read()
        rep.polls.append(st)
        if quiet(st):
            rep.waited_s = round(clock() - t0, 1)
            return rep
    rep.waited_s = round(clock() - t0, 1)
    rep.settled = False
    return rep
