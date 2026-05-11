"""End-to-end miner test against a local Anvil fork of Ethereum mainnet.

Skipped unless FORK_RPC_URL points at a running anvil node, e.g.:

    anvil --fork-url $MAINNET_RPC --port 8545
    FORK_RPC_URL=http://127.0.0.1:8545 pytest tests/test_fork_e2e.py -v -s

What it does on the fork:
  1. open mining the honest way - impersonate `controller`, mint some genesis,
     warp past the 30-min delay, call `partialSeed()` (which seeds the Uniswap-V4
     pool and flips `genesisComplete`). If seeding reverts on this fork it skips.
  2. locate `currentDifficulty`'s storage slot at runtime and poke it to an easy
     target so the GPU finds solutions in milliseconds.
  3. fund a fresh miner key, run the miner for a few seconds, assert it landed at
     least one `Mined` event for our address and our $HASH balance went up.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

FORK_RPC_URL = os.environ.get("FORK_RPC_URL")
pytestmark = [pytest.mark.fork,
              pytest.mark.skipif(not FORK_RPC_URL, reason="set FORK_RPC_URL to a running anvil fork to run this")]

EASY_DIFFICULTY = 1 << 250  # ~1 in 64 hashes


def _rpc(w3, method, params):
    return w3.provider.make_request(method, params)


@pytest.fixture
def w3():
    pytest.importorskip("pyopencl")
    from web3 import Web3
    w = Web3(Web3.HTTPProvider(FORK_RPC_URL, request_kwargs={"timeout": 30}))
    assert w.is_connected(), f"cannot connect to {FORK_RPC_URL}"
    return w


@pytest.fixture
def hash_contract(w3):
    from eth_utils import to_checksum_address
    from hashminer.abi import HASH_ABI, HASH_CONTRACT_ADDRESS
    return w3.eth.contract(address=to_checksum_address(HASH_CONTRACT_ADDRESS), abi=HASH_ABI)


def _send_as(w3, sender, fn, **overrides):
    """Send a contract call from an impersonated `sender` (no private key needed on anvil)."""
    tx = fn.build_transaction({"from": sender, "gasPrice": w3.eth.gas_price,
                               "nonce": w3.eth.get_transaction_count(sender), **overrides})
    h = _rpc(w3, "eth_sendTransaction", [tx])["result"]
    return w3.eth.wait_for_transaction_receipt(h)


def _open_mining(w3, c):
    """Make `genesisComplete()` true on the fork via the real partialSeed path. Skips on revert."""
    if c.functions.genesisComplete().call():
        return
    controller = c.functions.controller().call()
    _rpc(w3, "anvil_impersonateAccount", [controller])
    _rpc(w3, "anvil_setBalance", [controller, hex(50 * 10**18)])
    try:
        # mint enough genesis that the pool has non-zero liquidity (5 units = 5000 HASH / 0.05 ETH per tx)
        for _ in range(8):
            r = _send_as(w3, controller, c.functions.mintGenesis(5), value=5 * 10**16, gas=500_000)
            assert r["status"] == 1
        # warp past deployedAt + 30 min, then seed
        _rpc(w3, "evm_increaseTime", [40 * 60])
        _rpc(w3, "evm_mine", [])
        r = _send_as(w3, controller, c.functions.partialSeed(), gas=8_000_000)
        assert r["status"] == 1
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not open mining on this fork (genesis/Uniswap-V4 seeding reverted): {exc}")
    finally:
        _rpc(w3, "anvil_stopImpersonatingAccount", [controller])
    assert c.functions.genesisComplete().call()


def _make_difficulty_easy(w3, c):
    cur = c.functions.currentDifficulty().call()
    if cur <= EASY_DIFFICULTY:
        return
    addr = c.address
    slot = None
    for s in range(40):
        v = int(w3.eth.get_storage_at(addr, s).hex(), 16)
        if v == cur:
            slot = s
            break
    assert slot is not None, "could not locate currentDifficulty storage slot"
    _rpc(w3, "anvil_setStorageAt", [addr, hex(slot), "0x" + EASY_DIFFICULTY.to_bytes(32, "big").hex()])
    assert c.functions.currentDifficulty().call() == EASY_DIFFICULTY


def test_e2e_mine_against_fork(w3, hash_contract, tmp_path, monkeypatch):
    from eth_account import Account
    from hashminer.config import Config
    from hashminer.miner import Miner

    _open_mining(w3, hash_contract)
    _make_difficulty_easy(w3, hash_contract)

    acct = Account.create()
    _rpc(w3, "anvil_setBalance", [acct.address, hex(10 * 10**18)])
    monkeypatch.setenv("HASH256_PRIVATE_KEY", acct.key.hex())

    cfg = Config()
    cfg.rpc_url = FORK_RPC_URL
    cfg.rpc_fallbacks = []
    cfg.chain_id = w3.eth.chain_id
    cfg.poll_interval_s = 1.0
    cfg.gas.max_fee_gwei = None  # don't gate on price on a fork

    miner = Miner(cfg)
    before = hash_contract.functions.totalMiningMinted().call()
    t = threading.Thread(target=miner.run, daemon=True)
    t.start()
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            # nudge the fork forward so blocks (and tx inclusions) happen
            _rpc(w3, "evm_mine", [])
            if hash_contract.functions.totalMiningMinted().call() > before:
                break
            time.sleep(1.0)
    finally:
        miner.shutdown()
        t.join(timeout=5)

    after = hash_contract.functions.totalMiningMinted().call()
    bal = hash_contract.functions.balanceOf(acct.address).call()
    assert after > before, "no mining mint landed on the fork"
    assert bal > 0, "miner account did not receive any $HASH"
    assert miner.submitter.confirmed >= 1
