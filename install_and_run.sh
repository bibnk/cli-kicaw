#!/usr/bin/env bash
set -euo pipefail
if ! command -v node >/dev/null 2>&1; then
  apt update
  apt install -y curl ca-certificates gnupg
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt install -y nodejs
fi
npm install
cp -n .env.example .env || true
echo "Edit .env then run: set -a; source .env; set +a; node hash256_miner_bot.mjs --status"
