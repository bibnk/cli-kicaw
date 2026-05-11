"""Building, signing and sending ``mine(nonce)`` transactions.

Drained by a ThreadPoolExecutor in miner.py so multiple submissions can overlap
their network I/O (estimate_gas / send_raw_transaction). The submitter is
thread-safe: a short lock guards the tx-nonce counter and the used-set / pending
list; the slow RPC work (fees, estimate_gas, sign, send) runs outside the lock.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from eth_utils import keccak, to_checksum_address

from .chain import ChainClient
from .config import Config
from .verify import VerifiedSolution

log = logging.getLogger("hashminer.submit")

GWEI = 10**9

# 4-byte selectors of the Hash contract's custom errors -> name, so estimate_gas/revert
# reasons read as e.g. "BlockCapReached" instead of an opaque 0x.... blob.
_ERROR_NAMES = ["NotPoolManager", "InvalidAction", "GenesisAlreadyComplete", "GenesisNotComplete",
                "GenesisSoldOut", "TxCapExceeded", "InsufficientPayment", "GenesisNotSoldOut",
                "InsufficientWork", "ProofAlreadyUsed", "BlockCapReached", "SupplyExhausted",
                "EthTransferFailed", "NotController", "NothingToClaim", "UnsupportedSwapMode",
                "InsufficientLiquidity", "WrongPairConfig", "TooSoon", "NothingToSeed"]
ERROR_SELECTORS = {"0x" + keccak(text=n + "()")[:4].hex(): n for n in _ERROR_NAMES}
_SELECTOR_RE = re.compile(r"0x[0-9a-fA-F]{8}")


def decode_revert(exc: object) -> str:
    """Best-effort: turn a web3 ContractLogicError / estimate_gas failure into a readable name."""
    s = str(exc)
    for m in _SELECTOR_RE.findall(s):
        name = ERROR_SELECTORS.get(m.lower())
        if name:
            return name
    return s if len(s) < 120 else s[:117] + "..."


def _raw_tx_bytes(signed) -> bytes:
    # eth-account >=0.13 -> .raw_transaction ; older -> .rawTransaction
    return bytes(getattr(signed, "raw_transaction", None) or signed.rawTransaction)


@dataclass
class SubmitResult:
    ok: bool
    reason: str                       # "sent" | "dry-run" | "skipped:..." | "error:..."
    nonce: int
    epoch: int
    tx_hash: Optional[str] = None
    gas: Optional[int] = None
    max_fee_gwei: Optional[float] = None


@dataclass
class _Pending:
    tx_hash: str
    nonce: int
    epoch: int
    sent_at: float = field(default_factory=time.time)


class Submitter:
    def __init__(self, cfg: Config, chain: ChainClient, *, address: str | None = None):
        self.cfg = cfg
        self.chain = chain
        self.account = cfg.resolved_account()
        # the address rewards/gas are tied to: the key's address, else the one the caller
        # resolved (Miner passes its dry-run placeholder when there's no key/miner_address).
        addr = (self.account.address if self.account else None) or address or cfg.effective_miner_address()
        self.address = to_checksum_address(addr) if addr else None
        self.dry_run = cfg.dry_run or self.account is None
        self._lock = threading.Lock()
        self._tx_nonce: Optional[int] = None             # next account tx nonce to use
        self._used: dict[int, set[int]] = {}             # epoch -> set of submitted PoW nonces
        self._pending: list[_Pending] = []
        # stats
        self.sent = 0
        self.confirmed = 0
        self.reverted = 0
        self.skipped = 0
        self.errors = 0
        self.hash_earned = 0                              # wei of $HASH credited (from Mined events)

    # ------------------------------------------------------------------ gas
    def _fees(self) -> tuple[int, int] | None:
        """(maxFeePerGas, maxPriorityFeePerGas) in wei, or None to skip on price."""
        base = self.chain.base_fee()
        prio = int(self.cfg.gas.priority_gwei * GWEI)
        if base is None:  # pre-London / weird node - fall back to legacy-ish numbers
            base = int(self.cfg.gas.priority_gwei * GWEI)
        cap_gw = self.cfg.profitability.skip_if_basefee_above_gwei
        if cap_gw is not None and base > cap_gw * GWEI:
            log.info("skip: baseFee %.2f gwei > skip_if_basefee_above_gwei %.2f", base / GWEI, cap_gw)
            return None
        max_fee = int(base * self.cfg.gas.base_fee_multiplier) + prio
        if self.cfg.gas.max_fee_gwei is not None and max_fee > self.cfg.gas.max_fee_gwei * GWEI:
            log.info("skip: needed maxFee %.2f gwei > gas.max_fee_gwei cap %.2f", max_fee / GWEI, self.cfg.gas.max_fee_gwei)
            return None
        return max_fee, prio

    def _gas_limit(self, nonce: int) -> int | None:
        if self.cfg.gas.gas_limit:
            return int(self.cfg.gas.gas_limit)
        try:
            est = self.chain.contract.functions.mine(nonce).estimate_gas({"from": self.address})
        except Exception as exc:  # noqa: BLE001 - a revert here means it'd revert on-chain too
            log.info("skip nonce %d: estimate_gas reverted -> %s", nonce, decode_revert(exc))
            return None
        return int(est * self.cfg.gas.gas_limit_multiplier)

    # ------------------------------------------------------------- tx nonce
    def _sync_tx_nonce(self) -> int:
        n = self.chain.w3.eth.get_transaction_count(self.address, "pending")
        self._tx_nonce = int(n)
        return self._tx_nonce

    def _next_tx_nonce(self) -> int:
        if self._tx_nonce is None:
            self._sync_tx_nonce()
        n = self._tx_nonce
        self._tx_nonce = n + 1
        return n

    # ------------------------------------------------------------- bookkeeping
    def already_used(self, epoch: int, nonce: int) -> bool:
        return nonce in self._used.get(epoch, set())

    def epoch_count(self, epoch: int) -> int:
        return len(self._used.get(epoch, set()))

    def _mark_used(self, epoch: int, nonce: int) -> None:
        self._used.setdefault(epoch, set()).add(nonce)
        # prune epochs older than the previous one
        for e in [e for e in self._used if e < epoch - 1]:
            self._used.pop(e, None)

    # ------------------------------------------------------------- submit
    def submit(self, sol: VerifiedSolution) -> SubmitResult:
        # 1) Quick eligibility under lock (cheap state checks only)
        with self._lock:
            if self.address is None:
                self.skipped += 1
                log.warning("no miner address - cannot submit (set a key or miner_address)")
                return SubmitResult(False, "skipped:no-address", sol.nonce, sol.epoch)
            if self.already_used(sol.epoch, sol.nonce):
                return SubmitResult(False, "skipped:duplicate", sol.nonce, sol.epoch)
            if self.epoch_count(sol.epoch) >= self.cfg.profitability.max_pending_per_epoch:
                return SubmitResult(False, "skipped:epoch-cap", sol.nonce, sol.epoch)

        # 2) Slow RPC work — OUTSIDE the lock so peers can overlap.
        #    _fees() reads chain.cached_base_fee() (set by miningState polls) → ~0 RPC most of the time.
        #    _gas_limit() does estimate_gas unless cfg.gas.gas_limit is set.
        fees = self._fees()
        if fees is None:
            with self._lock:
                self.skipped += 1
            return SubmitResult(False, "skipped:gas", sol.nonce, sol.epoch)
        max_fee, prio = fees

        gas = self._gas_limit(sol.nonce)
        if gas is None:
            with self._lock:
                self.skipped += 1
            return SubmitResult(False, "skipped:estimate-revert", sol.nonce, sol.epoch)

        # 3) Reserve tx-nonce + mark used (tiny critical section)
        with self._lock:
            if self.already_used(sol.epoch, sol.nonce):  # re-check after the slow work
                return SubmitResult(False, "skipped:duplicate", sol.nonce, sol.epoch)
            if self.dry_run:
                tx_nonce = self._tx_nonce if self._tx_nonce is not None else 0
            else:
                tx_nonce = self._next_tx_nonce()
            self._mark_used(sol.epoch, sol.nonce)

        # 4) Sign + send — outside the lock (parallelizable, no shared state touched)
        tx = self._build(sol.nonce, tx_nonce, gas, max_fee, prio)

        if self.dry_run:
            if self.account is not None:
                signed = self.account.sign_transaction(tx)
                log.info("[dry-run] would send mine(%d): gas=%d maxFee=%.2f gwei raw=%s...",
                         sol.nonce, gas, max_fee / GWEI, _raw_tx_bytes(signed)[:8].hex())
            else:
                log.info("[dry-run] would send mine(%d): gas=%d maxFee=%.2f gwei (no key - read-only)",
                         sol.nonce, gas, max_fee / GWEI)
            return SubmitResult(True, "dry-run", sol.nonce, sol.epoch, gas=gas, max_fee_gwei=max_fee / GWEI)

        try:
            signed = self.account.sign_transaction(tx)
            tx_hash = self.chain.w3.eth.send_raw_transaction(_raw_tx_bytes(signed))
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            with self._lock:
                if "nonce too low" in msg or "already known" in msg or "replacement transaction underpriced" in msg:
                    log.warning("send hiccup (%s) - resyncing tx nonce", decode_revert(exc))
                    self._sync_tx_nonce()
                else:
                    self.errors += 1
                    log.error("send failed for mine(%d): %s", sol.nonce, decode_revert(exc))
            return SubmitResult(False, f"error:{decode_revert(exc)}", sol.nonce, sol.epoch)

        txh = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        with self._lock:
            self._pending.append(_Pending(tx_hash=txh, nonce=sol.nonce, epoch=sol.epoch))
            self.sent += 1
        log.info("sent mine(%d)  tx=%s  gas=%d  maxFee=%.2f gwei  (%d leading zero bits)",
                 sol.nonce, txh, gas, max_fee / GWEI, sol.leading_zero_bits)
        return SubmitResult(True, "sent", sol.nonce, sol.epoch, tx_hash=txh, gas=gas, max_fee_gwei=max_fee / GWEI)

    def record_pending(self, tx_hash: str, pow_nonce: int, epoch: int) -> None:
        """Called by the bundle path to plug bundled-tx hashes into the same receipt poller."""
        with self._lock:
            self._pending.append(_Pending(tx_hash=tx_hash, nonce=pow_nonce, epoch=epoch))
            self._mark_used(epoch, pow_nonce)
            self.sent += 1

    def reserve_tx_nonces(self, n: int) -> int:
        """Reserve n consecutive tx-nonces for a bundle; returns the first one."""
        with self._lock:
            if self._tx_nonce is None:
                self._sync_tx_nonce()
            start = self._tx_nonce
            self._tx_nonce = start + n
            return start

    def _build(self, pow_nonce: int, tx_nonce: int, gas: int, max_fee: int, prio: int) -> dict:
        return self.chain.contract.functions.mine(pow_nonce).build_transaction({
            "from": self.address,
            "nonce": tx_nonce,
            "chainId": self.cfg.chain_id,
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "value": 0,
        })

    # ------------------------------------------------------------- receipts
    def poll_receipts(self) -> None:
        """Non-blocking sweep of outstanding tx hashes; update confirmed/reverted/earned stats."""
        if not self._pending:
            return
        still: list[_Pending] = []
        for p in self._pending:
            try:
                rcpt = self.chain.w3.eth.get_transaction_receipt(p.tx_hash)
            except Exception:  # not mined yet (TransactionNotFound) or RPC blip
                if time.time() - p.sent_at < 600:
                    still.append(p)
                continue
            if rcpt is None:
                still.append(p)
                continue
            if rcpt.get("status", 0) == 1:
                self.confirmed += 1
                reward = self._reward_from_receipt(rcpt)
                if reward:
                    self.hash_earned += reward
                log.info("CONFIRMED mine(%d) tx=%s%s", p.nonce, p.tx_hash,
                         f"  +{reward / 1e18:.2f} HASH" if reward else "")
            else:
                self.reverted += 1
                log.warning("REVERTED mine(%d) tx=%s (block cap / target moved / not open) - fine, continuing", p.nonce, p.tx_hash)
        self._pending = still

    def _reward_from_receipt(self, rcpt) -> int:
        try:
            evs = self.chain.contract.events.Mined().process_receipt(rcpt)
            return int(sum(int(e["args"]["reward"]) for e in evs))
        except Exception:
            return 0

    # ------------------------------------------------------------- stats
    def stats(self) -> dict:
        return {"sent": self.sent, "confirmed": self.confirmed, "reverted": self.reverted,
                "skipped": self.skipped, "errors": self.errors, "pending": len(self._pending),
                "hash_earned": self.hash_earned / 1e18}
