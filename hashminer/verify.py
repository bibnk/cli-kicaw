"""CPU re-verification of GPU-reported nonces.

A GPU hit is *never* trusted blindly - every nonce is re-hashed here with the
canonical ``eth_utils.keccak`` (the same function the EVM uses) and re-checked
against the target before it can become a transaction. This catches kernel bugs,
bit-flips, and stale-target races for free.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import pow_digest


@dataclass(frozen=True)
class VerifiedSolution:
    challenge: bytes
    nonce: int
    epoch: int
    digest: bytes          # 32-byte keccak256(abi.encode(challenge, nonce))
    difficulty: int        # the target it was verified against (== on-chain currentDifficulty at find time)

    @property
    def digest_int(self) -> int:
        return int.from_bytes(self.digest, "big")

    @property
    def leading_zero_bits(self) -> int:
        return 256 - self.digest_int.bit_length() if self.digest_int else 256


def verify(challenge: bytes, nonce: int, difficulty: int, epoch: int) -> VerifiedSolution | None:
    """Return a VerifiedSolution iff uint256(keccak256(abi.encode(challenge, nonce))) < difficulty, else None."""
    if len(challenge) != 32:
        raise ValueError("challenge must be 32 bytes")
    if not 0 <= nonce < (1 << 256):
        return None
    digest = pow_digest(challenge, nonce)
    if int.from_bytes(digest, "big") < difficulty:
        return VerifiedSolution(challenge=challenge, nonce=nonce, epoch=epoch, digest=digest, difficulty=difficulty)
    return None
