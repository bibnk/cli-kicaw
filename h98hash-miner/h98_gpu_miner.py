#!/usr/bin/env python3
"""H98HASH GPU OpenCL miner.

Mines the h98hash.xyz proof locally on OpenCL GPUs and submits mint(bytes16 nonce)
with a burner wallet. Secrets are read only from env/CLI; do not hardcode keys.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import struct
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import numpy as np

try:
    import pyopencl as cl
except Exception as exc:  # pragma: no cover
    cl = None
    _CL_IMPORT_ERROR = exc
else:
    _CL_IMPORT_ERROR = None

try:
    from web3 import Web3
except Exception:  # pragma: no cover
    Web3 = None

CONTRACT = Web3.to_checksum_address("0x1E5adF70321CA28b3Ead70Eac545E6055E969e6f") if Web3 else "0x1E5adF70321CA28b3Ead70Eac545E6055E969e6f"
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
ABI = [
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"challengeFor","outputs":[{"internalType":"bytes16","name":"","type":"bytes16"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getConfig","outputs":[{"components":[{"internalType":"bool","name":"mintOpen","type":"bool"},{"internalType":"bool","name":"marketOpen","type":"bool"},{"internalType":"bool","name":"listingOpen","type":"bool"},{"internalType":"bool","name":"buyingOpen","type":"bool"},{"internalType":"bool","name":"batchOpen","type":"bool"},{"internalType":"enum TEAD.MarketMode","name":"marketMode","type":"uint8"},{"internalType":"uint256","name":"difficulty","type":"uint256"},{"internalType":"uint256","name":"mintPrice","type":"uint256"},{"internalType":"uint256","name":"mintAmount","type":"uint256"},{"internalType":"uint256","name":"maxPublicMints","type":"uint256"},{"internalType":"uint256","name":"treasuryReserveMints","type":"uint256"},{"internalType":"uint256","name":"lotSize","type":"uint256"},{"internalType":"uint256","name":"minListingAmount","type":"uint256"},{"internalType":"uint256","name":"maxBatchSize","type":"uint256"},{"internalType":"uint256","name":"marketFeeBps","type":"uint256"},{"internalType":"address","name":"feeRecipient","type":"address"}],"internalType":"struct TEAD.Config","name":"","type":"tuple"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getStats","outputs":[{"internalType":"uint256","name":"publicMinted_","type":"uint256"},{"internalType":"uint256","name":"treasuryReserved_","type":"uint256"},{"internalType":"uint256","name":"totalSupply_","type":"uint256"},{"internalType":"uint256","name":"activeListings_","type":"uint256"},{"internalType":"uint256","name":"difficulty_","type":"uint256"},{"internalType":"bool","name":"mintOpen_","type":"bool"},{"internalType":"bool","name":"marketOpen_","type":"bool"},{"internalType":"bool","name":"listingOpen_","type":"bool"},{"internalType":"bool","name":"buyingOpen_","type":"bool"},{"internalType":"bool","name":"batchOpen_","type":"bool"},{"internalType":"enum TEAD.MarketMode","name":"marketMode_","type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"bytes16","name":"nonce","type":"bytes16"}],"name":"mint","outputs":[{"internalType":"uint256","name":"mintIndex","type":"uint256"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"mintNonce","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

KERNEL = r"""
#pragma OPENCL EXTENSION cl_khr_global_int32_base_atomics : enable
#define ROTR(x,n) rotate((uint)(x), (uint)(32-(n)))
#define Ch(x,y,z) ((x & (y ^ z)) ^ z)
#define Maj(x,y,z) ((x & (y | z)) | (y & z))
#define S0(x) (ROTR(x,2) ^ ROTR(x,13) ^ ROTR(x,22))
#define S1(x) (ROTR(x,6) ^ ROTR(x,11) ^ ROTR(x,25))
#define s0(x) (ROTR(x,7) ^ ROTR(x,18) ^ ((x) >> 3))
#define s1(x) (ROTR(x,17) ^ ROTR(x,19) ^ ((x) >> 10))

