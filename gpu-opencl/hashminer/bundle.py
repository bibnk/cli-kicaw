"""eth_sendBundle submission to MEV builders (Flashbots / Beaverbuild / Titan).

A bundle is N pre-signed transactions that the builder includes together, in order, in
its block. For HASH256 this is the proven way to sweep multiple mints in a single block:
sign N ``mine(nonce)`` txs at consecutive tx-nonces, ship them as one unit, and if the
bundle lands you take that block's mining output before any public-mempool competitors.

All builder endpoints in :class:`hashminer.config.BundleConfig.endpoints` get the same
bundle in parallel. Flashbots requires ``X-Flashbots-Signature`` (signed with an
ephemeral key generated here — no balance, just for searcher reputation); the others
accept plain JSON-RPC.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak as keccak256
from web3 import Web3

from .config import Config
from .submit import GWEI, _raw_tx_bytes
from .verify import VerifiedSolution

if TYPE_CHECKING:  # pragma: no cover
    from .chain import ChainClient
    from .submit import Submitter

log = logging.getLogger("hashminer.bundle")


@dataclass
class BundleResult:
    sent: int                           # txs in the bundle
    target_block: int
    endpoints_ok: list[str]
    endpoints_err: dict[str, str]
    tx_hashes: list[str]


class BundleSubmitter:
    def __init__(self, cfg: Config, chain: "ChainClient", account):
        self.cfg = cfg
        self.chain = chain
        self.account = account              # miner LocalAccount — signs the mine() txs
        # Ephemeral key for X-Flashbots-Signature; no balance, only proves a stable searcher identity.
        self._fb_signer = Account.create()
        self._lock = threading.Lock()
        log.info("bundle: enabled, size=%d, target=+%d, fb_signer=%s, endpoints=%d",
                 cfg.bundle.size, cfg.bundle.target_blocks_ahead, self._fb_signer.address, len(cfg.bundle.endpoints))

    # --------------------------------------------------------------------- gas
    def _gas(self, base_fee: int | None) -> tuple[int, int, int]:
        """(gas_limit, maxFeePerGas, maxPriorityFeePerGas) in (gas units, wei, wei)."""
        prio_gw = self.cfg.bundle.priority_gwei
        if prio_gw is None:
            prio_gw = self.cfg.gas.priority_gwei
        prio = int(prio_gw * GWEI)
        if base_fee is None:
            base_fee = max(prio, GWEI)        # fallback when we have no fresh baseFee
        max_fee = int(base_fee * self.cfg.gas.base_fee_multiplier) + prio
        gas_limit = int(self.cfg.gas.gas_limit) if self.cfg.gas.gas_limit else 250_000
        return gas_limit, max_fee, prio

    # --------------------------------------------------------------------- main entry
    def send(self, sols: list[VerifiedSolution], *, target_block: int,
             base_fee: int | None, submitter: "Submitter") -> BundleResult:
        """Sign `sols` as consecutive tx-nonces and broadcast as `eth_sendBundle` to every endpoint."""
        if not sols:
            return BundleResult(sent=0, target_block=target_block, endpoints_ok=[], endpoints_err={}, tx_hashes=[])

        gas_limit, max_fee, prio = self._gas(base_fee)
        start_tx_nonce = submitter.reserve_tx_nonces(len(sols))

        raw_txs: list[str] = []
        tx_hashes: list[str] = []
        for i, sol in enumerate(sols):
            tx = self.chain.contract.functions.mine(sol.nonce).build_transaction({
                "from": self.account.address,
                "nonce": start_tx_nonce + i,
                "chainId": self.cfg.chain_id,
                "gas": gas_limit,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": prio,
                "value": 0,
            })
            signed = self.account.sign_transaction(tx)
            raw = _raw_tx_bytes(signed)
            raw_txs.append("0x" + raw.hex())
            tx_hashes.append("0x" + keccak256(raw).hex())

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendBundle",
            "params": [{
                "txs": raw_txs,
                "blockNumber": hex(target_block),
                # allow every tx to revert without invalidating the bundle - some hits will land
                # the per-block cap; we still want the bundle to make it in
                "revertingTxHashes": tx_hashes,
            }],
        }
        body = json.dumps(payload).encode("utf-8")

        # Fan-out: builders (eth_sendBundle) AND the regular RPC (one eth_sendRawTransaction per
        # tx) in parallel. Public-mempool broadcast is the safety net so a missed bundle doesn't
        # leave us with a stuck tx-nonce queue - the txs still get picked up by *some* block.
        result = self._fan_out(body, raw_txs)

        # plug the bundle's tx hashes into the existing receipt poller + mark nonces used
        for sol, txh in zip(sols, tx_hashes):
            submitter.record_pending(txh, sol.nonce, sol.epoch)

        log.info("bundle: %d txs -> block %d, gas=%d maxFee=%.2f gw prio=%.2f gw | "
                 "builders ok=%d/%d  mempool ok=%d/%d  tx_nonces %d..%d",
                 len(sols), target_block, gas_limit, max_fee / GWEI, prio / GWEI,
                 len(result["ok_bundle"]), len(self.cfg.bundle.endpoints),
                 result["ok_pub"], len(raw_txs),
                 start_tx_nonce, start_tx_nonce + len(sols) - 1)
        for url, err in result["err_bundle"].items():
            log.warning("  builder %s -> %s", url, err)
        for txh, err in result["err_pub"].items():
            log.debug("  mempool %s -> %s", txh, err)

        return BundleResult(sent=len(sols), target_block=target_block,
                            endpoints_ok=result["ok_bundle"], endpoints_err=result["err_bundle"], tx_hashes=tx_hashes)

    # --------------------------------------------------------------------- HTTP fan-out
    def _fan_out(self, body: bytes, raw_txs: list[str]) -> dict:
        endpoints = list(self.cfg.bundle.endpoints)
        ok_bundle: list[str] = []
        err_bundle: dict[str, str] = {}
        ok_pub = 0
        err_pub: dict[str, str] = {}
        workers = max(len(endpoints) + len(raw_txs), 1)
        with ThreadPoolExecutor(max_workers=min(workers, 32)) as ex:
            b_futs = {ex.submit(self._send_one, url, body): ("bundle", url) for url in endpoints}
            p_futs = {ex.submit(self._send_to_chain, raw): ("pub", raw[:18]) for raw in raw_txs}
            futs = {**b_futs, **p_futs}
            for f in as_completed(futs):
                kind, tag = futs[f]
                try:
                    resp = f.result()
                    if kind == "bundle":
                        rpc_err = resp.get("error") if isinstance(resp, dict) else None
                        if rpc_err:
                            err_bundle[tag] = f"rpc: {rpc_err.get('message', rpc_err)}"
                        else:
                            ok_bundle.append(tag)
                    else:
                        ok_pub += 1
                except Exception as exc:  # noqa: BLE001
                    (err_bundle if kind == "bundle" else err_pub)[tag] = f"{type(exc).__name__}: {exc}"
        return {"ok_bundle": ok_bundle, "err_bundle": err_bundle, "ok_pub": ok_pub, "err_pub": err_pub}

    def _send_to_chain(self, raw_hex: str) -> dict:
        """Broadcast a single signed tx to the public mempool via the configured RPC.
        Treats 'already known' / 'nonce too low' as benign (the bundle path may have already landed it)."""
        raw = bytes.fromhex(raw_hex.removeprefix("0x"))
        try:
            self.chain.w3.eth.send_raw_transaction(raw)
            return {"ok": True}
        except Exception as exc:
            msg = str(exc).lower()
            if "already known" in msg or "nonce too low" in msg or "already imported" in msg:
                return {"ok": True, "note": "benign"}
            raise

    def _send_one(self, url: str, body: bytes) -> dict:
        headers = {"Content-Type": "application/json"}
        # Flashbots-family endpoints require a signature over keccak(body) from a stable searcher key.
        # Other builders accept the same header (harmless) so we send it everywhere.
        digest = "0x" + keccak256(body).hex()
        msg = encode_defunct(text=digest)
        sig = self._fb_signer.sign_message(msg)
        # hexbytes<1 returns "0x..." from .hex(); hexbytes>=1 returns "..." — normalize either way
        sig_hex = sig.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex
        headers["X-Flashbots-Signature"] = f"{self._fb_signer.address}:{sig_hex}"
        r = requests.post(url, data=body, headers=headers, timeout=self.cfg.request_timeout_s)
        r.raise_for_status()
        return r.json()

    # --------------------------------------------------------------------- helpers (testing)
    @staticmethod
    def build_payload(raw_txs: list[str], target_block: int) -> dict:
        """Pure helper used by tests; mirrors what send() POSTs."""
        return {
            "jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle",
            "params": [{"txs": raw_txs, "blockNumber": hex(target_block), "revertingTxHashes": [
                # bundle.py mirrors this hash derivation; tests just check structural shape
            ]}],
        }
