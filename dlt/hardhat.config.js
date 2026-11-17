require("@nomicfoundation/hardhat-ethers");
require("@nomicfoundation/hardhat-chai-matchers");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Test keys live outside git (~/.config/smartfarm/dlt-wallets.json, mode 600)
const wallets = (() => {
  try { return JSON.parse(fs.readFileSync(path.join(os.homedir(), ".config/smartfarm/dlt-wallets.json"))); }
  catch { return null; }
})();

module.exports = {
  solidity: { version: "0.8.24", settings: { optimizer: { enabled: true, runs: 200 } } },
  networks: {
    sepolia: {
      url: process.env.SEPOLIA_RPC || "https://ethereum-sepolia-rpc.publicnode.com",
      accounts: wallets ? [wallets.funder.privateKey] : [],
      chainId: 11155111,
    },
  },
};
