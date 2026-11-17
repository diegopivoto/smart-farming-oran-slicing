// Sends SepoliaETH from the funder wallet to the Smart Farm Pay app wallet (or any address).
//   node scripts/fund.js --app 0.05        address = last app wallet seen by the orchestrator (wallet/status requests)
//   node scripts/fund.js 0xABC... 0.05     explicit address (shown in the app: Settings -> Wallet, tap to copy)
//   node scripts/fund.js --balance         balances of funder, operator and the last app wallet
const { ethers } = require("ethers");
const fs = require("fs"), os = require("os"), path = require("path");
const dir = path.join(os.homedir(), ".config/smartfarm");
const cfg = JSON.parse(fs.readFileSync(path.join(dir, "dlt-config.json")));
const w = JSON.parse(fs.readFileSync(path.join(dir, "dlt-wallets.json")));
function lastAppAddress() {
  const logs = path.join(__dirname, "../../experiments/demo");
  const files = fs.existsSync(logs) ? fs.readdirSync(logs, { recursive: true }).filter((f) => String(f).endsWith(".jsonl")) : [];
  let found = null;
  for (const f of files.map((x) => path.join(logs, x)).sort((a, b) => fs.statSync(a).mtimeMs - fs.statSync(b).mtimeMs)) {
    for (const m of fs.readFileSync(f, "utf8").matchAll(/wallet\/status\?address=(0x[0-9a-fA-F]{40})/g)) found = m[1];
  }
  return found;
}
(async () => {
  const p = new ethers.JsonRpcProvider(cfg.rpc);
  const a = process.argv.slice(2);
  const app = lastAppAddress();
  if (a[0] === "--balance") {
    for (const [n, x] of [["funder", w.funder.address], ["operator", cfg.operator], ["app", app]]) if (x) console.log(n.padEnd(9), x, ethers.formatEther(await p.getBalance(x)), "ETH");
    return;
  }
  const to = a[0] === "--app" ? app : a[0];
  if (!to || !ethers.isAddress(to)) throw new Error("no app wallet seen yet: open Settings -> Wallet in the app, or pass the address");
  const value = ethers.parseEther(a[1] || "0.05");
  const tx = await new ethers.Wallet(w.funder.privateKey, p).sendTransaction({ to, value });
  await tx.wait(1);
  console.log(`sent ${ethers.formatEther(value)} ETH to ${to}\ntx ${cfg.explorer}/tx/${tx.hash}\nbalance ${ethers.formatEther(await p.getBalance(to))} ETH`);
})().catch((e) => { console.error(e.shortMessage || e.message); process.exit(1); });
