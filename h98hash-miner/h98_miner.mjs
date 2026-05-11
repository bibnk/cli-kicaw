#!/usr/bin/env node
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import { createHash, randomBytes } from 'node:crypto';
import os from 'node:os';
import { ethers } from 'ethers';

const CONTRACT = '0x1E5adF70321CA28b3Ead70Eac545E6055E969e6f';
const DEFAULT_RPC = 'https://ethereum-rpc.publicnode.com';
const ABI = [
  'function challengeFor(address account) view returns (bytes16)',
  'function getConfig() view returns (tuple(bool mintOpen,bool marketOpen,bool listingOpen,bool buyingOpen,bool batchOpen,uint8 marketMode,uint256 difficulty,uint256 mintPrice,uint256 mintAmount,uint256 maxPublicMints,uint256 treasuryReserveMints,uint256 lotSize,uint256 minListingAmount,uint256 maxBatchSize,uint256 marketFeeBps,address feeRecipient))',
  'function getStats() view returns (uint256 publicMinted_,uint256 treasuryReserved_,uint256 totalSupply_,uint256 activeListings_,uint256 difficulty_,bool mintOpen_,bool marketOpen_,bool listingOpen_,bool buyingOpen_,bool batchOpen_,uint8 marketMode_)',
  'function mint(bytes16 nonce) payable returns (uint256 mintIndex)',
  'function mintNonce(address) view returns (uint256)',
  'function balanceOf(address) view returns (uint256)',
  'event Minted(address indexed account,uint256 indexed mintIndex,bytes16 indexed nonce,bytes32 digest,uint256 amount)',
];

function parseArgs(argv) {
  const out = { workers: Math.max(1, Math.min(os.cpus().length, 8)), count: 1, status: false, selftest: false, dryRun: false, gasGwei: null, maxFeeGwei: null, progressMs: 5000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === '--rpc') out.rpc = next();
    else if (a === '--key') out.key = next();
    else if (a === '--workers') out.workers = Number(next());
    else if (a === '--count') out.count = Number(next());
    else if (a === '--status') out.status = true;
    else if (a === '--selftest') out.selftest = true;
    else if (a === '--dry-run') out.dryRun = true;
    else if (a === '--gas-gwei') out.gasGwei = next();
    else if (a === '--max-fee-gwei') out.maxFeeGwei = next();
    else if (a === '--progress-ms') out.progressMs = Number(next());
    else if (a === '--help' || a === '-h') out.help = true;
    else throw new Error(`Unknown arg: ${a}`);
  }
  return out;
}

function usage() {
  return `H98HASH miner bot\n\nEnv:\n  H98_PRIVATE_KEY=0x...        burner wallet private key\n  H98_RPC_URL=https://...      Ethereum mainnet RPC\n\nCommands:\n  node h98_miner.mjs --status\n  node h98_miner.mjs --workers 8 --count 1\n  node h98_miner.mjs --workers 8 --count 5 --gas-gwei 2 --max-fee-gwei 50\n\nOptions:\n  --status                    read contract status only\n  --selftest                  run local low-difficulty mining smoke test\n  --dry-run                   find proof but do not submit tx\n  --rpc URL                   override RPC\n  --key 0x...                 private key, prefer env instead\n  --workers N                 CPU worker threads, default max 8\n  --count N                   mints to attempt, default 1; 0=infinite\n  --gas-gwei N                maxPriorityFeePerGas in gwei\n  --max-fee-gwei N            maxFeePerGas in gwei\n`;
}

function hasLeadingZeroBits(buf, bits) {
  let full = Math.floor(bits / 8);
  let rem = bits % 8;
  for (let i = 0; i < full; i++) if (buf[i] !== 0) return false;
  if (rem) return (buf[full] & (0xff << (8 - rem))) === 0;
  return true;
}

function mineLoop() {
  const { challengeHex, difficulty, workerId, workers } = workerData;
  const challenge = Buffer.from(challengeHex.replace(/^0x/, ''), 'hex');
  let hashes = 0n;
  let last = Date.now();

  // 16-byte nonce layout compatible with the website miner: 4 big-endian uint32 words.
  const nonce = Buffer.allocUnsafe(16);
  nonce.writeUInt32BE(randomBytes(4).readUInt32BE(0), 0);
  nonce.writeUInt32BE(workerId >>> 0, 4);
  let w2 = randomBytes(4).readUInt32BE(0) >>> 0;
  let w3 = 0;

  while (true) {
    nonce.writeUInt32BE(w2 >>> 0, 8);
    nonce.writeUInt32BE(w3 >>> 0, 12);
    const digest = createHash('sha256').update(challenge).update(nonce).digest();
    hashes++;
    if (hasLeadingZeroBits(digest, difficulty)) {
      parentPort.postMessage({ type: 'found', nonce: '0x' + nonce.toString('hex'), digest: '0x' + digest.toString('hex'), hashes: hashes.toString() });
      return;
    }
    w3 = (w3 + workers) >>> 0;
    if (w3 < workers) w2 = (w2 + 1) >>> 0;
    const now = Date.now();
    if (now - last >= 1000) {
      parentPort.postMessage({ type: 'progress', hashes: hashes.toString() });
      hashes = 0n;
      last = now;
    }
  }
}

