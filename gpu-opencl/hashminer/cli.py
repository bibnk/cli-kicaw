"""Command-line interface: `hashminer devices | selftest | bench | run`."""

from __future__ import annotations

import logging
import sys

import click

from . import __version__


def _force_utf8_io() -> None:
    # Windows consoles often use a legacy code page (cp1251/cp1252/...) that can't encode the
    # characters in our output; reconfigure so we never crash on a stray non-ASCII byte.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _setup_logging(level: str) -> None:
    _force_utf8_io()
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="hashminer")
def main() -> None:
    """OpenCL GPU proof-of-work miner for the HASH256 ($HASH) token. See reference/SPEC.md."""
    _force_utf8_io()


# --------------------------------------------------------------------------- devices
@main.command()
def devices() -> None:
    """List the OpenCL platforms / devices visible to PyOpenCL (with their flat indices)."""
    from .gpu import list_devices
    devs = list_devices()
    if not devs:
        click.echo("No OpenCL devices found. Install GPU drivers + an OpenCL ICD.")
        raise SystemExit(1)
    try:
        from rich.console import Console
        from rich.table import Table
        t = Table(title="OpenCL devices")
        for col in ("idx", "name", "type", "CUs", "mem (MB)", "max WG", "platform"):
            t.add_column(col)
        for d in devs:
            t.add_row(str(d.index), d.name, d.type, str(d.compute_units), str(d.global_mem_mb),
                      str(d.max_work_group_size), d.platform_name)
        Console().print(t)
    except Exception:
        for d in devs:
            click.echo(d.describe())


# --------------------------------------------------------------------------- selftest
@main.command()
@click.option("--device", "device_idx", type=int, default=None, help="OpenCL device index (default: first GPU).")
def selftest(device_idx: int | None) -> None:
    """Verify the OpenCL Keccak-256 kernel against eth_utils.keccak (KATs + a roundtrip search)."""
    _setup_logging("WARNING")
    from eth_utils import keccak
    from .constants import compute_challenge, is_valid_nonce
    from .gpu import GpuWorker, list_devices, select_devices

    # 1) pure-Python sanity: the canonical empty-string vector.
    want = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak(b"").hex() == want, "eth_utils.keccak broken?!"
    click.echo(f"keccak256(b'') == {want}  OK")

    devs = list_devices()
    if not devs:
        click.echo("no OpenCL device - skipping GPU checks", err=True)
        raise SystemExit(1)
    info = next(d for d in devs if d.index == device_idx) if device_idx is not None else select_devices("all")[0]
    click.echo(f"device: {info.describe()}")
    w = GpuWorker(info)

    # 2) roundtrip: easy target, scan a window, every GPU hit must verify on CPU,
    #    and the GPU hit set must equal the brute-forced CPU hit set.
    challenge = compute_challenge(1, "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc",
                                  "0x000000000000000000000000000000000000dEaD", 424242)
    target = 2 ** 240  # ~1 in 65536
    N = 1 << 20
    found, n, dt = w.search_batch(challenge, target, n=N, nonce_base=0)
    for nz in found:
        assert is_valid_nonce(challenge, nz, target), f"GPU false positive: nonce={nz}"
    cpu = {nz for nz in range(N) if is_valid_nonce(challenge, nz, target)}
    gpu = set(found)
    assert gpu == cpu, f"GPU/CPU mismatch: only-gpu={sorted(gpu - cpu)[:5]} only-cpu={sorted(cpu - gpu)[:5]}"
    click.echo(f"roundtrip over 2^20 nonces: {len(gpu)} solutions, GPU == CPU  OK  ({n/dt/1e6:.0f} MH/s)")

    # 3) a few exact-digest checks: kernel preimage layout vs abi.encode(challenge, nonce).
    target_all = (1 << 256) - 1  # everything qualifies; check the digests it reports
    f2, _, _ = w.search_batch(challenge, target_all, n=4, nonce_base=0)
    assert set(f2) == {0, 1, 2, 3}, f"expected nonces 0..3 to all qualify, got {sorted(f2)}"
    click.echo("preimage layout matches abi.encode(bytes32 challenge, uint256 nonce)  OK")
    click.echo("SELFTEST PASSED")


