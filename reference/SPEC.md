# HASH256 ($HASH) proof-of-work — pinned spec

Derived from the **verified source** (`reference/Hash.sol`, Sourcify full match for
`0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc`, Solidity 0.8.26). This is the ground truth the
miner is built against.

## The hash

```solidity
function mine(uint256 nonce) external nonReentrant {
    if (!genesisComplete)                                  revert GenesisNotComplete();
    if (totalMiningMinted >= MINING_SUPPLY)                revert SupplyExhausted();
    if (mintsInBlock[block.number] >= MAX_MINTS_PER_BLOCK) revert BlockCapReached();

    bytes32 result = keccak256(abi.encode(_challenge(msg.sender), nonce));
    if (uint256(result) >= currentDifficulty) revert InsufficientWork();   // valid iff result < currentDifficulty

    bytes32 key = keccak256(abi.encode(msg.sender, nonce, _epoch()));
    if (usedProofs[key]) revert ProofAlreadyUsed();
    usedProofs[key] = true;
    ...
}

function _challenge(address miner) internal view returns (bytes32) {
    return keccak256(abi.encode(block.chainid, address(this), miner, _epoch()));
}
function _epoch() internal view returns (uint256) { return block.number / EPOCH_BLOCKS; }
```

### Concretely

- `epoch = block.number // 100`
- `challenge = keccak256( abi.encode(uint256 chainId, address contract, address miner, uint256 epoch) )`
  - `abi.encode` of `(uint256, address, address, uint256)` is **128 bytes**:
    `chainId_be32 ‖ (12 zero bytes ‖ contract[20]) ‖ (12 zero bytes ‖ miner[20]) ‖ epoch_be32`
  - i.e. `eth_abi.encode(["uint256","address","address","uint256"], [chainId, contract, miner, epoch])`, then `keccak256`.
  - Stable for 100 blocks; depends only on `(chainId, contract, miner, epoch)` → fully precomputable, including the *next* epoch.
- A nonce is **valid** iff `keccak256( abi.encode(bytes32 challenge, uint256 nonce) ) < currentDifficulty`.
  - `abi.encode(bytes32, uint256)` is **64 bytes**: `challenge[32] ‖ nonce_be32`. (Same bytes as `abi.encodePacked` here since both members are already 32 bytes.)
  - So the GPU hashes a fixed **64-byte preimage** = `challenge ‖ nonce(uint256, big-endian)`, one Keccak-f[1600] per try (64 < 136-byte rate ⇒ single absorb block; padding `0x01 … 0x80`, i.e. *original Keccak*, not SHA3-256's `0x06`).
  - The 32-byte digest is then read **big-endian as a uint256** for the `< currentDifficulty` comparison.
- `currentDifficulty` is a **target ceiling** (NOT a difficulty multiplier): larger ⇒ more hashes qualify ⇒ easier.
  - Initialised at `type(uint256).max >> 32 == 2**224 - 1` when the pool is seeded ⇒ initially you need the top 32 bits of the digest to be zero (≈ 1 in 2³² ≈ 4.29e9).
- Replay protection: each `(miner, nonce, epoch)` tuple mints once (`usedProofs[keccak256(abi.encode(miner, nonce, epoch))]`). Use a fresh nonce for every submission within an epoch.

## Constants (from the source)

| name | value |
|---|---|
| `EPOCH_BLOCKS` | `100` |
| `ADJUSTMENT_INTERVAL` | `2_016` (mints) |
| `TARGET_BLOCKS_PER_MINT` | `5` (≈ 60 s/mint at 12 s blocks ⇒ ~1 mint/min globally) |
| `MAX_MINTS_PER_BLOCK` | `10` |
| `ERA_MINTS` | `100_000` |
| `BASE_REWARD` | `100e18` (era `e` reward = `BASE_REWARD >> e` for `e < 64`, else 0) |
| `MINING_SUPPLY` | `18_900_000e18` (90%); `TOTAL_SUPPLY` `21_000_000e18` |
| `currentDifficulty` initial | `2**224 - 1` |
| genesis | `0.01 ETH` per `1_000e18` HASH unit; ≤ `5` units/tx |

## Difficulty retarget (every 2016 mints)

```
taken  = block.number - lastAdjustmentBlock          # blocks elapsed over the last 2016 mints
target = ADJUSTMENT_INTERVAL * TARGET_BLOCKS_PER_MINT # = 10_080
next   = (taken == 0) ? old/4 : mulDiv(old, taken, target)
next   = clamp(next, old/4, old*4); if next == 0: next = 1
currentDifficulty = next
```
Faster mints ⇒ smaller `taken` ⇒ smaller `next` ⇒ harder. So a GPU coming online raises difficulty over the next ~2016 mints; early on it is easy.

## Gating / lifecycle

- `mine()` reverts with `GenesisNotComplete` until `genesisComplete == true`.
- `genesisComplete` flips inside `_seedBody()`, reached via:
  - `seedPool()` — anyone, once `genesisMinted >= GENESIS_CAP` (genesis fully sold), or
  - `partialSeed()` — only `controller` (`tx.origin` at deploy), ≥ 30 min after deploy, if any genesis was minted.
- Read state via: `genesisComplete()`, `genesisState() -> (minted, remaining, ethRaised, complete)`,
  `currentDifficulty()`, `getChallenge(address) -> bytes32`, `currentReward()`, `epochBlocksLeft()`,
  `miningState() -> (era, reward, difficulty, minted, remaining, epoch, epochBlocksLeft)`, `totalMints()`, `totalMiningMinted()`, `mintsInBlock(blockNumber)`, `usedProofs(bytes32)`.
- Event on success: `Mined(address indexed miner, uint256 nonce, uint256 reward, uint256 era)`.

## Mainnet addresses

- `Hash` token / hook / miner: `0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc` (chainId 1).

## Submission

`mine(uint256 nonce)` — `nonpayable`, `nonReentrant`. Costs gas (ERC20 `_transfer` + a few SSTOREs + occasional retarget). Reverts to expect and treat as non-fatal: `BlockCapReached` (10/block hit), `ProofAlreadyUsed` (nonce reused this epoch — shouldn't happen if we track), `InsufficientWork` (target moved against you between find and inclusion — rare), `GenesisNotComplete` (not open yet), `SupplyExhausted` (done).
