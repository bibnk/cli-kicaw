"""On-chain constants and the pure-Python reference implementation of the PoW.

These mirror `reference/Hash.sol` exactly. The on-chain values are also re-read at
runtime via `chain.py` (constants here are the source of truth for *encoding* and a
sanity fallback for the numeric constants).
"""

from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_canonical_address

# --- mainnet ---------------------------------------------------------------
HASH_CONTRACT_ADDRESS = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
MAINNET_CHAIN_ID = 1

# --- contract constants (see reference/Hash.sol) ---------------------------
EPOCH_BLOCKS = 100
ADJUSTMENT_INTERVAL = 2_016        # mints
TARGET_BLOCKS_PER_MINT = 5
MAX_MINTS_PER_BLOCK = 10
ERA_MINTS = 100_000
BASE_REWARD = 100 * 10**18
MINING_SUPPLY = 18_900_000 * 10**18
TOTAL_SUPPLY = 21_000_000 * 10**18

# currentDifficulty is a *target ceiling*: a digest is valid iff (uint256(digest) < currentDifficulty).
# Larger value => easier. Seeded at type(uint256).max >> 32.
INITIAL_DIFFICULTY = (2**256 - 1) >> 32
UINT256_MAX = 2**256 - 1
NONCE_SPACE = 2**256

# --- the proof-of-work, in Python (ground truth for cross-checking the kernel) ----------


def epoch_for_block(block_number: int) -> int:
    """epoch = block.number / EPOCH_BLOCKS (Solidity integer division)."""
    return block_number // EPOCH_BLOCKS


def epoch_blocks_left(block_number: int) -> int:
    return EPOCH_BLOCKS - (block_number % EPOCH_BLOCKS)


def compute_challenge(chain_id: int, contract: str, miner: str, epoch: int) -> bytes:
    """challenge = keccak256(abi.encode(uint256 chainId, address contract, address miner, uint256 epoch)).

    Returns the 32-byte challenge. Matches `Hash._challenge`.
    """
    preimage = abi_encode(
        ["uint256", "address", "address", "uint256"],
        [chain_id, to_canonical_address(contract), to_canonical_address(miner), epoch],
    )
    assert len(preimage) == 128, len(preimage)
    return keccak(preimage)


def pow_preimage(challenge: bytes, nonce: int) -> bytes:
    """abi.encode(bytes32 challenge, uint256 nonce) == challenge || nonce_be32  (64 bytes)."""
    if len(challenge) != 32:
        raise ValueError("challenge must be 32 bytes")
    if not (0 <= nonce < NONCE_SPACE):
        raise ValueError("nonce out of uint256 range")
    return challenge + nonce.to_bytes(32, "big")


def pow_digest(challenge: bytes, nonce: int) -> bytes:
    """The 32-byte keccak256 digest the contract checks against currentDifficulty."""
    return keccak(pow_preimage(challenge, nonce))


def is_valid_nonce(challenge: bytes, nonce: int, difficulty: int) -> bool:
    """True iff uint256(keccak256(abi.encode(challenge, nonce))) < difficulty (== `currentDifficulty`)."""
    return int.from_bytes(pow_digest(challenge, nonce), "big") < difficulty


def proof_key(miner: str, nonce: int, epoch: int) -> bytes:
    """usedProofs key = keccak256(abi.encode(address miner, uint256 nonce, uint256 epoch))."""
    return keccak(abi_encode(["address", "uint256", "uint256"], [to_canonical_address(miner), nonce, epoch]))


def era_for_total_mints(total_mints: int) -> int:
    return total_mints // ERA_MINTS


def reward_for_era(era: int) -> int:
    return (BASE_REWARD >> era) if era < 64 else 0
