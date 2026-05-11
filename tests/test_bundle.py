"""Unit tests for the eth_sendBundle path.

These don't hit any network — `requests.post` and the chain's `eth_sendRawTransaction`
are mocked. We just verify the bundle payload is well-formed (correct method, hex block
number, all raw txs present, every tx in `revertingTxHashes`), and that the Flashbots
auth header is a valid `<address>:<sig>` pair signed by our ephemeral key.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from hashminer.bundle import BundleSubmitter
from hashminer.config import BundleConfig, Config
from hashminer.verify import VerifiedSolution

CONTRACT = to_checksum_address("0xac7b5d06fa1e77d08aea40d46cb7c5923a87a0cc")


def _make_submitter(monkeypatch):
    """Build a BundleSubmitter against an in-memory mock chain + a fresh miner account."""
    miner = Account.create()
    monkeypatch.setenv("HASH256_PRIVATE_KEY", miner.key.hex())
    cfg = Config()
    cfg.bundle = BundleConfig(
        enabled=True, size=4, target_blocks_ahead=1, priority_gwei=3.0, submit_concurrency=4,
        endpoints=["https://relay.flashbots.net", "https://rpc.beaverbuild.org"],
    )

    def fake_build(params):
        # Build a fully-valid EIP-1559 tx dict; sign_transaction will accept this.
        return {"to": CONTRACT, "data": "0x" + "00" * 36, "value": 0,
                "chainId": params["chainId"], "nonce": params["nonce"], "gas": params["gas"],
                "maxFeePerGas": params["maxFeePerGas"], "maxPriorityFeePerGas": params["maxPriorityFeePerGas"], "type": 2}

    chain = MagicMock()
    chain.contract.functions.mine = lambda nonce: MagicMock(build_transaction=fake_build)
    chain.w3.eth.send_raw_transaction = MagicMock(return_value=b"\x01" * 32)
    return BundleSubmitter(cfg, chain, miner), miner, cfg


def _make_sol(nonce: int) -> VerifiedSolution:
    return VerifiedSolution(challenge=b"\x00" * 32, nonce=nonce, epoch=42,
                            digest=b"\x00" * 32, difficulty=(1 << 256) - 1)


def test_bundle_payload_shape_and_signature(monkeypatch):
    bs, miner, cfg = _make_submitter(monkeypatch)
    captured = []

    def fake_post(url, data, headers, timeout):
        captured.append({"url": url, "data": data, "headers": dict(headers), "timeout": timeout})
        rsp = MagicMock()
        rsp.raise_for_status = lambda: None
        rsp.json = lambda: {"jsonrpc": "2.0", "id": 1, "result": {"bundleHash": "0x" + "ff" * 32}}
        return rsp

    sub = MagicMock()
    sub.reserve_tx_nonces.return_value = 7
    sub.record_pending = MagicMock()

    sols = [_make_sol(n) for n in (100, 101, 102, 103)]
    with patch("hashminer.bundle.requests.post", side_effect=fake_post):
        result = bs.send(sols, target_block=21_000_000, base_fee=1_000_000_000, submitter=sub)

    # one POST per configured endpoint
    assert {c["url"] for c in captured} == set(cfg.bundle.endpoints)
    # the bundle's tx_hashes get plugged back into the submitter for receipt tracking
    assert sub.record_pending.call_count == len(sols)
    assert result.sent == len(sols)
    assert result.target_block == 21_000_000

    # payload sanity-check
    body = captured[0]["data"]
    payload = json.loads(body)
    assert payload["method"] == "eth_sendBundle"
    p = payload["params"][0]
    assert p["blockNumber"] == hex(21_000_000)
    assert len(p["txs"]) == len(sols)
    assert all(t.startswith("0x") for t in p["txs"])
    # every tx is in revertingTxHashes so the bundle is still valid even if some hit BlockCapReached
    assert set(p["revertingTxHashes"]) == set(result.tx_hashes)

    # X-Flashbots-Signature: <signer_address>:<sig> over keccak(body) signed via EIP-191
    hdr = captured[0]["headers"]["X-Flashbots-Signature"]
    signer_addr, sig_hex = hdr.split(":")
    digest = "0x" + keccak(body).hex()
    recovered = Account.recover_message(encode_defunct(text=digest), signature=sig_hex)
    assert recovered.lower() == signer_addr.lower()


def test_bundle_consecutive_tx_nonces(monkeypatch):
    """Each tx in the bundle uses tx-nonce start..start+N-1 — that's what makes the builder include them in order."""
    bs, miner, cfg = _make_submitter(monkeypatch)

    sub = MagicMock()
    sub.reserve_tx_nonces.return_value = 1000
    captured_nonces = []

    orig_build = bs.chain.contract.functions.mine

    def wrap(nonce):
        m = orig_build(nonce)
        orig_bt = m.build_transaction

        def bt(params):
            captured_nonces.append(params["nonce"])
            return orig_bt(params)
        m.build_transaction = bt
        return m
    bs.chain.contract.functions.mine = wrap

    sols = [_make_sol(n) for n in (1, 2, 3, 4, 5)]
    with patch("hashminer.bundle.requests.post") as p:
        p.return_value.raise_for_status = lambda: None
        p.return_value.json = lambda: {"jsonrpc": "2.0", "id": 1, "result": {}}
        bs.send(sols, target_block=12345, base_fee=10**9, submitter=sub)

    assert captured_nonces == [1000, 1001, 1002, 1003, 1004]


def test_bundle_handles_endpoint_errors(monkeypatch):
    bs, _, cfg = _make_submitter(monkeypatch)

    def fake_post(url, **_):
        if "flashbots" in url:
            raise ConnectionError("simulated network error")
        rsp = MagicMock(); rsp.raise_for_status = lambda: None
        rsp.json = lambda: {"error": {"message": "bundle rejected"}}
        return rsp

    sub = MagicMock(); sub.reserve_tx_nonces.return_value = 0
    with patch("hashminer.bundle.requests.post", side_effect=fake_post):
        result = bs.send([_make_sol(1)], target_block=1, base_fee=10**9, submitter=sub)
    # both endpoints fail (one network, one rpc error)
    assert len(result.endpoints_ok) == 0
    assert set(result.endpoints_err) == set(cfg.bundle.endpoints)
