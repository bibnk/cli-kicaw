# HASH256 GPU Miner (OpenCL)

Ini miner GPU OpenCL untuk HASH256. Cocok untuk Vast.ai RTX 5090.

## Install di VPS Vast Ubuntu/root

```bash
apt update
apt install -y git python3 python3-venv python3-pip ocl-icd-opencl-dev clinfo
clinfo | head -80

cd /root/cli-kicaw/gpu-opencl
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## Cek GPU

```bash
hashminer devices
hashminer selftest
hashminer bench --device all --seconds 5
```

## Dry-run dulu

Tanpa private key:

```bash
hashminer run --dry-run --devices all
```

Dengan address wallet untuk challenge wallet kamu:

```bash
export HASH256_MINER_ADDRESS=0xWALLET_KAMU
hashminer run --dry-run --devices all
```

## Run real mining

Pakai burner wallet, isi ETH gas secukupnya. Jangan pakai wallet utama.

```bash
export HASH256_PRIVATE_KEY=0xPRIVATEKEY_KAMU
export HASH256_RPC_URL=https://ethereum-rpc.publicnode.com
hashminer run --devices all
```

## Background

Jalankan di screen/tmux agar tidak mati saat SSH putus:

```bash
apt install -y screen
screen -S hash256
cd /root/cli-kicaw/gpu-opencl
source .venv/bin/activate
export HASH256_PRIVATE_KEY=0xPRIVATEKEY_KAMU
export HASH256_RPC_URL=https://ethereum-rpc.publicnode.com
hashminer run --devices all
```

Keluar dari screen tanpa stop: tekan `CTRL+A` lalu `D`.
Masuk lagi:

```bash
screen -r hash256
```

## Catatan

- GPU miner ini beda dengan `hash256_miner_bot.mjs` yang WASM/CPU.
- Untuk RTX 5090, gunakan `hashminer bench` untuk lihat hashrate nyata.
- Kalau OpenCL tidak detect GPU, Vast image/template belum punya NVIDIA OpenCL runtime. Pakai image CUDA/PyTorch NVIDIA.