async function makeContract(opts) {
  const rpc = opts.rpc || process.env.H98_RPC_URL || process.env.ETH_RPC_URL || DEFAULT_RPC;
  const provider = new ethers.JsonRpcProvider(rpc, 1, { staticNetwork: true });
  const key = opts.key || process.env.H98_PRIVATE_KEY;
  const signer = key ? new ethers.Wallet(key, provider) : null;
  return { rpc, provider, signer, contract: new ethers.Contract(CONTRACT, ABI, signer || provider) };
}

async function printStatus(contract, signer, rpc) {
  const [cfg, stats] = await Promise.all([contract.getConfig(), contract.getStats()]);
  console.log(`RPC: ${rpc}`);
  console.log(`Contract: ${CONTRACT}`);
  console.log(`Mint open: ${cfg.mintOpen}`);
  console.log(`Difficulty: ${cfg.difficulty}`);
  console.log(`Mint price: ${ethers.formatEther(cfg.mintPrice)} ETH`);
  console.log(`Mint amount: ${ethers.formatUnits(cfg.mintAmount, 18)} H98`);
  console.log(`Public minted: ${stats.publicMinted_} / ${cfg.maxPublicMints}`);
  if (signer) {
    const addr = await signer.getAddress();
    const [nonce, bal] = await Promise.all([contract.mintNonce(addr), contract.balanceOf(addr)]);
    console.log(`Wallet: ${addr}`);
    console.log(`Wallet mints: ${nonce} / 5`);
    console.log(`Balance: ${ethers.formatUnits(bal, 18)} H98`);
  }
}

function findProof(challengeHex, difficulty, workers, progressMs) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    let totalRate = 0;
    const rates = Array(workers).fill(0);
    let done = false;
    const pool = [];
    const timer = setInterval(() => {
      const mh = totalRate / 1e6;
      const elapsed = ((Date.now() - started) / 1000).toFixed(0);
      console.log(`[mine] ${mh.toFixed(2)} MH/s | elapsed=${elapsed}s | diff=${difficulty}`);
      totalRate = 0;
    }, progressMs);

    for (let i = 0; i < workers; i++) {
      const w = new Worker(new URL(import.meta.url), { workerData: { challengeHex, difficulty, workerId: i + 1, workers } });
      pool.push(w);
      w.on('message', (m) => {
        if (m.type === 'progress') {
          const n = Number(m.hashes);
          rates[i] = n;
          totalRate += n;
        } else if (m.type === 'found' && !done) {
          done = true;
          clearInterval(timer);
          for (const p of pool) if (p !== w) p.terminate();
          resolve(m);
        }
      });
      w.on('error', (e) => { if (!done) { done = true; clearInterval(timer); reject(e); } });
      w.on('exit', (code) => { if (code && !done) { done = true; clearInterval(timer); reject(new Error(`worker exit ${code}`)); } });
    }
  });
}

async function main() {
  if (!isMainThread) return mineLoop();
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) { console.log(usage()); return; }
  if (opts.selftest) {
    const challenge = '0x' + '11'.repeat(16);
    const difficulty = 16;
    const proof = await findProof(challenge, difficulty, Math.max(1, Math.min(opts.workers, 4)), 1000);
    const digest = createHash('sha256').update(Buffer.from(challenge.slice(2), 'hex')).update(Buffer.from(proof.nonce.slice(2), 'hex')).digest();
    if (!hasLeadingZeroBits(digest, difficulty)) throw new Error('selftest failed: proof does not verify');
    console.log(`[selftest] ok nonce=${proof.nonce} digest=0x${digest.toString('hex')}`);
    return;
  }
  const { rpc, signer, contract } = await makeContract(opts);
  if (opts.status) { await printStatus(contract, signer, rpc); return; }
  if (!signer) throw new Error('Set H98_PRIVATE_KEY=0x... for burner wallet, or run --status only.');
  opts.workers = Math.max(1, Math.floor(opts.workers || 1));
  let minted = 0;
  while (opts.count === 0 || minted < opts.count) {
    await printStatus(contract, signer, rpc);
    const addr = await signer.getAddress();
    const [challenge, cfg] = await Promise.all([contract.challengeFor(addr), contract.getConfig()]);
    if (!cfg.mintOpen) throw new Error('Mint is closed on-chain.');
    if ((await contract.mintNonce(addr)) >= 5n) throw new Error('Wallet mint limit reached. Use another burner wallet.');
    const difficulty = Number(cfg.difficulty);
    console.log(`[job] challenge=${challenge} difficulty=${difficulty} workers=${opts.workers}`);
    const proof = await findProof(challenge, difficulty, opts.workers, opts.progressMs);
    console.log(`[found] nonce=${proof.nonce} digest=${proof.digest}`);
    if (opts.dryRun) { console.log('[dry-run] not submitting tx'); return; }
    const overrides = { value: cfg.mintPrice };
    if (opts.gasGwei) overrides.maxPriorityFeePerGas = ethers.parseUnits(String(opts.gasGwei), 'gwei');
    if (opts.maxFeeGwei) overrides.maxFeePerGas = ethers.parseUnits(String(opts.maxFeeGwei), 'gwei');
    const tx = await contract.mint(proof.nonce, overrides);
    console.log(`[tx] sent ${tx.hash}`);
    const rcpt = await tx.wait(1);
    console.log(`[tx] confirmed block=${rcpt.blockNumber} gasUsed=${rcpt.gasUsed}`);
    minted++;
  }
}

main().catch((e) => {
  console.error(e.shortMessage || e.message || e);
  process.exit(1);
});
