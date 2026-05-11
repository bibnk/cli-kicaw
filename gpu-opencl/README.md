# hash256-miner

A GPU (OpenCL) proof-of-work miner for **HASH256 / $HASH** — the mineable ERC-20 at
[`0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc`](https://etherscan.io/address/0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc)
on Ethereum mainnet ([hash256.org](https://hash256.org/mine)).

The official miner brute-forces `keccak256` in a browser tab (WASM, single-digit MH/s). The
contract's proof-of-work is a plain 64-byte-preimage Keccak-256 search (`keccak256(abi.encode(challenge, nonce)) < currentDifficulty`),
which is exactly what GPUs are good at — this miner does ≈1–2 **GH/s** on a single RTX 3080,
i.e. ~10²–10³× a browser miner. See [`reference/SPEC.md`](reference/SPEC.md) for the exact PoW
(pinned from the verified source in [`reference/Hash.sol`](reference/Hash.sol)).

> ⚠️ **Status / honesty notes**
> - **Mining is live.** Genesis sold out (1,050,000 HASH / 10.5 ETH) and the pool is seeded —
>   `mine()` works now. **Running with a real funded key mints real $HASH and spends real ETH on gas.**
> - **Heavy competition + a hard 10-mints/block cap.** On-chain right now: ~30 `mine()` txs *attempted*
>   per block, only 10 succeed (~67% revert rate). The miner's default is to **skip** a solution whose
>   `estimate_gas` says `BlockCapReached` (no wasted gas). Set `gas.gas_limit = 250000` in `miner.toml`
>   to **bypass `estimate_gas` entirely** and submit every found nonce — some will revert on-chain,
>   but you stop passing on solutions that would have landed a block or two later. Even bigger upgrade:
>   turn on `[bundle]` (see below) to send N pre-signed txs as one atomic unit to MEV builders.
> - **Gas vs. reward.** Each `mine()` tx costs ~140k gas; era-1 reward is 100 HASH (genesis price
>   was 0.01 ETH / 1000 HASH). When gas is high a mint can cost more than it's worth — there's a
>   configurable cap (`gas.max_fee_gwei`) and an optional base-fee gate. A true HASH-denominated
>   break-even check needs a HASH/ETH price feed — wire one into `submit.py` if you want it.
> - **Brand-new, unaudited contract.** Read `reference/Hash.sol` before pointing a funded key at it.
>   Use a **dedicated burner key** with only enough ETH for gas.
> - This is independent software; not affiliated with hash256.org.

## How it works

```
chain.py  ──poll eth_blockNumber & miningState()──▶  miner.py  ──set_job(challenge,target,epoch)──▶  gpu.py (one OpenCL worker per device)
                                                        ▲                                                │  Found(nonce, epoch)
                                                        │                                                ▼
                                                  submit.py ◀── verify.py (CPU re-hash w/ eth_utils.keccak) ◀── results queue
                                                        │
                                                        └── build / sign / send  mine(nonce)  (EIP-1559), track receipts & `Mined` events
```

- `challenge = keccak256(abi.encode(chainId, contract, miner, epoch))`, `epoch = block.number/100` — stable for 100 blocks, derived locally (cross-checked against `getChallenge()` at startup), so the next epoch's challenge is precomputed.
- The kernel hashes `challenge ‖ nonce(uint256, big-endian)` (one Keccak-f[1600] per try) and reports any `nonce` whose digest, read big-endian, is `< currentDifficulty`.
- Every GPU hit is **re-verified on the CPU** with the same `keccak` the EVM uses before it can become a transaction.
- Multi-GPU: one worker thread per OpenCL device, disjoint nonce ranges, a shared submit path.
- Epoch rolls / difficulty retargets are picked up on the next block; in-flight results from a stale epoch are dropped.

## Install

Requires Python ≥ 3.11 and working **OpenCL** (GPU vendor driver + ICD; NVIDIA's CUDA toolkit, AMD's ROCm/Adrenalin, or Intel's runtime all ship one).

```bash
cd hash256-miner
python -m venv .venv && . .venv/Scripts/activate      # Windows;  on Linux/macOS: source .venv/bin/activate
pip install -e .                                       # add ".[dev]" for the test deps
hashminer devices                                      # sanity-check OpenCL sees your GPU(s)
hashminer selftest                                     # verify the kernel == eth_utils.keccak
hashminer bench --device all                           # measure hashrate
```

## Configure

```bash
cp miner.example.toml miner.toml      # miner.toml is gitignored so your RPC key never leaks
# edit miner.toml — every field has a sensible default
```

Key points:

- `network.rpc_url` defaults to a **public** mainnet RPC (+ a fallback list it rotates through on
  rate-limit/error). Public RPCs are fine for dev / `--dry-run` / casual mining; for the real mint
  race (10 mints/block, ~1 mint/min globally) point it at **your own node or a paid provider**.
- The miner's **private key is never read from `miner.toml`**. Provide it via the
  `HASH256_PRIVATE_KEY` environment variable or `wallet.key_file`. With no key set, the miner is
  read-only and forces `--dry-run`. (A `.env` file in the working directory is loaded automatically.)
- `gpu.devices` = `"all"` (every GPU) or a list of indices from `hashminer devices`, e.g. `[0, 1]`.
- **`gas.gas_limit = 250000`** (uncomment in `miner.toml`) — aggressive mode. Bypasses `estimate_gas`,
  accepts some on-chain `BlockCapReached` reverts in exchange for never skipping a solution that
  would have landed. Recommended once you're competing for real.

### Bundle mode (opt-in) — eth_sendBundle to MEV builders

The proven way to sweep multiple mints out of one block: pre-sign N `mine()` txs at consecutive
tx-nonces and ship them as one atomic unit to block builders (Flashbots, Beaverbuild, Titan,
Rsync, securerpc). If the bundle lands, all N mints get included before any public-mempool
competitor — that's how `0x501d…` swept all 10 slots in block 25069006.

Enable in `miner.toml`:

```toml
[bundle]
enabled              = true
size                 = 10        # txs per bundle (per-block cap is 10)
target_blocks_ahead  = 1         # target block N+1 on each new block tick
priority_gwei        = 5.0       # overrides [gas].priority_gwei for bundle txs
```

A fresh ephemeral key is generated at startup for `X-Flashbots-Signature` (no balance, just for
searcher reputation). The bundle's txs are *also* broadcast to your regular RPC as a safety net,
so a missed bundle doesn't leave the tx-nonce queue stuck.

## Commands

```
hashminer devices                                  # list OpenCL platforms / devices (flat indices)
hashminer selftest  [--device IDX]                 # kernel == eth_utils.keccak (KAT + roundtrip)
hashminer bench     [--device all|0,2] [--seconds 5.0]   # per-device + total MH/s
hashminer run       [flags below]                  # the miner
```

`hashminer run` flags:

| flag | default | what |
|---|---|---|
| `--config PATH` | `./miner.toml` if present | path to a TOML config |
| `--dry-run / --no-dry-run` | from toml (default `false`) | build & sign txs, never broadcast |
| `--rpc URL` | from toml | override `network.rpc_url` |
| `--devices "all" \| "0,1"` | from toml (`"all"`) | which OpenCL devices to mine on |
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | `INFO` | python logging level |
| `--bundle / --no-bundle` | from toml (`false`) | toggle `eth_sendBundle` mode to MEV builders |
| `--bundle-size N` | `10` | txs per bundle (per-block cap is 10) |
| `--bundle-priority-gwei F` | from toml (= `gas.priority_gwei` if null) | tip on bundled txs |

## Run — examples

```bash
# 1) DRY RUN against live mainnet (no key needed). Connects, reads miningState, derives the
#    challenge, runs the GPU, prints the mine() tx it WOULD send. Safest first thing to do.
hashminer run --dry-run

# 2) FOR REAL. Single-tx mode (default). Mints real $HASH, spends real ETH on gas.
#    PowerShell:
$env:HASH256_PRIVATE_KEY = "0xYOURKEY..."
hashminer run

# 2b) Same, but with explicit config + DEBUG logs (useful when something looks off):
hashminer run --config miner.toml --log-level DEBUG

# 3) BUNDLE MODE — the block-sweep play. Pre-signs 10 mine() txs with consecutive
#    tx-nonces and ships them as one eth_sendBundle to Flashbots / Beaverbuild / Titan /
#    Rsync / securerpc every block. The bundled txs are ALSO broadcast via your RPC as a
#    safety net so a missed bundle doesn't strand the tx-nonce queue.
hashminer run --bundle

# 3b) Bundle mode with a fatter tip (helps win the bundle slot when builders see competing bundles):
hashminer run --bundle --bundle-priority-gwei 5

# 3c) Smaller bundles (e.g. 5 txs per block) — useful if you want to leave room for other
#     activity from the same wallet, or to test the path:
hashminer run --bundle --bundle-size 5 --dry-run

# 4) READ-ONLY: no key, just watch state from a public address (or your address; mining
#    is forced off because there's no key to sign with). Combine with --dry-run.
$env:HASH256_MINER_ADDRESS = "0xYourAddress"
hashminer run --dry-run

# 5) Specific GPU(s) only — handy for a multi-GPU box where you want to leave one for the OS:
hashminer run --devices 0,1                # mines on flat-index 0 and 1
hashminer run --devices 2                  # single device

# 6) Override the RPC for a one-off (paid endpoint, custom node, etc.) without editing toml:
hashminer run --rpc "https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY"

# 7) AGGRESSIVE MODE — bypass `estimate_gas` so you submit every found nonce. Set this in
#    miner.toml under [gas] then rerun:
#       gas_limit = 250000
#    Some submissions will revert on-chain with BlockCapReached (~$0.05 each); the
#    survivors mint 100 HASH each. Net positive at current gas levels.

# 8) Quick sanity-check the GPU after install / a driver update:
hashminer selftest
hashminer bench --device all --seconds 3
```

## Environment variables

All map onto fields in `miner.toml`; env wins over toml. Useful for CI / containers / one-shot overrides where you'd rather not edit a file.

| variable | maps to | example |
|---|---|---|
| `HASH256_PRIVATE_KEY` | (signs `mine()` txs — never goes in toml) | `0x...` |
| `HASH256_RPC_URL` | `network.rpc_url` | `https://eth.llamarpc.com` |
| `HASH256_RPC_FALLBACKS` | `network.rpc_fallbacks` | `url1,url2,url3` |
| `HASH256_WS_URL` | `network.ws_url` (reserved) | `wss://...` |
| `HASH256_CONTRACT` | `network.contract` | `0xAC7b...` |
| `HASH256_CHAIN_ID` | `network.chain_id` | `1` |
| `HASH256_KEY_FILE` | `wallet.key_file` | `C:/keys/burner.key` |
| `HASH256_MINER_ADDRESS` | `wallet.miner_address` | `0xYour...` (read-only/dry-run) |
| `HASH256_GPU_DEVICES` | `gpu.devices` | `all` or `0,1` |
| `HASH256_DRY_RUN` | `behaviour.dry_run` | `1` / `true` |
| `HASH256_LOG_LEVEL` | `behaviour.log_level` | `DEBUG` |
| `HASH256_BUNDLE` | `bundle.enabled` | `1` / `true` |
| `HASH256_BUNDLE_SIZE` | `bundle.size` | `10` |
| `HASH256_BUNDLE_PRIORITY_GWEI` | `bundle.priority_gwei` | `5.0` |

A `.env` file in the working directory is auto-loaded (via `python-dotenv`) — drop your `HASH256_PRIVATE_KEY=0x...` there and it never touches your shell history. `.env` is in `.gitignore`.

## Test

```bash
pip install -e ".[dev]"
pytest                                  # keccak/PoW correctness + GPU kernel-vs-CPU (GPU tests skip if no OpenCL device)

# full end-to-end against a local mainnet fork:
anvil --fork-url $MAINNET_RPC --port 8545
FORK_RPC_URL=http://127.0.0.1:8545 pytest tests/test_fork_e2e.py -v -s
```

The fork test opens mining the honest way (impersonate `controller` → `mintGenesis` → warp 30 min →
`partialSeed`), drops `currentDifficulty` to an easy value, funds a fresh key, runs the miner, and
asserts a `Mined` event landed and the account's $HASH balance went up. It skips cleanly if
`FORK_RPC_URL` isn't set, or if Uniswap-V4 seeding reverts on that particular fork.

## Layout

| path | what |
|---|---|
| `hashminer/kernels/keccak256.cl` | OpenCL Keccak-256 PoW search kernel (single-block absorb, MSB-first early-exit, atomic hit buffer) |
| `hashminer/gpu.py` | OpenCL device discovery, the per-device `GpuWorker`, the multi-GPU `GpuFarm` |
| `hashminer/chain.py` | web3.py wrapper: contract reads, block following, RPC rotation |
| `hashminer/verify.py` | CPU re-verification of every GPU hit |
| `hashminer/submit.py` | `mine()` tx build/sign/send, tx-nonce mgmt, gas + profitability gates, receipt/`Mined` tracking |
| `hashminer/miner.py` | orchestrator (wait-for-open → set job per epoch → drain → submit) |
| `hashminer/config.py` | `miner.toml` + env loading; key handling |
| `hashminer/constants.py` | on-chain constants + the PoW in pure Python (ground truth for the kernel) |
| `hashminer/abi.py` | bundled contract ABI (`scripts/fetch_abi.py` regenerates it) |
| `hashminer/bundle.py` | optional `eth_sendBundle` path to Flashbots / Beaverbuild / Titan / Rsync / securerpc |
| `reference/` | the verified `Hash.sol`, its metadata, and `SPEC.md` |

## Tuning / notes

- Default OpenCL work-group size is 64 (good on NVIDIA); override with `gpu.local_size`. Batch size auto-tunes toward ~80 ms/launch so workers react quickly to new epochs.
- The kernel uses a straightforward reference Keccak-f[1600]; there's headroom (lane-complement trick, bit-interleaving, fully-unrolled rounds) if you want more H/s.
- WebSocket `newHeads` (`network.ws_url`) is reserved for a future version — this one polls `eth_blockNumber` every `poll_interval_s`, which is plenty given 12 s blocks and a per-epoch (100-block) challenge.