__constant uint K[64] = {
  0x428A2F98u,0x71374491u,0xB5C0FBCFu,0xE9B5DBA5u,0x3956C25Bu,0x59F111F1u,0x923F82A4u,0xAB1C5ED5u,
  0xD807AA98u,0x12835B01u,0x243185BEu,0x550C7DC3u,0x72BE5D74u,0x80DEB1FEu,0x9BDC06A7u,0xC19BF174u,
  0xE49B69C1u,0xEFBE4786u,0x0FC19DC6u,0x240CA1CCu,0x2DE92C6Fu,0x4A7484AAu,0x5CB0A9DCu,0x76F988DAu,
  0x983E5152u,0xA831C66Du,0xB00327C8u,0xBF597FC7u,0xC6E00BF3u,0xD5A79147u,0x06CA6351u,0x14292967u,
  0x27B70A85u,0x2E1B2138u,0x4D2C6DFCu,0x53380D13u,0x650A7354u,0x766A0ABBu,0x81C2C92Eu,0x92722C85u,
  0xA2BFE8A1u,0xA81A664Bu,0xC24B8B70u,0xC76C51A3u,0xD192E819u,0xD6990624u,0xF40E3585u,0x106AA070u,
  0x19A4C116u,0x1E376C08u,0x2748774Cu,0x34B0BCB5u,0x391C0CB3u,0x4ED8AA4Au,0x5B9CCA4Fu,0x682E6FF3u,
  0x748F82EEu,0x78A5636Fu,0x84C87814u,0x8CC70208u,0x90BEFFFAu,0xA4506CEBu,0xBEF9A3F7u,0xC67178F2u
};

