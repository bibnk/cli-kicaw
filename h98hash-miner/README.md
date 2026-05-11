# H98HASH Miner Bot

Standalone miner untuk `https://www.h98hash.xyz/home`.

## DYOR hasil inspect

- Site: `https://www.h98hash.xyz/home`
- Chain: Ethereum mainnet
- Contract: `0x1E5adF70321CA28b3Ead70Eac545E6055E969e6f`
- Mint function: `mint(bytes16 nonce)` payable
- Challenge: `challengeFor(address account) -> bytes16`
- Config: `getConfig()` includes `mintOpen`, `difficulty`, `mintPrice`, `mintAmount`, `maxPublicMints`
- Proof-of-work: `SHA256(challenge_16_bytes || nonce_16_bytes)` harus punya leading zero bits sebanyak `difficulty`
- Wallet limit dari UI: 5 mints/wallet
- Gunakan burner wallet, jangan main wallet.

## Install

```bash
cd /root/cli-kicaw/h98hash-miner
npm install
```

## Status on-chain

```bash
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU
node h98_miner.mjs --status
```

## Selftest lokal

```bash
node h98_miner.mjs --selftest --workers 4
```

## Run mining

```bash
export H98_PRIVATE_KEY=0xPRIVATEKEY_BURNER_KAMU
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU
node h98_miner.mjs --workers 8 --count 1
```

Optional gas:

```bash
node h98_miner.mjs --workers 8 --count 1 --gas-gwei 2 --max-fee-gwei 50
```

Dry-run cari proof tanpa submit tx:

```bash
node h98_miner.mjs --workers 8 --dry-run
```

## Screen

```bash
screen -S h98
cd /root/cli-kicaw/h98hash-miner
export H98_PRIVATE_KEY=0xPRIVATEKEY_BURNER_KAMU
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU
node h98_miner.mjs --workers 8 --count 1
```

Detach: `CTRL+A` lalu `D`

Reattach:

```bash
screen -r h98
```

## Catatan performa

Script ini CPU multi-worker. Website memakai WebGL2/WebGPU + Wasm di browser, jadi untuk difficulty tinggi CPU bisa lambat. Untuk VPS GPU, perlu port OpenCL/CUDA agar lebih kencang.
