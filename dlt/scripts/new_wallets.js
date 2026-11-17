// Creates the TEST wallets of the slice market once (~/.config/smartfarm/dlt-wallets.json, mode 600, outside git):
// funder (receives faucet ETH, pays deployment and top-ups), operator (receives the slice payments, starts at 0),
// cli (scripted purchases of the measurements) and chf (signs the per-minute charges). Never send real ETH to them.
const { Wallet } = require("ethers");
const fs = require("fs"), os = require("os"), path = require("path");
const f = path.join(os.homedir(), ".config/smartfarm/dlt-wallets.json");
const w = fs.existsSync(f) ? JSON.parse(fs.readFileSync(f)) : { note: "Sepolia TEST keys only - never send real ETH" };
for (const k of ["funder", "operator", "cli", "chf"]) {
  if (!w[k]) { const x = Wallet.createRandom(); w[k] = { address: x.address, privateKey: x.privateKey }; }
}
fs.mkdirSync(path.dirname(f), { recursive: true });
fs.writeFileSync(f, JSON.stringify(w, null, 2), { mode: 0o600 });
for (const k of ["funder", "operator", "cli", "chf"]) console.log(k.padEnd(9), w[k].address);
console.log("-> fund the funder address in the PoW faucet (https://sepolia-faucet.pk910.de), then run scripts/deploy-v2.js");
