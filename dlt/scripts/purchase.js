// Scripted slice purchase for the measurement runs (the demo purchase is made by the Smart Farm Pay app).
// Pays SliceMarket.purchase(slice, keccak256(phone SUPI), adaptive) from the "cli" test wallet and prints the timings.
// Usage: node scripts/purchase.js <weather|crop|drone> [--adaptive] [--config ~/.config/smartfarm/dlt-config.json]
const { ethers } = require("ethers");
const fs = require("fs"), os = require("os"), path = require("path");
const argv = process.argv.slice(2);
const service = argv[0], adaptive = argv.includes("--adaptive");
const cfgPath = argv.includes("--config") ? argv[argv.indexOf("--config") + 1] : path.join(os.homedir(), ".config/smartfarm/dlt-config.json");
const SLICE = { weather: 1, crop: 2, drone: 3 };
(async () => {
  if (!SLICE[service]) throw new Error("service must be weather, crop or drone");
  const cfg = JSON.parse(fs.readFileSync(cfgPath));
  const w = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".config/smartfarm/dlt-wallets.json")));
  const ids = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".config/smartfarm/ue-identities.json")));
  const supi = ids.oneplus_8t.supi.startsWith("imsi-") ? ids.oneplus_8t.supi : `imsi-${ids.oneplus_8t.supi}`;
  const provider = new ethers.JsonRpcProvider(cfg.rpc);
  const key = cfg.network === "localhost" ? "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a" : w.cli.privateKey;
  const payer = new ethers.Wallet(key, provider);
  const m = new ethers.Contract(cfg.contract, ["function purchase(uint8,bytes32,bool) payable returns (uint256)",
    "function quote(uint8,bool) view returns (uint256)"], payer);
  const value = await m.quote(SLICE[service], adaptive);
  const [ueBefore, opBefore] = await Promise.all([provider.getBalance(payer.address), provider.getBalance(cfg.operator)]);
  const t0 = Date.now();
  const tx = await m.purchase(SLICE[service], ethers.keccak256(ethers.toUtf8Bytes(supi)), adaptive, { value });
  const rc = await tx.wait(1); const tMined = Date.now();
  await tx.wait(cfg.confirmations || 2); const tConf = Date.now();
  const [ueAfter, opAfter] = await Promise.all([provider.getBalance(payer.address), provider.getBalance(cfg.operator)]);
  const out = { service, adaptive, tx: tx.hash, block: rc.blockNumber, priceEth: ethers.formatEther(value),
    gasEth: ethers.formatEther(rc.gasUsed * rc.gasPrice), payer: payer.address, operator: cfg.operator,
    payerBeforeEth: ethers.formatEther(ueBefore), payerAfterEth: ethers.formatEther(ueAfter),
    operatorBeforeEth: ethers.formatEther(opBefore), operatorAfterEth: ethers.formatEther(opAfter),
    minedS: (tMined - t0) / 1000, confirmedS: (tConf - t0) / 1000, at: new Date(t0).toISOString(),
    explorer: cfg.explorer ? `${cfg.explorer}/tx/${tx.hash}` : null };
  console.log(JSON.stringify(out, null, 1));
  const log = path.join(__dirname, "../../experiments/chf/purchases.jsonl");
  fs.mkdirSync(path.dirname(log), { recursive: true }); fs.appendFileSync(log, JSON.stringify(out) + "\n");
})().catch((e) => { console.error(e.shortMessage || e.message); process.exit(1); });