# --------------------------------------------------------------------------- bench
@main.command()
@click.option("--device", "device_spec", default="all", help='"all" (default), or a comma list of indices, e.g. "0,2".')
@click.option("--seconds", type=float, default=5.0, show_default=True, help="Benchmark duration per device.")
def bench(device_spec: str, seconds: float) -> None:
    """Measure Keccak-256 search hashrate for each selected device (and the total)."""
    _setup_logging("WARNING")
    from .gpu import GpuWorker, select_devices
    spec = "all" if device_spec.strip().lower() == "all" else [int(x) for x in device_spec.replace(" ", "").split(",") if x]
    devs = select_devices(spec)
    total = 0.0
    for d in devs:
        w = GpuWorker(d)
        mhps = w.benchmark(seconds)
        total += mhps
        click.echo(f"[{d.index}] {d.name}: {mhps:,.0f} MH/s  (batch 2^{w.batch_size.bit_length()-1})")
    if len(devs) > 1:
        click.echo(f"TOTAL: {total:,.0f} MH/s")
    # context: initial on-chain difficulty needs the top 32 bits of the digest == 0 (1 in 2^32).
    if total > 0:
        click.echo(f"~ {2**32 / (total*1e6):.2f} s per solution at the initial difficulty (2^224-1).")


# --------------------------------------------------------------------------- run
@main.command()
@click.option("--config", "config_path", type=click.Path(dir_okay=False), default=None,
              help="Path to miner.toml (default: ./miner.toml if present).")
@click.option("--dry-run/--no-dry-run", default=None, help="Build & sign txs but never broadcast.")
@click.option("--rpc", "rpc_url", default=None, help="Override the RPC URL.")
@click.option("--devices", "device_spec", default=None, help='Override GPU devices: "all" or a comma list.')
@click.option("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR.")
@click.option("--bundle/--no-bundle", "bundle", default=None,
              help="Toggle eth_sendBundle mode to MEV builders (overrides [bundle].enabled in miner.toml).")
@click.option("--bundle-size", type=int, default=None,
              help="Txs per bundle (default 10; per-block cap is 10).")
@click.option("--bundle-priority-gwei", type=float, default=None,
              help="Priority fee for bundled txs (overrides [gas].priority_gwei when bundling).")
def run(config_path, dry_run, rpc_url, device_spec, log_level, bundle, bundle_size, bundle_priority_gwei) -> None:
    """Start mining: poll the contract, brute-force nonces on the GPU, submit mine() txs."""
    from .config import Config
    from .miner import Miner

    cfg = Config.load(config_path)
    if rpc_url:
        cfg.rpc_url = rpc_url
    if device_spec:
        cfg.gpu_devices = "all" if device_spec.strip().lower() == "all" else [int(x) for x in device_spec.replace(" ", "").split(",") if x]
    if dry_run is not None:
        cfg.dry_run = dry_run
    if log_level:
        cfg.log_level = log_level
    if bundle is not None:
        cfg.bundle.enabled = bundle
    if bundle_size is not None:
        cfg.bundle.size = bundle_size
    if bundle_priority_gwei is not None:
        cfg.bundle.priority_gwei = bundle_priority_gwei
    _setup_logging(cfg.log_level)

    if cfg.resolved_account() is None and not cfg.dry_run and cfg.miner_address is None:
        raise SystemExit("No private key found (HASH256_PRIVATE_KEY env or wallet.key_file) and no wallet.miner_address.\n"
                         "  - to mine for real: set HASH256_PRIVATE_KEY (a dedicated burner key with some ETH for gas)\n"
                         "  - to test the pipeline now: add --dry-run\n"
                         "  - for read-only stats: set wallet.miner_address in miner.toml")
    Miner(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
