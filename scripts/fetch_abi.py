#!/usr/bin/env python3
"""Re-fetch the verified Hash contract source + ABI and regenerate hashminer/abi.py.

Sourcify needs no API key:  python scripts/fetch_abi.py
Etherscan fallback:         python scripts/fetch_abi.py --etherscan-key YOUR_KEY
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ADDR = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference"
ABI_PY = ROOT / "hashminer" / "abi.py"

# hook callbacks the miner never calls - kept out of the bundled ABI to keep it lean
DROP = {"afterAddLiquidity", "afterDonate", "afterInitialize", "afterRemoveLiquidity", "afterSwap",
        "beforeAddLiquidity", "beforeDonate", "beforeInitialize", "beforeRemoveLiquidity", "beforeSwap", "poolKey"}

HEADER = '''"""ABI for the Hash ($HASH) contract at %s.

Source: verified on Etherscan / Sourcify (full match). Uniswap-V4 hook callbacks
(beforeSwap/afterSwap/... and poolKey) are intentionally omitted - the miner never
calls them. See reference/Hash.sol for the complete source. Regenerate: scripts/fetch_abi.py
"""

import json

HASH_CONTRACT_ADDRESS = "%s"

HASH_ABI = json.loads(r"""
''' % (ADDR, ADDR)
FOOTER = '\n""")\n'


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "hash256-miner/fetch_abi"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def from_sourcify() -> tuple[dict, dict[str, str]]:
    data = json.loads(_get(f"https://sourcify.dev/server/files/any/1/{ADDR}"))
    files = {f["name"]: f["content"] for f in data["files"]}
    if "metadata.json" not in files:
        raise RuntimeError("sourcify response missing metadata.json")
    abi = json.loads(files["metadata.json"])["output"]["abi"]
    return {"abi": abi}, files


def from_etherscan(key: str) -> tuple[dict, dict[str, str]]:
    raw = json.loads(_get(f"https://api.etherscan.io/v2/api?chainid=1&module=contract&action=getsourcecode&address={ADDR}&apikey={key}"))
    if raw.get("status") != "1":
        raise RuntimeError(f"etherscan: {raw.get('result')}")
    res = raw["result"][0]
    abi = json.loads(res["ABI"])
    files: dict[str, str] = {}
    src = res.get("SourceCode", "")
    if src.startswith("{{"):  # standard-json-input, double-wrapped
        parsed = json.loads(src[1:-1])
        for path, obj in parsed.get("sources", {}).items():
            files[Path(path).name] = obj.get("content", "")
    elif src:
        files["Hash.sol"] = src
    return {"abi": abi}, files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--etherscan-key", default=None)
    args = ap.parse_args()

    if args.etherscan_key:
        meta, files = from_etherscan(args.etherscan_key)
    else:
        try:
            meta, files = from_sourcify()
        except Exception as exc:  # noqa: BLE001
            print(f"sourcify failed ({exc}); pass --etherscan-key to use Etherscan instead", file=sys.stderr)
            return 1

    abi = [e for e in meta["abi"] if not (e.get("type") == "function" and e.get("name") in DROP)]
    ABI_PY.write_text(HEADER + json.dumps(abi, indent=2) + FOOTER, encoding="utf-8")
    print(f"wrote {ABI_PY}  ({len(abi)} entries)")

    REF.mkdir(exist_ok=True)
    for name in ("Hash.sol", "metadata.json", "constructor-args.txt", "creator-tx-hash.txt"):
        if name in files:
            out = REF / ("Hash.metadata.json" if name == "metadata.json" else name)
            out.write_text(files[name], encoding="utf-8")
            print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
