"""Keccak-256 / PoW correctness: pure-Python reference vs eth_utils, and the OpenCL
kernel vs the reference. The GPU tests skip cleanly if no OpenCL device is present.
"""

from __future__ import annotations

import random

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_canonical_address

from hashminer.constants import (
    HASH_CONTRACT_ADDRESS, INITIAL_DIFFICULTY, compute_challenge, is_valid_nonce,
    pow_digest, pow_preimage, proof_key,
)

EMPTY_VECTOR = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
DEAD = "0x000000000000000000000000000000000000dEaD"


def test_keccak_empty_string_known_answer():
    assert keccak(b"").hex() == EMPTY_VECTOR


def test_pow_preimage_is_challenge_concat_nonce_be():
    ch = bytes(range(32))
    for nonce in (0, 1, 2**32 - 1, 2**128 + 7, 2**256 - 1):
        pre = pow_preimage(ch, nonce)
        assert pre == ch + nonce.to_bytes(32, "big")
        assert len(pre) == 64
        assert pow_digest(ch, nonce) == keccak(pre)
        # matches Solidity: keccak256(abi.encode(bytes32 challenge, uint256 nonce))
        assert pow_digest(ch, nonce) == keccak(abi_encode(["bytes32", "uint256"], [ch, nonce]))


def test_compute_challenge_matches_solidity_abi_encode():
    chain_id, miner, epoch = 1, DEAD, 424242
    pre = abi_encode(["uint256", "address", "address", "uint256"],
                     [chain_id, to_canonical_address(HASH_CONTRACT_ADDRESS), to_canonical_address(miner), epoch])
    assert len(pre) == 128
    assert compute_challenge(chain_id, HASH_CONTRACT_ADDRESS, miner, epoch) == keccak(pre)


def test_proof_key_matches_solidity():
    miner, nonce, epoch = DEAD, 12345, 99
    assert proof_key(miner, nonce, epoch) == keccak(
        abi_encode(["address", "uint256", "uint256"], [to_canonical_address(miner), nonce, epoch]))


def test_is_valid_nonce_threshold():
    ch = keccak(b"some-challenge").rjust(32, b"\0")
    d = int.from_bytes(pow_digest(ch, 7), "big")
    assert is_valid_nonce(ch, 7, d + 1)      # digest < d+1
    assert not is_valid_nonce(ch, 7, d)      # digest == d is NOT < d
    assert not is_valid_nonce(ch, 7, d - 1)


# --------------------------------------------------------------------------- GPU
def _first_device_or_skip():
    cl = pytest.importorskip("pyopencl")
    from hashminer.gpu import list_devices
    devs = list_devices()
    if not devs:
        pytest.skip("no OpenCL device")
    return next((d for d in devs if d.is_gpu), devs[0])


@pytest.mark.gpu
def test_gpu_kernel_matches_cpu_over_window():
    info = _first_device_or_skip()
    from hashminer.gpu import GpuWorker
    w = GpuWorker(info)
    challenge = compute_challenge(1, HASH_CONTRACT_ADDRESS, DEAD, 7)
    target = 2 ** 240  # ~1 / 65536
    N = 1 << 19
    found, n, _dt = w.search_batch(challenge, target, n=N, nonce_base=0)
    assert n == N
    for nz in found:
        assert is_valid_nonce(challenge, nz, target), f"GPU false positive nonce={nz}"
    cpu = {nz for nz in range(N) if is_valid_nonce(challenge, nz, target)}
    assert set(found) == cpu


@pytest.mark.gpu
def test_gpu_kernel_exact_digests_low_nonces():
    info = _first_device_or_skip()
    from hashminer.gpu import GpuWorker
    w = GpuWorker(info)
    challenge = bytes.fromhex("00112233445566778899aabbccddeeff0123456789abcdef0fedcba987654321")
    # target == 2^256-1 -> every nonce qualifies; check the exact set the kernel reports
    found, _n, _dt = w.search_batch(challenge, (1 << 256) - 1, n=8, nonce_base=2**200)
    assert set(found) == {2 ** 200 + i for i in range(8)}
    for nz in found:
        assert pow_digest(challenge, nz) == keccak(challenge + nz.to_bytes(32, "big"))


@pytest.mark.gpu
def test_gpu_kernel_nonce_base_wraparound():
    """nonce = nonce_base + gid with full 256-bit carry - base near 2^64-1 must ripple."""
    info = _first_device_or_skip()
    from hashminer.gpu import GpuWorker
    w = GpuWorker(info)
    challenge = bytes(32)
    base = (1 << 64) - 3  # gids 0..7 -> nonces straddle the 64-bit limb boundary
    found, _n, _dt = w.search_batch(challenge, (1 << 256) - 1, n=8, nonce_base=base)
    assert set(found) == {base + i for i in range(8)}
    # spot-check one that crossed: nonce == 2^64 has limb0 == 0, limb1 == 1
    assert (base + 3) == (1 << 64)
    assert is_valid_nonce(challenge, 1 << 64, (1 << 256) - 1)


@pytest.mark.gpu
def test_gpu_initial_difficulty_is_top_32_bits_zero():
    info = _first_device_or_skip()
    from hashminer.gpu import GpuWorker
    w = GpuWorker(info)
    challenge = compute_challenge(1, HASH_CONTRACT_ADDRESS, DEAD, 1)
    # scan ~2^24 nonces; expect ~4 hits for INITIAL_DIFFICULTY (2^224-1 -> 1 in 2^32)
    found, _n, _dt = w.search_batch(challenge, INITIAL_DIFFICULTY, n=1 << 24, nonce_base=12345)
    for nz in found:
        d = int.from_bytes(pow_digest(challenge, nz), "big")
        assert d < INITIAL_DIFFICULTY
        assert d >> 224 == 0  # top 32 bits zero
