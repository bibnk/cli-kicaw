# cli-kicaw / HASH256 GPU Miner

Repo ini sekarang berisi **HASH256 GPU miner beneran** di folder:

```bash
gpu-opencl/
```

Miner lama Node/WASM masih ada sebagai arsip, tapi untuk mining cepat pakai **OpenCL GPU miner** ini.

## Quick install di VPS GPU baru

Untuk Ubuntu/Debian VPS GPU / Vast.ai:

```bash
apt update
apt install -y git python3 python3-venv python3-pip ocl-icd-opencl-dev clinfo screen

nvidia-smi
clinfo | head -80

cd /root
git clone https://github.com/bibnk/cli-kicaw.git
cd /root/cli-kicaw/gpu-opencl

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

hashminer devices
hashminer selftest
hashminer bench --device all --tune-local-size --seconds 3
```

Kalau `nvidia-smi` error, driver NVIDIA di VPS belum aktif/terinstall.

## Config recommended

```bash
cd /root/cli-kicaw/gpu-opencl
cp miner.example.toml miner.toml
nano miner.toml
```

Isi/pastikan:

```toml
[gpu]
devices = "all"
local_size = 256          # ganti sesuai BEST local_size dari benchmark kalau beda
batch_target_ms = 500.0

[gas]
priority_gwei = 6.0
gas_limit = 250000
max_fee_gwei = 80.0
base_fee_multiplier = 3.0

[bundle]
enabled = true
size = 10
target_blocks_ahead = 1
priority_gwei = 6.0
```

## Run mining

Pakai **burner wallet**, jangan main wallet.

```bash
cd /root/cli-kicaw/gpu-opencl
source .venv/bin/activate

export HASH256_PRIVATE_KEY=0xPRIVATEKEY_KAMU
export HASH256_RPC_URL=RPC_PREMIUM_KAMU

hashminer run --devices all --local-size 256 --batch-target-ms 500 --bundle
```

Kalau benchmark kasih `BEST local_size` selain 256, ganti angka `--local-size` sesuai hasil itu.

## Run pakai screen

```bash
screen -S hash256
cd /root/cli-kicaw/gpu-opencl
source .venv/bin/activate
export HASH256_PRIVATE_KEY=0xPRIVATEKEY_KAMU
export HASH256_RPC_URL=RPC_PREMIUM_KAMU
hashminer run --devices all --local-size 256 --batch-target-ms 500 --bundle
```

Detach:

```text
CTRL + A lalu D
```

Masuk lagi:

```bash
screen -r hash256
```

Stop miner:

```text
CTRL + C
```

## Update VPS existing

```bash
cd /root/cli-kicaw
git pull origin main
cd /root/cli-kicaw/gpu-opencl
source .venv/bin/activate
pip install -e .

hashminer bench --device all --tune-local-size --seconds 3
hashminer run --devices all --local-size 256 --batch-target-ms 500 --bundle
```

## Commands penting

```bash
hashminer devices
hashminer selftest
hashminer bench --device all --tune-local-size --seconds 3
hashminer run --devices all --local-size 256 --batch-target-ms 500 --bundle
```

Detail lengkap ada di:

```bash
gpu-opencl/README.md
```
