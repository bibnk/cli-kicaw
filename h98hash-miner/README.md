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

---

# GPU OpenCL miner

## Install fresh VPS GPU

```bash
apt update
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.11 python3.11-venv python3.11-dev
apt install -y git python3-pip ocl-icd-opencl-dev clinfo screen

nvidia-smi
clinfo | head -80

cd /root
git clone https://github.com/bibnk/cli-kicaw.git
cd /root/cli-kicaw/h98hash-miner

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -r requirements-gpu.txt
```

## Cek status dan GPU

```bash
source .venv/bin/activate
python h98_gpu_miner.py status
python h98_gpu_miner.py devices
```

## Selftest GPU

```bash
python h98_gpu_miner.py selftest --devices all --local-size 256 --global-size 262144 --iter 64
```

## Run GPU mining

```bash
export H98_PRIVATE_KEY=0xPRIVATEKEY_BURNER_KAMU
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU

python h98_gpu_miner.py run --devices all --local-size 256 --global-size 1048576 --iter 256 --count 1
```

Dengan gas custom:

```bash
python h98_gpu_miner.py run --devices all --count 1 --gas-gwei 2 --max-fee-gwei 50
```

Dry-run cari proof tanpa submit tx:

```bash
python h98_gpu_miner.py run --devices all --dry-run
```

## Screen GPU

```bash
screen -S h98gpu
cd /root/cli-kicaw/h98hash-miner
source .venv/bin/activate
export H98_PRIVATE_KEY=0xPRIVATEKEY_BURNER_KAMU
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU
python h98_gpu_miner.py run --devices all --local-size 256 --global-size 1048576 --iter 256 --count 1
```

Detach: `CTRL+A` lalu `D`

Reattach:

```bash
screen -r h98gpu
```

---

# CPU Node miner

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

## Run CPU mining

```bash
export H98_PRIVATE_KEY=0xPRIVATEKEY_BURNER_KAMU
export H98_RPC_URL=RPC_ETH_MAINNET_KAMU
node h98_miner.mjs --workers 8 --count 1
```

## Catatan performa

- `h98_gpu_miner.py` = GPU OpenCL, lebih cocok untuk VPS GPU.
- `h98_miner.mjs` = CPU multi-worker fallback.
- Pakai RPC Ethereum premium supaya submit tidak telat.
- Burner wallet only.
