"""The orchestrator: ties the GPU farm, the chain client, and the submitter together.

Flow
----
1. Connect; resolve miner address.
2. If the contract isn't open yet (`genesisComplete == false`), wait for it (poll).
3. Read `miningState()`, derive the epoch's challenge, point the GPU farm at
   `(challenge, target, epoch)`, start hashing.
4. On every new block: refresh `miningState()`; if the epoch rolled (new challenge)
   or `currentDifficulty` retargeted, re-point the farm; sweep tx receipts; log stats.
5. A drain thread consumes `Found` nonces from the farm, re-verifies each on CPU
   against the *current* challenge/difficulty, drops stragglers from a past epoch,
   and hands survivors to the submitter.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from eth_utils import to_checksum_address

from .bundle import BundleSubmitter
from .chain import ChainClient, MiningState
from .config import Config
from .constants import epoch_for_block
from .gpu import GpuFarm, select_devices
from .submit import Submitter
from .verify import VerifiedSolution, verify

log = logging.getLogger("hashminer.miner")


@dataclass
class _Job:
    challenge: bytes
    difficulty: int
    epoch: int


# a stand-in identity for --dry-run when no key / miner_address is configured (challenge is address-bound,
# so we still derive a real-looking challenge — just nothing can ever be submitted from it)
_DRY_RUN_PLACEHOLDER = "0x000000000000000000000000000000000000dEaD"


class Miner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.chain = ChainClient(cfg)
        self.address = cfg.effective_miner_address()
        if self.address is None:
            if cfg.dry_run:
                self.address = _DRY_RUN_PLACEHOLDER
                log.warning("no key / miner_address configured - using placeholder %s for the dry run", self.address)
            else:
                raise SystemExit("no miner address: set HASH256_PRIVATE_KEY / wallet.key_file, or wallet.miner_address for read-only")
        self.address = to_checksum_address(self.address)
        self.submitter = Submitter(cfg, self.chain, address=self.address)
        self.bundle_submitter: BundleSubmitter | None = None
        if cfg.bundle.enabled:
            acct = cfg.resolved_account()
            if acct is None:
                if not cfg.dry_run:
                    raise SystemExit("bundle.enabled requires a private key (HASH256_PRIVATE_KEY) to sign bundle txs")
                log.warning("bundle.enabled with no key + dry_run: bundle path disabled (would have nothing to sign)")
            else:
                self.bundle_submitter = BundleSubmitter(cfg, self.chain, acct)
        self.farm = GpuFarm(select_devices(cfg.gpu_devices), local_size=cfg.local_size)
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._stop = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._submit_pool = ThreadPoolExecutor(max_workers=max(1, cfg.submit_concurrency), thread_name_prefix="submit")
        self._bundle_buffer: list[VerifiedSolution] = []   # used only when cfg.bundle.enabled
        self._bundle_lock = threading.Lock()
        self._last_stats = 0.0
        self._found_total = 0
        self._dropped_stale = 0

    # ------------------------------------------------------------------ job
    def _current_job(self) -> _Job | None:
        with self._lock:
            return self._job

    def _set_job_from_state(self, st: MiningState) -> bool:
        """Returns True if the GPU job changed (epoch rolled or difficulty retargeted)."""
        challenge = self.chain.challenge_for(self.address, st.epoch)
        with self._lock:
            old = self._job
            changed = (old is None or old.epoch != st.epoch or old.difficulty != st.difficulty
                       or old.challenge != challenge)
            if changed:
                self._job = _Job(challenge=challenge, difficulty=st.difficulty, epoch=st.epoch)
        if changed:
            self.farm.set_job(challenge, st.difficulty, st.epoch)
            log.info("job: epoch=%d  difficulty=%#x  challenge=%s  era=%d reward=%d HASH  remaining=%.0f HASH",
                     st.epoch, st.difficulty, challenge.hex(), st.era, st.reward // 10**18, st.remaining / 1e18)
        return changed

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        log.info("hash256-miner: contract=%s  chainId=%d  miner=%s%s",
                 self.chain.contract_address, self.cfg.chain_id, self.address,
                 "  [DRY-RUN]" if self.submitter.dry_run else "")
        try:
            cid = self.chain.chain_id()
            if cid != self.cfg.chain_id:
                log.warning("RPC chainId %d != configured chain_id %d", cid, self.cfg.chain_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read chainId: %s", exc)

        self._wait_for_open()

        st = self.chain.mining_state()
        if st.remaining <= 0:
            log.info("mining supply already exhausted - nothing to do")
            return
        self._set_job_from_state(st)
        self.farm.start()
        self._drain_thread = threading.Thread(target=self._drain_loop, name="drain", daemon=True)
        self._drain_thread.start()
        log.info("mining. devices: %s", ", ".join(d.name for d in (w.info for w in self.farm.workers)))

        try:
            self._block_loop()
        except KeyboardInterrupt:
            log.info("interrupted - shutting down")
        finally:
            self.shutdown()

    def _wait_for_open(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                if self.chain.genesis_complete():
                    if not first:
                        log.info("genesis complete - mining is OPEN")
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("genesisComplete() read failed: %s", exc)
                time.sleep(self.cfg.poll_interval_s)
                continue
            if first:
                try:
                    minted, remaining, eth_raised, _ = self.chain.contract.functions.genesisState().call()
                    log.info("mining NOT open yet (genesis incomplete): genesisMinted=%.0f HASH remaining=%.0f HASH ethRaised=%.4f ETH",
                             minted / 1e18, remaining / 1e18, eth_raised / 1e18)
                except Exception:
                    log.info("mining NOT open yet (genesisComplete == false). Waiting...")
                log.info("Will poll every %.0fs. (Tip: run with --dry-run to test the full pipeline against the live RPC now.)",
                         max(self.cfg.poll_interval_s, 15.0))
                first = False
            time.sleep(max(self.cfg.poll_interval_s, 15.0))

    def _block_loop(self) -> None:
        for bn in self.chain.follow_blocks(stop=self._stop.is_set):
            if self._stop.is_set():
                break
            # epoch could roll mid-poll - recompute from the block we actually saw
            try:
                st = self.chain.mining_state()
            except Exception as exc:  # noqa: BLE001
                log.warning("miningState() failed at block %d: %s", bn, exc)
                continue
            if st.remaining <= 0:
                log.info("mining supply exhausted at block %d - stopping", bn)
                break
            self._set_job_from_state(st)
            if self.bundle_submitter is not None:
                self._maybe_flush_bundle(st)
            self.submitter.poll_receipts()
            self._maybe_log_stats(st)

    def _drain_loop(self) -> None:
        q = self.farm.results
        while not self._stop.is_set():
            try:
                found = q.get(timeout=0.5)
            except Exception:
                continue
            self._found_total += 1
            job = self._current_job()
            if job is None or found.epoch != job.epoch:
                self._dropped_stale += 1
                continue
            sol = verify(job.challenge, found.nonce, job.difficulty, job.epoch)
            if sol is None:
                # Difficulty may have retargeted harder since the GPU found it; or (never, but be safe) a false hit.
                self._dropped_stale += 1
                continue
            if self.bundle_submitter is not None:
                with self._bundle_lock:
                    self._bundle_buffer.append(sol)
            else:
                # fire-and-forget into the pool so multiple submits can overlap their RPC calls
                self._submit_pool.submit(self._safe_submit, sol)

    def _safe_submit(self, sol: VerifiedSolution) -> None:
        try:
            self.submitter.submit(sol)
        except Exception as exc:  # noqa: BLE001
            log.exception("submit raised for nonce %d: %s", sol.nonce, exc)

    def _maybe_flush_bundle(self, st: MiningState) -> None:
        if self.bundle_submitter is None:
            return
        with self._bundle_lock:
            # drop stale-epoch and already-submitted entries before bundling
            self._bundle_buffer = [s for s in self._bundle_buffer
                                   if s.epoch == st.epoch and not self.submitter.already_used(s.epoch, s.nonce)]
            if not self._bundle_buffer:
                return
            n = min(self.cfg.bundle.size, len(self._bundle_buffer))
            sols = self._bundle_buffer[:n]
            del self._bundle_buffer[:n]
        target = st.block_number + max(1, self.cfg.bundle.target_blocks_ahead)
        try:
            self.bundle_submitter.send(sols, target_block=target, base_fee=st.base_fee, submitter=self.submitter)
        except Exception as exc:  # noqa: BLE001
            log.exception("bundle send failed: %s", exc)

    # ------------------------------------------------------------------ stats / teardown
    def _maybe_log_stats(self, st: MiningState, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_stats < 30:
            return
        self._last_stats = now
        g = self.farm.stats()
        s = self.submitter.stats()
        log.info("STATS  %.0f MH/s total  | block=%d epoch=%d (%d blk left) diff=%#x | "
                 "found=%d (stale-drop=%d) sent=%d confirmed=%d reverted=%d skipped=%d errors=%d | earned=%.1f HASH",
                 g["total_mhps"], st.block_number, st.epoch, st.epoch_blocks_left, st.difficulty,
                 self._found_total, self._dropped_stale, s["sent"], s["confirmed"], s["reverted"],
                 s["skipped"], s["errors"], s["hash_earned"])
        for idx, d in g["devices"].items():
            log.debug("  device[%d] %s: %.0f MH/s  batch=2^%d", idx, d["name"], d["mhps"], int(d["batch"]).bit_length() - 1)

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.farm.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._drain_thread:
            self._drain_thread.join(timeout=3.0)
        try:
            self._submit_pool.shutdown(wait=True, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.submitter.poll_receipts()
        except Exception:  # noqa: BLE001
            pass
        try:
            st = self.chain.mining_state()
            self._maybe_log_stats(st, force=True)
        except Exception:  # noqa: BLE001
            g, s = self.farm.stats(), self.submitter.stats()
            log.info("FINAL  %.0f MH/s | found=%d sent=%d confirmed=%d reverted=%d earned=%.1f HASH",
                     g["total_mhps"], self._found_total, s["sent"], s["confirmed"], s["reverted"], s["hash_earned"])
        log.info("bye.")