__kernel void h98_sha256_search(
    uint w0, uint w1, uint w2, uint w3,
    uint seed0, uint seed1, uint iter_count,
    uint mask0, uint mask1,
    __global uint *out)
{
    uint gid = (uint)get_global_id(0);
    for (uint i = 0; i < iter_count; i++) {
        uint W[64];
        W[0]=w0; W[1]=w1; W[2]=w2; W[3]=w3;
        W[4]=seed0; W[5]=seed1; W[6]=gid; W[7]=i;
        W[8]=0x80000000u;
        for (int j=9; j<15; j++) W[j]=0u;
        W[15]=256u;
        for (int j=16; j<64; j++) W[j] = s1(W[j-2]) + W[j-7] + s0(W[j-15]) + W[j-16];
        uint a=0x6A09E667u,b=0xBB67AE85u,c=0x3C6EF372u,d=0xA54FF53Au;
        uint e=0x510E527Fu,f=0x9B05688Cu,g=0x1F83D9ABu,h=0x5BE0CD19u;
        for (int j=0; j<64; j++) {
            uint t1 = h + S1(e) + Ch(e,f,g) + K[j] + W[j];
            uint t2 = S0(a) + Maj(a,b,c);
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        uint H0 = a + 0x6A09E667u;
        uint H1 = b + 0xBB67AE85u;
        if (((H0 & mask0) == 0u) && ((H1 & mask1) == 0u)) {
            if (atomic_cmpxchg((volatile __global int*)out, 0, 1) == 0) {
                out[1]=seed0; out[2]=seed1; out[3]=gid; out[4]=i; out[5]=H0; out[6]=H1;
            }
            return;
        }
    }
}
"""

@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    platform: str
    is_gpu: bool
    compute_units: int
    max_work_group_size: int
    device: object


def require_cl():
    if cl is None:
        raise RuntimeError(f"pyopencl not installed/importable: {_CL_IMPORT_ERROR}; install requirements-gpu.txt")


def list_devices() -> list[DeviceInfo]:
    require_cl()
    out = []
    idx = 0
    try:
        platforms = cl.get_platforms()
    except Exception as exc:
        if "PLATFORM_NOT_FOUND" in str(exc):
            return []
        raise
    for plat in platforms:
        for dev in plat.get_devices():
            out.append(DeviceInfo(idx, dev.name.strip(), plat.name.strip(), bool(dev.type & cl.device_type.GPU), dev.max_compute_units, dev.max_work_group_size, dev))
            idx += 1
    return out


def pick_devices(spec: str) -> list[DeviceInfo]:
    devs = list_devices()
    if not devs:
        raise RuntimeError("no OpenCL devices found; check nvidia-smi and clinfo")
    if spec in ("all", "gpu", "*"):
        gpus = [d for d in devs if d.is_gpu]
        return gpus or devs
    want = {int(x) for x in spec.split(",") if x.strip()}
    chosen = [d for d in devs if d.index in want]
    if len(chosen) != len(want):
        raise RuntimeError(f"some device indices not found; run devices first")
    return chosen


def masks(difficulty: int) -> tuple[int, int]:
    if difficulty < 0 or difficulty > 64:
        raise ValueError("this miner supports difficulty 0..64")
    if difficulty == 0:
        return 0, 0
    if difficulty <= 32:
        return ((0xFFFFFFFF << (32 - difficulty)) & 0xFFFFFFFF), 0
    return 0xFFFFFFFF, ((0xFFFFFFFF << (64 - difficulty)) & 0xFFFFFFFF)


def verify(challenge: bytes, nonce: bytes, difficulty: int) -> bytes | None:
    digest = hashlib.sha256(challenge + nonce).digest()
    n = int.from_bytes(digest, "big")
    if n >> (256 - difficulty) == 0:
        return digest
    return None


class OpenCLWorker:
    def __init__(self, info: DeviceInfo, local_size: int = 256, global_size: int = 1 << 20, iter_count: int = 256):
        require_cl()
        self.info = info
        self.ctx = cl.Context([info.device])
        self.queue = cl.CommandQueue(self.ctx)
        self.program = cl.Program(self.ctx, KERNEL).build()
        self.kernel = self.program.h98_sha256_search
        self.local_size = max(1, min(local_size, info.max_work_group_size))
        self.global_size = ((global_size + self.local_size - 1) // self.local_size) * self.local_size
        self.iter_count = int(iter_count)
        self.out = cl.Buffer(self.ctx, cl.mem_flags.READ_WRITE, 7 * 4)

    def batch(self, challenge: bytes, difficulty: int) -> tuple[bytes | None, bytes | None, int, float]:
        words = struct.unpack(">4I", challenge)
        seed0, seed1 = secrets.randbits(32), secrets.randbits(32)
        mask0, mask1 = masks(difficulty)
        zeros = np.zeros(7, dtype=np.uint32)
        cl.enqueue_copy(self.queue, self.out, zeros)
        t0 = time.perf_counter()
        self.kernel(self.queue, (self.global_size,), (self.local_size,),
                    *(np.uint32(x) for x in (*words, seed0, seed1, self.iter_count, mask0, mask1)), self.out)
        result = np.empty(7, dtype=np.uint32)
        cl.enqueue_copy(self.queue, result, self.out)
        self.queue.finish()
        dt = time.perf_counter() - t0
        tried = self.global_size * self.iter_count
        if int(result[0]) == 1:
            nonce = struct.pack(">4I", int(result[1]), int(result[2]), int(result[3]), int(result[4]))
            digest = verify(challenge, nonce, difficulty)
            if digest:
                return nonce, digest, tried, dt
            print("WARN: GPU reported invalid nonce; ignoring", file=sys.stderr)
        return None, None, tried, dt


def w3_contract(rpc: str, key: str | None):
    if Web3 is None:
        raise RuntimeError("web3 is not installed; pip install -r requirements-gpu.txt")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    acct = w3.eth.account.from_key(key) if key else None
    return w3, acct, w3.eth.contract(address=CONTRACT, abi=ABI)


def print_status(rpc: str, key: str | None):
    w3, acct, c = w3_contract(rpc, key)
    cfg = c.functions.getConfig().call()
    stats = c.functions.getStats().call()
    print(f"RPC: {rpc}")
    print(f"Contract: {CONTRACT}")
    print(f"Mint open: {cfg[0]}")
    print(f"Difficulty: {cfg[6]}")
    print(f"Mint price: {w3.from_wei(cfg[7], 'ether')} ETH")
    print(f"Mint amount: {cfg[8] / 10**18:g} H98")
    print(f"Public minted: {stats[0]} / {cfg[9]}")
    if acct:
        print(f"Wallet: {acct.address}")
        print(f"Wallet mints: {c.functions.mintNonce(acct.address).call()} / 5")
        print(f"Balance: {c.functions.balanceOf(acct.address).call() / 10**18:g} H98")


def mine_proof(challenge: bytes, difficulty: int, devices: list[DeviceInfo], local_size: int, global_size: int, iter_count: int):
    workers = [OpenCLWorker(d, local_size, global_size, iter_count) for d in devices]
    total = 0
    start = time.perf_counter()
    print("OpenCL devices:")
    for d in devices:
        print(f"  [{d.index}] {d.name} ({d.platform}, CU={d.compute_units})")
    while True:
        for w in workers:
            nonce, digest, tried, dt = w.batch(challenge, difficulty)
            total += tried
            mh = tried / max(dt, 1e-9) / 1e6
            avg = total / max(time.perf_counter() - start, 1e-9) / 1e6
            print(f"[{w.info.index}] {mh:.2f} MH/s batch | avg={avg:.2f} MH/s | searched={total:,}", flush=True)
            if nonce:
                return nonce, digest, avg


def submit_mint(rpc: str, key: str, nonce: bytes, gas_gwei: float | None, max_fee_gwei: float | None):
    w3, acct, c = w3_contract(rpc, key)
    cfg = c.functions.getConfig().call()
    tx = c.functions.mint(nonce).build_transaction({
        "from": acct.address,
        "value": int(cfg[7]),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": 1,
    })
    if gas_gwei is not None:
        tx["maxPriorityFeePerGas"] = w3.to_wei(gas_gwei, "gwei")
    if max_fee_gwei is not None:
        tx["maxFeePerGas"] = w3.to_wei(max_fee_gwei, "gwei")
    if "gas" not in tx:
        tx["gas"] = int(c.functions.mint(nonce).estimate_gas({"from": acct.address, "value": int(cfg[7])}) * 1.25)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or signed.raw_transaction
    h = w3.eth.send_raw_transaction(raw)
    print(f"TX sent: {h.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print(f"TX confirmed: block={rcpt.blockNumber} status={rcpt.status} gasUsed={rcpt.gasUsed}")


def main():
    p = argparse.ArgumentParser(description="H98HASH GPU OpenCL miner")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    st = sub.add_parser("status")
    st.add_argument("--rpc", default=os.getenv("H98_RPC_URL", DEFAULT_RPC))
    st.add_argument("--key", default=os.getenv("H98_PRIVATE_KEY"))
    sf = sub.add_parser("selftest")
    sf.add_argument("--devices", default="all")
    sf.add_argument("--local-size", type=int, default=256)
    sf.add_argument("--global-size", type=int, default=1 << 18)
    sf.add_argument("--iter", type=int, default=64)
    run = sub.add_parser("run")
    run.add_argument("--rpc", default=os.getenv("H98_RPC_URL", DEFAULT_RPC))
    run.add_argument("--key", default=os.getenv("H98_PRIVATE_KEY"))
    run.add_argument("--devices", default="all")
    run.add_argument("--local-size", type=int, default=256)
    run.add_argument("--global-size", type=int, default=1 << 20)
    run.add_argument("--iter", type=int, default=256)
    run.add_argument("--count", type=int, default=1, help="mints to attempt; 0=infinite")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--gas-gwei", type=float)
    run.add_argument("--max-fee-gwei", type=float)
    args = p.parse_args()

    if args.cmd == "devices":
        devs = list_devices()
        if not devs:
            print("No OpenCL devices found. Check: nvidia-smi && clinfo")
            return
        for d in devs:
            print(f"[{d.index}] {d.name} ({d.platform}) GPU={d.is_gpu} CU={d.compute_units} max_wg={d.max_work_group_size}")
        return
    if args.cmd == "status":
        print_status(args.rpc, args.key)
        return
    if args.cmd == "selftest":
        nonce, digest, avg = mine_proof(b"\x11" * 16, 16, pick_devices(args.devices), args.local_size, args.global_size, args.iter)
        print(f"SELFTEST OK nonce=0x{nonce.hex()} digest=0x{digest.hex()} avg={avg:.2f} MH/s")
        return
    if args.cmd == "run":
        if not args.key:
            raise SystemExit("Set H98_PRIVATE_KEY=0x... burner wallet private key")
        minted = 0
        while args.count == 0 or minted < args.count:
            w3, acct, c = w3_contract(args.rpc, args.key)
            cfg = c.functions.getConfig().call()
            if not cfg[0]:
                raise SystemExit("Mint is closed on-chain")
            if c.functions.mintNonce(acct.address).call() >= 5:
                raise SystemExit("Wallet mint limit reached; use another burner wallet")
            challenge = c.functions.challengeFor(acct.address).call()
            difficulty = int(cfg[6])
            print_status(args.rpc, args.key)
            print(f"JOB challenge=0x{challenge.hex()} difficulty={difficulty}")
            nonce, digest, avg = mine_proof(challenge, difficulty, pick_devices(args.devices), args.local_size, args.global_size, args.iter)
            print(f"FOUND nonce=0x{nonce.hex()} digest=0x{digest.hex()} avg={avg:.2f} MH/s")
            if args.dry_run:
                return
            submit_mint(args.rpc, args.key, nonce, args.gas_gwei, args.max_fee_gwei)
            minted += 1


if __name__ == "__main__":
    main()
