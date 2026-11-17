const { expect } = require("chai");
const { ethers } = require("hardhat");
const P = [ethers.parseEther("0.0015"), ethers.parseEther("0.0038"), ethers.parseEther("0.0048")];
const F = [ethers.parseEther("0.0026"), ethers.parseEther("0.0065"), ethers.parseEther("0.0082")];

describe("SliceMarketV2 (metered)", () => {
  async function deploy() {
    const [funder, operator, agent, ue] = await ethers.getSigners();
    const m = await (await ethers.getContractFactory("SliceMarketV2")).deploy(operator.address, agent.address, P, F);
    return { m, operator, agent, ue };
  }
  it("escrows the credit, debits one tenth per minute to the operator and refunds the rest", async () => {
    const { m, operator, agent, ue } = await deploy();
    await m.connect(ue).purchase(3, ethers.ZeroHash, false, { value: P[2] });
    const op0 = await ethers.provider.getBalance(operator.address);
    for (let i = 1; i <= 3; i++) await expect(m.connect(agent).charge(1)).to.emit(m, "Charged").withArgs(1n, i, P[2] / 10n, P[2] - P[2] / 10n * BigInt(i));
    expect(await ethers.provider.getBalance(operator.address) - op0).to.equal(P[2] * 3n / 10n);
    const ue0 = await ethers.provider.getBalance(ue.address);
    await expect(m.connect(agent).close(1)).to.emit(m, "Closed").withArgs(1n, P[2] * 3n / 10n, P[2] - P[2] * 3n / 10n);
    expect(await ethers.provider.getBalance(ue.address) - ue0).to.equal(P[2] - P[2] * 3n / 10n);
  });
  it("charges the whole credit in ten minutes and refuses an eleventh", async () => {
    const { m, agent, ue } = await deploy();
    await m.connect(ue).purchase(2, ethers.ZeroHash, true, { value: P[1] + F[1] });
    for (let i = 0; i < 10; i++) await m.connect(agent).charge(1);
    expect((await m.credits(1)).remaining).to.equal(0n);
    await expect(m.connect(agent).charge(1)).to.be.revertedWith("credit");
  });
  it("only the CHF agent charges; the UE recovers its credit only after expiry", async () => {
    const { m, ue } = await deploy();
    await m.connect(ue).purchase(1, ethers.ZeroHash, false, { value: P[0] });
    await expect(m.connect(ue).charge(1)).to.be.revertedWith("agent");
    await expect(m.connect(ue).close(1)).to.be.revertedWith("auth");
    await ethers.provider.send("evm_increaseTime", [1300]); await ethers.provider.send("evm_mine", []);
    await expect(m.connect(ue).close(1)).to.emit(m, "Closed").withArgs(1n, 0n, P[0]);
  });
});
