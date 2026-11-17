// Deploys SliceMarketV2 (metered charging) and rewrites ~/.config/smartfarm/dlt-config.json (v1 kept as dlt-config-v1.json).
const { ethers, network } = require("hardhat");
const fs = require("fs"), os = require("os"), path = require("path");
async function main() {
  const dir = path.join(os.homedir(), ".config/smartfarm");
  const wallets = JSON.parse(fs.readFileSync(path.join(dir, "dlt-wallets.json")));
  if (!wallets.chf) { const w = ethers.Wallet.createRandom(); wallets.chf = { address: w.address, privateKey: w.privateKey };
    fs.writeFileSync(path.join(dir, "dlt-wallets.json"), JSON.stringify(wallets, null, 2), { mode: 0o600 }); }
  const [funder] = await ethers.getSigners();
  const fund = await funder.sendTransaction({ to: wallets.chf.address, value: ethers.parseEther("0.05") }); await fund.wait(1);
  const prices = ["0.0015", "0.0038", "0.0048"].map(ethers.parseEther);
  const fees = ["0.0026", "0.0065", "0.0082"].map(ethers.parseEther);
  const m = await (await ethers.getContractFactory("SliceMarketV2")).deploy(wallets.operator.address, wallets.chf.address, prices, fees);
  const rcpt = await m.deploymentTransaction().wait(2);
  const old = path.join(dir, "dlt-config.json");
  if (fs.existsSync(old) && !fs.existsSync(path.join(dir, "dlt-config-v1.json"))) fs.copyFileSync(old, path.join(dir, "dlt-config-v1.json"));
  const cfg = { network: network.name, chainId: 11155111, rpc: network.config.url, version: 2, contract: await m.getAddress(),
    deployBlock: rcpt.blockNumber, deployTx: rcpt.hash, operator: wallets.operator.address, agent: wallets.chf.address,
    explorer: "https://sepolia.etherscan.io", confirmations: 2, agentFundingTx: fund.hash };
  fs.writeFileSync(old, JSON.stringify(cfg, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(cfg, null, 2));
}
main().catch((e) => { console.error(e); process.exit(1); });
