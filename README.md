# HASH256 Miner Bot

Standalone miner for https://hash256.org/mine.

Important: this uses the official browser WASM miner (`/miner/hash_miner.js` + `hash_miner_bg.wasm`). The website currently says "No GPU" and does not ship a CUDA/OpenCL/WebGPU kernel, so this bot is CPU/WASM, not true GPU.

## Install

Already installed in this folder:

```bash
cd /root/hash256-miner
npm install
```

## Status check

```bash
cd /root/hash256-miner
PRIVATE_KEY=0xYOUR_PRIVATE_KEY node hash256_miner_bot.mjs --status
```

## Mine 1 nonce and submit tx

```bash
cd /root/hash256-miner
PRIVATE_KEY=0xYOUR_PRIVATE_KEY node hash256_miner_bot.mjs --count 1
```

## Dry-run only (find nonce, no tx)

```bash
cd /root/hash256-miner
PRIVATE_KEY=0xYOUR_PRIVATE_KEY node hash256_miner_bot.mjs --count 1 --dry-run
```

## Run infinite

```bash
cd /root/hash256-miner
PRIVATE_KEY=0xYOUR_PRIVATE_KEY nohup node hash256_miner_bot.mjs --count 0 > hash256-miner.log 2>&1 &
tail -f hash256-miner.log
```

## Options

```bash
node hash256_miner_bot.mjs --help
```

Never paste private key into Telegram/chat. Set it only in the terminal environment.

## Quick install helper

```bash
bash install_and_run.sh
nano .env
set -a; source .env; set +a
node hash256_miner_bot.mjs --status
```
