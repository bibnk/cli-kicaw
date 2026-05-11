"""Ethereum side: a thin web3.py wrapper for reading the Hash contract, following
new blocks, and (via ``submit.py``) sending ``mine`` transactions.

Resilience model: a list of RPC URLs (the configured one + fallbacks). Any request
that raises rotates to the next URL and retries; we only give up after a full pass
fails. Public RPCs are rate-limited, so transient failures are expected and normal.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from eth_utils import to_checksum_address
from web3 import Web3

from .abi import HASH_ABI
from .config import Config
from .constants import EPOCH_BLOCKS, compute_challenge, epoch_for_block

log = logging.getLogger("hashminer.chain")


@dataclass(frozen=True)
class MiningState:
    era: int
    reward: int
    difficulty: int        # == currentDifficulty (the target ceiling: digest must be < this)
    minted: int
    remaining: int
    epoch: int
    epoch_blocks_left: int
    block_number: int
    base_fee: int | None
    genesis_complete: bool


class ChainClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._urls = cfg.rpc_chain()
        self._i = 0
        self.contract_address = to_checksum_address(cfg.contract)
        self._w3 = self._connect(self._urls[self._i])
        self.contract = self._w3.eth.contract(address=self.contract_address, abi=HASH_ABI)
        self._challenge_local_ok: bool | None = None  # set on first cross-check
        # baseFee cache: populated by mining_state() each block; submit._fees() reads it instead of
        # calling eth_getBlock again. Stale reads across one block are fine — gas pricing tolerates it.
        self._cache_lock = threading.Lock()
        self._cached_base_fee: tuple[int, int] | None = None   # (block_number, base_fee_wei)
        if cfg.ws_url:
            log.info("ws_url is set but this version polls eth_blockNumber (interval %.1fs); ws is reserved for later",
                     cfg.poll_interval_s)

    # ---------------------------------------------------------------- plumbing
    def _connect(self, url: str) -> Web3:
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self.cfg.request_timeout_s}))

    def _rotate(self) -> None:
        self._i = (self._i + 1) % len(self._urls)
        url = self._urls[self._i]
        log.warning("rotating RPC -> %s", url)
        self._w3 = self._connect(url)
        self.contract = self._w3.eth.contract(address=self.contract_address, abi=HASH_ABI)

    def _do(self, fn: Callable, *, retries_per_url: int = 1, what: str = "rpc call"):
        attempts = len(self._urls) * max(1, retries_per_url)
        last_exc: Exception | None = None
        for n in range(attempts):
            try:
                return fn(self._w3, self.contract)
            except Exception as exc:  # noqa: BLE001 - RPCs fail in many shapes (timeouts, 429, JSON-RPC errors)
                last_exc = exc
                log.debug("%s failed (%s): %s", what, type(exc).__name__, exc)
                if n + 1 < attempts:
                    self._rotate()
                    time.sleep(min(2.0, 0.25 * (n + 1)))
        raise RuntimeError(f"{what} failed on all RPC endpoints ({self._urls}): {last_exc}") from last_exc

    @property
    def w3(self) -> Web3:
        return self._w3

    # ---------------------------------------------------------------- reads
    def chain_id(self) -> int:
        return self._do(lambda w3, c: w3.eth.chain_id, what="eth_chainId")

    def block_number(self) -> int:
        return self._do(lambda w3, c: w3.eth.block_number, what="eth_blockNumber")

    def base_fee(self) -> int | None:
        """Latest block's baseFee. Returns the cached value if mining_state() has populated it
        for the current (or any recent) block — saves an RPC per submission on the hot path."""
        with self._cache_lock:
            cached = self._cached_base_fee
        if cached is not None:
            return cached[1]
        def f(w3, c):
            blk = w3.eth.get_block("latest")
            return blk.get("baseFeePerGas")
        return self._do(f, what="eth_getBlock(latest)")

    def cached_base_fee(self) -> int | None:
        """Like base_fee() but never fetches — returns None if the cache is empty."""
        with self._cache_lock:
            return self._cached_base_fee[1] if self._cached_base_fee else None

    def genesis_complete(self) -> bool:
        return bool(self._do(lambda w3, c: c.functions.genesisComplete().call(), what="genesisComplete()"))

    def current_difficulty(self) -> int:
        return int(self._do(lambda w3, c: c.functions.currentDifficulty().call(), what="currentDifficulty()"))

    def total_mints(self) -> int:
        return int(self._do(lambda w3, c: c.functions.totalMints().call(), what="totalMints()"))

    def mints_in_block(self, block: int) -> int:
        return int(self._do(lambda w3, c: c.functions.mintsInBlock(block).call(), what="mintsInBlock()"))

    def get_challenge_onchain(self, miner: str) -> bytes:
        m = to_checksum_address(miner)
        return bytes(self._do(lambda w3, c: c.functions.getChallenge(m).call(), what="getChallenge()"))

    def mining_state(self) -> MiningState:
        def f(w3, c):
            era, reward, diff, minted, remaining, epoch, ebl = c.functions.miningState().call()
            blk = w3.eth.get_block("latest")
            gc = c.functions.genesisComplete().call()
            return MiningState(
                era=int(era), reward=int(reward), difficulty=int(diff), minted=int(minted),
                remaining=int(remaining), epoch=int(epoch), epoch_blocks_left=int(ebl),
                block_number=int(blk["number"]), base_fee=blk.get("baseFeePerGas"), genesis_complete=bool(gc),
            )
        st = self._do(f, what="miningState()")
        # populate the baseFee cache so submit._fees() doesn't redo eth_getBlock per submission
        if st.base_fee is not None:
            with self._cache_lock:
                self._cached_base_fee = (st.block_number, st.base_fee)
        return st

    # ---------------------------------------------------------------- challenge
    def challenge_for(self, miner: str, epoch: int, *, prefer_local: bool = True) -> bytes:
        """The 32-byte challenge for (miner, epoch). Computed locally (cheap, and lets us
        precompute the *next* epoch) once we've confirmed locally == on-chain at startup."""
        m = to_checksum_address(miner)
        local = compute_challenge(self.cfg.chain_id, self.contract_address, m, epoch)
        if not prefer_local or self._challenge_local_ok is False:
            return self.get_challenge_onchain(m)
        if self._challenge_local_ok is None:
            # cross-check against the contract for the *current* epoch only
            cur_epoch = epoch_for_block(self.block_number())
            onchain = self.get_challenge_onchain(m)
            ref = compute_challenge(self.cfg.chain_id, self.contract_address, m, cur_epoch)
            self._challenge_local_ok = (onchain == ref)
            if not self._challenge_local_ok:
                log.warning("local challenge != getChallenge() - falling back to on-chain getChallenge each epoch "
                            "(local=%s onchain=%s epoch=%d)", ref.hex(), onchain.hex(), cur_epoch)
                return onchain if epoch == cur_epoch else self.get_challenge_onchain(m)
            log.info("local challenge derivation verified against getChallenge()")
        return local

    # ---------------------------------------------------------------- block follower
    def follow_blocks(self, *, stop: Callable[[], bool] | None = None) -> Iterator[int]:
        """Yield the latest block number, then yield again each time it advances. Polls
        eth_blockNumber every cfg.poll_interval_s. Robust to RPC hiccups (rotates, keeps going)."""
        last = -1
        while not (stop and stop()):
            try:
                bn = self.block_number()
            except Exception as exc:  # noqa: BLE001
                log.warning("block poll failed, retrying: %s", exc)
                time.sleep(self.cfg.poll_interval_s)
                continue
            if bn != last:
                last = bn
                yield bn
            time.sleep(self.cfg.poll_interval_s)
