// Deploys SliceMarket and writes ~/.config/smartfarm/dlt-config.json (read by the CHF and the orchestrator).
// Prices approved for the paper: 0.001 ETH per unit of DDQN radio-resource cost per minute, 10-min session.
const { ethers, network } = require("hardhat");
const fs = require("fs"), os = require("os"), path = require("path");

async function main() {
  const wallets = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".config/smartfarm/dlt-wallets.json")));
  const operator = network.name === "sepolia" ? wallets.operator.address : (await ethers.getSigners())[1].address;
  const prices = ["0.0015", "0.0038", "0.0048"].map(ethers.parseEther);
  const fees = (process.env.ADAPT_FEES || "0,0,0").split(",").map((x) => ethers.parseEther(x));
  const f = await ethers.getContractFactory("SliceMarket");
  const m = await f.deploy(operator, prices, fees);
  const rcpt = await m.deploymentTransaction().wait(network.name === "sepolia" ? 2 : 1);
  const cfg = {
    network: network.name, chainId: Number((await ethers.provider.getNetwork()).chainId),
    rpc: network.name === "sepolia" ? network.config.url : "http://127.0.0.1:8545",
    contract: await m.getAddress(), deployBlock: rcpt.blockNumber, deployTx: rcpt.hash, operator,
    explorer: network.name === "sepolia" ? "https://sepolia.etherscan.io" : null,
    confirmations: network.name === "sepolia" ? 2 : 1,
  };
  const out = path.join(os.homedir(), `.config/smartfarm/dlt-config${network.name === "sepolia" ? "" : "-" + network.name}.json`);
  fs.writeFileSync(out, JSON.stringify(cfg, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(cfg, null, 2), "\n->", out);
}
main().catch((e) => { console.error(e); process.exit(1); });
