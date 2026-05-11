"""Configuration: ``miner.toml`` (stdlib ``tomllib``) + environment overrides.

Private keys are *never* read from the TOML file - only from a key file path or an
environment variable. A config with no key is still valid (read-only / ``--dry-run``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

from .constants import HASH_CONTRACT_ADDRESS, MAINNET_CHAIN_ID

# Public mainnet RPCs (no API key). Rate-limited & higher-latency - fine for dev / dry-run /
# casual mining; set your own node or a paid provider in miner.toml for serious mining.
DEFAULT_PUBLIC_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://eth.drpc.org",
    "https://cloudflare-eth.com",
]

_ENV_KEY = "HASH256_PRIVATE_KEY"


@dataclass
class GasConfig:
    priority_gwei: float = 6.0            # maxPriorityFeePerGas; aggressive default for competitive mining
    max_fee_gwei: float | None = 50.0     # hard cap on maxFeePerGas; skip a submission if the
                                          # needed maxFee (baseFee*base_fee_mult + priority) exceeds it
    base_fee_multiplier: float = 3.0      # headroom over the current baseFee (3x lets retargets not strand a tx)
    gas_limit: int | None = None          # override; None => estimate * gas_limit_multiplier
                                          # set this (e.g. 250000) to BYPASS estimate_gas entirely:
                                          # submit every found nonce, accept some on-chain BlockCapReached reverts
                                          # in exchange for never skipping a nonce that would have landed.
    gas_limit_multiplier: float = 1.30


@dataclass
class ProfitabilityConfig:
    # Simple gate: skip submitting while baseFee exceeds this (None => never skip on price).
    # True profit accounting needs a HASH/ETH price, which doesn't exist before the pool is
    # seeded; wire one in here later if you want a HASH-denominated break-even check.
    skip_if_basefee_above_gwei: float | None = None
    max_pending_per_epoch: int = 1000     # MAX_MINTS_PER_BLOCK * EPOCH_BLOCKS = 10 * 100; stop finding past this


@dataclass
class BundleConfig:
    """Submit verified solutions as `eth_sendBundle` payloads to MEV builders.

    A single wallet can land all 10 mints/block this way: pre-sign N txs with consecutive
    tx-nonces and ship them to builders as one atomic unit, so they're included before any
    competing public-mempool mines. The block 25069006 sweep by 0x501d... is exactly this.
    """
    enabled: bool = False
    size: int = 10                        # txs per bundle (the per-block cap is 10)
    target_blocks_ahead: int = 1          # target block N+target on every block tick
    priority_gwei: float | None = None    # override [gas].priority_gwei when bundling (None => reuse it)
    submit_concurrency: int = 4           # parallel POSTs to builder endpoints
    endpoints: list[str] = field(default_factory=lambda: [
        "https://relay.flashbots.net",      # Flashbots (X-Flashbots-Signature; we use an ephemeral key)
        "https://rpc.beaverbuild.org",
        "https://rpc.titanbuilder.xyz",
        "https://rpc.rsync-builder.xyz",
        "https://api.securerpc.com/v1",    # securerpc fronts Flashbots
    ])


@dataclass
class Config:
    # network
    rpc_url: str = field(default_factory=lambda: DEFAULT_PUBLIC_RPCS[0])
    rpc_fallbacks: list[str] = field(default_factory=lambda: list(DEFAULT_PUBLIC_RPCS[1:]))
    ws_url: str | None = None
    chain_id: int = MAINNET_CHAIN_ID
    contract: str = HASH_CONTRACT_ADDRESS
    poll_interval_s: float = 1.0          # block-poll cadence; 1.0 is fine on Alchemy / own node
    request_timeout_s: float = 5.0        # fail fast & let a fallback kick in

    # wallet (key only via key_file or the HASH256_PRIVATE_KEY env var)
    key_file: str | None = None
    miner_address: str | None = None      # if set without a key => read-only/dry-run

    # gpu
    gpu_devices: object = "all"           # "all" | list[int]
    local_size: int | None = 256           # RTX/NVIDIA-friendly default; tune with `hashminer bench --tune-local-size`
    batch_target_ms: float = 250.0         # target kernel launch duration; higher = less host overhead

    # behaviour
    dry_run: bool = False
    confirmations: int = 1
    log_level: str = "INFO"
    submit_concurrency: int = 4           # parallel single-tx submissions when bundle.enabled is false

    gas: GasConfig = field(default_factory=GasConfig)
    profitability: ProfitabilityConfig = field(default_factory=ProfitabilityConfig)
    bundle: BundleConfig = field(default_factory=BundleConfig)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        load_dotenv()  # pull a local .env if present
        cfg = cls()

        # 1) miner.toml (explicit path, else ./miner.toml if it exists)
        toml_path = Path(path) if path else Path("miner.toml")
        if path or toml_path.exists():
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
            cfg = _apply_toml(cfg, data)

        # 2) environment overrides
        cfg = _apply_env(cfg)

        cfg.validate()
        return cfg

    # ---------------------------------------------------------------- helpers
    def rpc_chain(self) -> list[str]:
        seen, out = set(), []
        for u in [self.rpc_url, *self.rpc_fallbacks]:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def load_private_key(self) -> str | None:
        """Hex private key (0x-prefixed) from env or key_file, or None."""
        env = os.environ.get(_ENV_KEY)
        if env:
            return env.strip()
        if self.key_file:
            text = Path(self.key_file).expanduser().read_text(encoding="utf-8").strip()
            return text
        return None

    def resolved_account(self):
        """Returns an `eth_account.Account` LocalAccount, or None if no key is configured."""
        key = self.load_private_key()
        if not key:
            return None
        from eth_account import Account
        return Account.from_key(key)

    def effective_miner_address(self) -> str | None:
        acct = self.resolved_account()
        if acct is not None:
            return acct.address
        return self.miner_address

    def validate(self) -> None:
        if not self.rpc_chain():
            raise ValueError("no RPC URL configured")
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if not (isinstance(self.gpu_devices, str) or
                (isinstance(self.gpu_devices, (list, tuple)) and all(isinstance(i, int) for i in self.gpu_devices))):
            raise ValueError('gpu_devices must be "all" or a list of integer device indices')
        # Whether a key is required depends on dry_run / read-only intent - that's decided by the
        # caller (the CLI gives a friendly message). We don't enforce it here.


# --------------------------------------------------------------------------- internals
def _apply_toml(cfg: Config, data: dict) -> Config:
    net = data.get("network", {})
    wal = data.get("wallet", {})
    g = data.get("gpu", {})
    beh = data.get("behaviour", data.get("behavior", {}))
    gas = data.get("gas", {})
    prof = data.get("profitability", {})
    bun = data.get("bundle", {})

    def pick(d, k, cur):
        return d[k] if k in d else cur

    cfg = replace(
        cfg,
        rpc_url=pick(net, "rpc_url", cfg.rpc_url),
        rpc_fallbacks=pick(net, "rpc_fallbacks", cfg.rpc_fallbacks),
        ws_url=pick(net, "ws_url", cfg.ws_url),
        chain_id=pick(net, "chain_id", cfg.chain_id),
        contract=pick(net, "contract", cfg.contract),
        poll_interval_s=pick(net, "poll_interval_s", cfg.poll_interval_s),
        request_timeout_s=pick(net, "request_timeout_s", cfg.request_timeout_s),
        key_file=pick(wal, "key_file", cfg.key_file),
        miner_address=pick(wal, "miner_address", cfg.miner_address),
        gpu_devices=pick(g, "devices", cfg.gpu_devices),
        local_size=pick(g, "local_size", cfg.local_size),
        batch_target_ms=pick(g, "batch_target_ms", cfg.batch_target_ms),
        dry_run=pick(beh, "dry_run", cfg.dry_run),
        confirmations=pick(beh, "confirmations", cfg.confirmations),
        log_level=pick(beh, "log_level", cfg.log_level),
        submit_concurrency=pick(beh, "submit_concurrency", cfg.submit_concurrency),
    )
    cfg.gas = GasConfig(
        priority_gwei=pick(gas, "priority_gwei", cfg.gas.priority_gwei),
        max_fee_gwei=pick(gas, "max_fee_gwei", cfg.gas.max_fee_gwei),
        base_fee_multiplier=pick(gas, "base_fee_multiplier", cfg.gas.base_fee_multiplier),
        gas_limit=pick(gas, "gas_limit", cfg.gas.gas_limit),
        gas_limit_multiplier=pick(gas, "gas_limit_multiplier", cfg.gas.gas_limit_multiplier),
    )
    cfg.profitability = ProfitabilityConfig(
        skip_if_basefee_above_gwei=pick(prof, "skip_if_basefee_above_gwei", cfg.profitability.skip_if_basefee_above_gwei),
        max_pending_per_epoch=pick(prof, "max_pending_per_epoch", cfg.profitability.max_pending_per_epoch),
    )
    cfg.bundle = BundleConfig(
        enabled=pick(bun, "enabled", cfg.bundle.enabled),
        size=pick(bun, "size", cfg.bundle.size),
        target_blocks_ahead=pick(bun, "target_blocks_ahead", cfg.bundle.target_blocks_ahead),
        priority_gwei=pick(bun, "priority_gwei", cfg.bundle.priority_gwei),
        submit_concurrency=pick(bun, "submit_concurrency", cfg.bundle.submit_concurrency),
        endpoints=pick(bun, "endpoints", cfg.bundle.endpoints),
    )
    if "key_private" in wal or "private_key" in wal:
        raise ValueError("refusing to read a private key from miner.toml - use the HASH256_PRIVATE_KEY env var or wallet.key_file")
    return cfg


def _apply_env(cfg: Config) -> Config:
    e = os.environ.get
    if v := e("HASH256_RPC_URL"):
        cfg.rpc_url = v
    if v := e("HASH256_RPC_FALLBACKS"):
        cfg.rpc_fallbacks = [s.strip() for s in v.split(",") if s.strip()]
    if v := e("HASH256_WS_URL"):
        cfg.ws_url = v
    if v := e("HASH256_CONTRACT"):
        cfg.contract = v
    if v := e("HASH256_CHAIN_ID"):
        cfg.chain_id = int(v)
    if v := e("HASH256_KEY_FILE"):
        cfg.key_file = v
    if v := e("HASH256_MINER_ADDRESS"):
        cfg.miner_address = v
    if v := e("HASH256_GPU_DEVICES"):
        cfg.gpu_devices = "all" if v.strip().lower() == "all" else [int(x) for x in v.replace(" ", "").split(",") if x != ""]
    if v := e("HASH256_GPU_LOCAL_SIZE"):
        cfg.local_size = int(v)
    if v := e("HASH256_GPU_BATCH_TARGET_MS"):
        cfg.batch_target_ms = float(v)
    if v := e("HASH256_DRY_RUN"):
        cfg.dry_run = v.strip().lower() in ("1", "true", "yes", "on")
    if v := e("HASH256_LOG_LEVEL"):
        cfg.log_level = v
    if v := e("HASH256_BUNDLE"):
        cfg.bundle.enabled = v.strip().lower() in ("1", "true", "yes", "on")
    if v := e("HASH256_BUNDLE_SIZE"):
        cfg.bundle.size = int(v)
    if v := e("HASH256_BUNDLE_PRIORITY_GWEI"):
        cfg.bundle.priority_gwei = float(v)
    return cfg
