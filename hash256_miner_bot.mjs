#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { JsonRpcProvider, Wallet, Contract, formatUnits, parseUnits } from 'ethers';
import init, { initSync, Miner } from './hash_miner.js';

const CONTRACT_ADDRESS = '0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc';
const DEFAULT_RPC = 'https://rpc.mevblocker.io/fast';
const ABI = [
  'function balanceOf(address account) view returns (uint256)',
  'function getChallenge(address miner) view returns (bytes32)',
  'function currentDifficulty() view returns (uint256)',
  'function currentReward() view returns (uint256)',
  'function epochBlocksLeft() view returns (uint256)',
  'function totalMiningMinted() view returns (uint256)',
  'function mine(uint256 nonce)',
];

function arg(name, def = undefined) {
  const idx = process.argv.indexOf(name);
  if (idx >= 0 && idx + 1 < process.argv.length) return process.argv[idx + 1];
  return def;
}
function has(name) { return process.argv.includes(name); }
function hexToBytes(hex) {
  const s = hex.startsWith('0x') ? hex.slice(2) : hex;
  if (s.length !== 64) throw new Error(`expected 32-byte hex, got ${s.length / 2} bytes`);
  return Uint8Array.from(Buffer.from(s, 'hex'));
}
function bytesToHex(bytes) { return '0x' + Buffer.from(bytes).toString('hex'); }
function u256ToBytes(value) {
  let hex = BigInt(value).toString(16);
  if (hex.length > 64) throw new Error('uint256 overflow');
  hex = hex.padStart(64, '0');
  return Uint8Array.from(Buffer.from(hex, 'hex'));
}
function fmtHashrate(hps) {
  if (hps >= 1e6) return `${(hps / 1e6).toFixed(2)} MH/s`;
  if (hps >= 1e3) return `${(hps / 1e3).toFixed(2)} kH/s`;
  return `${hps.toFixed(0)} H/s`;
}
function usage() {
  console.log(`HASH256 miner bot (official WASM CPU engine)\n\nUsage:\n  PRIVATE_KEY=0x... node hash256_miner_bot.mjs [options]\n\nOptions:\n  --rpc URL             Ethereum RPC URL (default: ${DEFAULT_RPC})\n  --count N             Number of successful mine tx to submit (default: 1, 0=infinite)\n  --batch N             Hashes per WASM search batch (default: 250000)\n  --gas-limit N         Fixed gas limit for mine tx (default: estimate, min 200000, max 400000)\n  --max-fee-gwei N      Optional maxFeePerGas in gwei\n  --priority-gwei N     Optional maxPriorityFeePerGas in gwei\n  --dry-run             Mine until nonce found, but do NOT submit tx\n  --status              Only print wallet/mining status\n  --help                Show this help\n\nNotes:\n  - This matches hash256.org/mine worker logic using /miner/hash_miner.js + wasm.\n  - The site states "No GPU"; GPU CUDA/OpenCL engine is not provided by upstream yet.\n`);
}

if (has('--help')) { usage(); process.exit(0); }

const rpc = arg('--rpc', process.env.RPC_URL || DEFAULT_RPC);
const count = Number(arg('--count', '1'));
const batchSize = BigInt(arg('--batch', '250000'));
const dryRun = has('--dry-run');
const statusOnly = has('--status');
const pk = process.env.PRIVATE_KEY;
if (!pk) {
  console.error('ERROR: set PRIVATE_KEY=0x... first (do not paste it into chat).');
  console.error('Run with --help for options.');
  process.exit(2);
}
if (!Number.isFinite(count) || count < 0) throw new Error('--count must be >= 0');

const wasmBytes = readFileSync(new URL('./hash_miner_bg.wasm', import.meta.url));
try { initSync({ module: wasmBytes }); } catch { await init(new URL('./hash_miner_bg.wasm', import.meta.url)); }

const provider = new JsonRpcProvider(rpc, 1);
const wallet = new Wallet(pk, provider);
const contract = new Contract(CONTRACT_ADDRESS, ABI, wallet);
let stopped = false;
process.on('SIGINT', () => { stopped = true; console.log('\nStopping after current batch...'); });

