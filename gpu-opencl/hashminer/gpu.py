"""OpenCL device discovery, the per-device Keccak-256 search worker, and the
multi-GPU farm that runs one worker thread per device.

Nothing here talks to Ethereum - it only hashes. The farm is fed a "job"
``(challenge, target, epoch)`` and emits ``Found`` records onto a queue; the
orchestrator (``miner.py``) verifies and submits them.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from importlib import resources

import numpy as np

try:  # pyopencl is required for everything except `import`-level use
    import pyopencl as cl
except Exception:  # pragma: no cover
    cl = None  # type: ignore

log = logging.getLogger("hashminer.gpu")

KERNEL_NAME = "keccak_pow_search"
_DEFAULT_LOCAL_SIZE = 256
_DEFAULT_MAX_RESULTS = 4096
_MIN_BATCH = 1 << 18
_MAX_BATCH = 1 << 28
_TARGET_BATCH_MS = 250.0  # larger launches reduce Python/OpenCL overhead on fast RTX cards


def _require_cl() -> None:
    if cl is None:  # pragma: no cover
        raise RuntimeError("pyopencl is not installed / failed to import - `pip install pyopencl`")


def load_kernel_source() -> str:
    return resources.files("hashminer.kernels").joinpath("keccak256.cl").read_text(encoding="utf-8")


@dataclass(frozen=True)
class DeviceInfo:
    index: int                 # flat index across all platforms (stable for a given driver/order)
    platform_name: str
    name: str
    type: str                  # human string, e.g. "GPU" or "ALL | GPU"
    is_gpu: bool
    compute_units: int
    global_mem_mb: int
    max_work_group_size: int
    _device: object = field(repr=False, default=None)

    def describe(self) -> str:
        return (f"[{self.index}] {self.name}  ({self.type}, {self.compute_units} CU, "
                f"{self.global_mem_mb} MB, plat='{self.platform_name}')")


def list_devices() -> list[DeviceInfo]:
    """All OpenCL devices, flattened and indexed in platform/device enumeration order."""
    _require_cl()
    out: list[DeviceInfo] = []
    i = 0
    try:
        platforms = cl.get_platforms()
    except Exception as exc:  # noqa: BLE001 - no OpenCL ICD/platform installed on CI/headless boxes
        if "PLATFORM_NOT_FOUND" in str(exc):
            return []
        raise
    for plat in platforms:
        for dev in plat.get_devices():
            out.append(DeviceInfo(
                index=i,
                platform_name=plat.name.strip(),
                name=dev.name.strip(),
                type=cl.device_type.to_string(dev.type),
                is_gpu=bool(dev.type & cl.device_type.GPU),
                compute_units=dev.max_compute_units,
                global_mem_mb=int(dev.global_mem_size // (1024 * 1024)),
                max_work_group_size=dev.max_work_group_size,
                _device=dev,
            ))
            i += 1
    return out


def select_devices(spec: object) -> list[DeviceInfo]:
    """`spec` is "all" / None for every GPU (fallback: every device), or a list of flat indices."""
    devs = list_devices()
    if not devs:
        raise RuntimeError("no OpenCL devices found")
    if spec in (None, "all", "ALL", "*"):
        gpus = [d for d in devs if d.is_gpu]
        return gpus or devs
    chosen = []
    for idx in spec:  # type: ignore[union-attr]
        match = [d for d in devs if d.index == int(idx)]
        if not match:
            raise ValueError(f"no OpenCL device with index {idx}; run `hashminer devices`")
        chosen.append(match[0])
    return chosen


# ---------------------------------------------------------------------------
# encoding helpers (mirror reference/SPEC.md; the host does all byte juggling)
# ---------------------------------------------------------------------------
_M64 = (1 << 64) - 1


def challenge_lanes(challenge: bytes) -> tuple[int, int, int, int]:
    """challenge -> 4 little-endian 64-bit lanes (XORed directly into state lanes A[0..3])."""
    if len(challenge) != 32:
        raise ValueError("challenge must be 32 bytes")
    return tuple(int.from_bytes(challenge[8 * i:8 * i + 8], "little") for i in range(4))  # type: ignore[return-value]


def target_limbs(target: int) -> tuple[int, int, int, int]:
    """target (== on-chain currentDifficulty) -> 4 big-endian 64-bit limbs, MS limb first."""
    if not 0 <= target < (1 << 256):
        raise ValueError("target out of uint256 range")
    return ((target >> 192) & _M64, (target >> 128) & _M64, (target >> 64) & _M64, target & _M64)


def _nonce_limbs_le(nonce_base: int) -> tuple[int, int, int, int]:
    return (nonce_base & _M64, (nonce_base >> 64) & _M64, (nonce_base >> 128) & _M64, (nonce_base >> 192) & _M64)


@dataclass
class Found:
    nonce: int
    epoch: int
    device_index: int


class GpuWorker:
    """One OpenCL device. Not thread-safe; the farm gives each worker its own thread."""

    def __init__(self, info: DeviceInfo, *, kernel_src: str | None = None,
                 local_size: int | None = None, max_results: int = _DEFAULT_MAX_RESULTS,
                 batch_target_ms: float | None = None):
        _require_cl()
        import warnings
        warnings.filterwarnings("ignore", category=cl.CompilerWarning)  # NVIDIA emits a benign note
        self.info = info
        self.ctx = cl.Context(devices=[info._device])
        self.queue = cl.CommandQueue(self.ctx)
        self.program = cl.Program(self.ctx, kernel_src or load_kernel_source()).build()
        self.kernel = getattr(self.program, KERNEL_NAME)
        ls = local_size or _DEFAULT_LOCAL_SIZE
        self.local_size = max(1, min(ls, info.max_work_group_size))
        self.max_results = max_results
        self.batch_target_ms = float(batch_target_ms or _TARGET_BATCH_MS)
        mf = cl.mem_flags
        self._out_count = cl.Buffer(self.ctx, mf.READ_WRITE, 4)
        self._out_nonces = cl.Buffer(self.ctx, mf.WRITE_ONLY, max_results * 4 * 8)
        self._salt = secrets.randbits(64)  # high-order tag so devices never share a nonce range
        self.batch_size = 1 << 24
        self.total_hashes = 0
        self.total_seconds = 0.0

    def _random_base(self) -> int:
        # top 64 bits = per-device salt, low 192 bits random; consecutive [base, base+n) within a batch.
        return (self._salt << 192) | secrets.randbits(192)

    def search_batch(self, challenge: bytes, target: int, *, n: int | None = None,
                     nonce_base: int | None = None) -> tuple[list[int], int, float]:
        """Run one launch. Returns (found_nonces, n_tried, seconds). `found_nonces` may be
        truncated to max_results (the kernel still reports the true count internally; we cap)."""
        n = int(n or self.batch_size)
        nonce_base = self._random_base() if nonce_base is None else int(nonce_base)
        ch = challenge_lanes(challenge)
        tg = target_limbs(target)
        nb = _nonce_limbs_le(nonce_base)
        gsize = ((n + self.local_size - 1) // self.local_size) * self.local_size  # round up to multiple of local_size

        cl.enqueue_fill_buffer(self.queue, self._out_count, np.uint32(0), 0, 4)
        args = ([np.uint64(x) for x in ch] + [np.uint64(x) for x in tg] + [np.uint64(x) for x in nb]
                + [np.uint32(n), np.uint32(self.max_results), self._out_count, self._out_nonces])
        t0 = time.perf_counter()
        self.kernel(self.queue, (gsize,), (self.local_size,), *args)
        cnt = np.empty(1, np.uint32)
        cl.enqueue_copy(self.queue, cnt, self._out_count)
        self.queue.finish()
        dt = time.perf_counter() - t0

        count = int(cnt[0])
        found: list[int] = []
        if count:
            got = min(count, self.max_results)
            buf = np.empty(got * 4, np.uint64)
            cl.enqueue_copy(self.queue, buf, self._out_nonces)
            self.queue.finish()
            for i in range(got):
                limbs = buf[i * 4:i * 4 + 4]
                found.append(int(limbs[0]) | (int(limbs[1]) << 64) | (int(limbs[2]) << 128) | (int(limbs[3]) << 192))
            if count > self.max_results:
                log.warning("device %d: %d solutions in one batch, only %d kept (lower batch_size or raise max_results)",
                            self.info.index, count, self.max_results)
        self.total_hashes += n
        self.total_seconds += dt
        return found, n, dt

    def _adapt_batch(self, last_seconds: float) -> None:
        if last_seconds <= 0:
            return
        scaled = int(self.batch_size * (self.batch_target_ms / 1000.0) / last_seconds)
        self.batch_size = max(_MIN_BATCH, min(_MAX_BATCH, scaled or _MIN_BATCH))

    @property
    def hashrate(self) -> float:
        return self.total_hashes / self.total_seconds if self.total_seconds else 0.0

    def benchmark(self, seconds: float = 3.0) -> float:
        """Hash as fast as possible against an unreachable target; return MH/s."""
        challenge = bytes(32)
        target = 1  # nothing qualifies -> no result-buffer traffic
        # warm up + size to ~_TARGET_BATCH_MS
        _, _, dt = self.search_batch(challenge, target, n=1 << 22)
        self._adapt_batch(dt)
        hashes = 0
        elapsed = 0.0
        while elapsed < seconds:
            _, n, dt = self.search_batch(challenge, target)
            hashes += n
            elapsed += dt
            self._adapt_batch(dt)
        return hashes / elapsed / 1e6


class GpuFarm:
    """Runs `GpuWorker`s in parallel threads against a shared mining job.

    Job model: `set_job(challenge, target, epoch)` (re)points all workers. Workers
    only ever submit results tagged with the *current* epoch; the orchestrator drops
    any straggler whose epoch has rolled. `results` is a queue of `Found`.
    """

    def __init__(self, devices: list[DeviceInfo], *, kernel_src: str | None = None,
                 local_size: int | None = None, batch_target_ms: float | None = None):
        kernel_src = kernel_src or load_kernel_source()
        self.workers = [GpuWorker(d, kernel_src=kernel_src, local_size=local_size,
                                  batch_target_ms=batch_target_ms) for d in devices]
        self.results: queue.Queue[Found] = queue.Queue()
        self._lock = threading.Lock()
        self._job: tuple[bytes, int, int] | None = None  # (challenge, target, epoch)
        self._job_gen = 0  # bumped on every set_job so workers can detect a change mid-batch
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- job control --------------------------------------------------------
    def set_job(self, challenge: bytes, target: int, epoch: int) -> None:
        if len(challenge) != 32:
            raise ValueError("challenge must be 32 bytes")
        with self._lock:
            self._job = (bytes(challenge), int(target), int(epoch))
            self._job_gen += 1
        self._wake.set()

    def clear_job(self) -> None:
        with self._lock:
            self._job = None
            self._job_gen += 1
        self._wake.clear()

    def _current(self) -> tuple[tuple[bytes, int, int] | None, int]:
        with self._lock:
            return self._job, self._job_gen

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        for w in self.workers:
            t = threading.Thread(target=self._run_worker, args=(w,), name=f"gpu{w.info.index}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=5.0)

    def _run_worker(self, w: GpuWorker) -> None:
        log.info("worker started: %s", w.info.describe())
        while not self._stop.is_set():
            job, gen = self._current()
            if job is None:
                self._wake.wait(timeout=0.5)
                continue
            challenge, target, epoch = job
            try:
                found, _n, dt = w.search_batch(challenge, target)
            except Exception:  # pragma: no cover - device hiccup; back off and retry
                log.exception("worker %d batch failed", w.info.index)
                time.sleep(1.0)
                continue
            # only emit if the job didn't change underneath us
            if found and self._current()[1] == gen:
                for nonce in found:
                    self.results.put(Found(nonce=nonce, epoch=epoch, device_index=w.info.index))
            w._adapt_batch(dt)
        log.info("worker %d stopped", w.info.index)

    # -- stats --------------------------------------------------------------
    def stats(self) -> dict:
        per = {w.info.index: {"name": w.info.name, "mhps": w.hashrate / 1e6,
                              "hashes": w.total_hashes, "batch": w.batch_size} for w in self.workers}
        return {"total_mhps": sum(v["mhps"] for v in per.values()),
                "total_hashes": sum(v["hashes"] for v in per.values()), "devices": per}