async function printStatus() {
  const [bal, diff, reward, left, minted, challenge] = await Promise.all([
    contract.balanceOf(wallet.address),
    contract.currentDifficulty(),
    contract.currentReward(),
    contract.epochBlocksLeft(),
    contract.totalMiningMinted(),
    contract.getChallenge(wallet.address),
  ]);
  console.log(`wallet: ${wallet.address}`);
  console.log(`balance: ${formatUnits(bal, 18)} HASH`);
  console.log(`reward: ${formatUnits(reward, 18)} HASH`);
  console.log(`difficulty: ${diff.toString()}`);
  console.log(`epochBlocksLeft: ${left.toString()}`);
  console.log(`totalMiningMinted: ${formatUnits(minted, 18)} HASH`);
  console.log(`challenge: ${challenge}`);
}

async function findNonce(challengeHex, difficultyBigInt) {
  const prefix = randomBytes(24);
  const miner = new Miner(hexToBytes(challengeHex), u256ToBytes(difficultyBigInt), prefix);
  let start = 0n;
  let hashes = 0n;
  let last = performance.now();
  const t0 = last;
  try {
    while (!stopped) {
      const hit = miner.search(start, batchSize);
      start += batchSize;
      hashes += batchSize;
      const now = performance.now();
      if (now - last >= 1000) {
        const hps = Number(hashes) / ((now - t0) / 1000);
        process.stdout.write(`\rmining ${hashes.toString()} hashes | ${fmtHashrate(hps)}      `);
        last = now;
      }
      if (hit) {
        process.stdout.write('\n');
        return { nonceHex: bytesToHex(hit.nonce), resultHex: bytesToHex(hit.result), hashes: hit.hashes ?? hashes };
      }
    }
    return null;
  } finally {
    miner.free();
  }
}

async function submitNonce(nonceHex) {
  const nonce = BigInt(nonceHex);
  const overrides = {};
  const fixedGas = arg('--gas-limit');
  if (fixedGas) overrides.gasLimit = BigInt(fixedGas);
  else {
    try {
      let gas = await contract.mine.estimateGas(nonce);
      gas = gas * 3n / 2n;
      if (gas < 200000n) gas = 200000n;
      if (gas > 400000n) gas = 400000n;
      overrides.gasLimit = gas;
    } catch (e) {
      console.warn('gas estimation failed, fallback 250000:', e.shortMessage || e.message);
      overrides.gasLimit = 250000n;
    }
  }
  const maxFee = arg('--max-fee-gwei');
  const priority = arg('--priority-gwei');
  if (maxFee) overrides.maxFeePerGas = parseUnits(maxFee, 'gwei');
  if (priority) overrides.maxPriorityFeePerGas = parseUnits(priority, 'gwei');
  const tx = await contract.mine(nonce, overrides);
  console.log(`tx sent: ${tx.hash}`);
  const rcpt = await tx.wait();
  console.log(`tx ${rcpt.status === 1 ? 'confirmed' : 'failed'} block=${rcpt.blockNumber} gasUsed=${rcpt.gasUsed.toString()}`);
  return rcpt;
}

await printStatus();
if (statusOnly) process.exit(0);

let mined = 0;
while (!stopped && (count === 0 || mined < count)) {
  const [challenge, difficulty] = await Promise.all([
    contract.getChallenge(wallet.address),
    contract.currentDifficulty(),
  ]);
  console.log(`\nround ${mined + 1}${count ? '/' + count : ''}`);
  console.log(`challenge=${challenge}`);
  console.log(`difficulty=${difficulty.toString()}`);
  const hit = await findNonce(challenge, difficulty);
  if (!hit) break;
  console.log(`FOUND nonce=${hit.nonceHex}`);
  console.log(`result=${hit.resultHex}`);
  if (dryRun) {
    console.log('dry-run: not submitting tx');
    mined += 1;
    continue;
  }
  await submitNonce(hit.nonceHex);
  mined += 1;
}
console.log(`done. submitted=${dryRun ? 0 : mined}, found=${mined}`);
