const { expect } = require("chai");
const { ethers } = require("hardhat");

const P = [ethers.parseEther("0.0015"), ethers.parseEther("0.0038"), ethers.parseEther("0.0048")];
const F = [ethers.parseEther("0.0005"), ethers.parseEther("0.0010"), ethers.parseEther("0.0012")];

describe("SliceMarket", () => {
  async function deploy() {
    const [funder, operator, ue] = await ethers.getSigners();
    const m = await (await ethers.getContractFactory("SliceMarket")).deploy(operator.address, P, F);
    return { m, funder, operator, ue };
  }

  it("moves the exact price from the UE to the operator and emits the purchase", async () => {
    const { m, operator, ue } = await deploy();
    const sub = ethers.keccak256(ethers.toUtf8Bytes("imsi-001010123456789"));
    const before = await ethers.provider.getBalance(operator.address);
    await expect(m.connect(ue).purchase(3, sub, false, { value: P[2] }))
      .to.emit(m, "SlicePurchased").withArgs(1n, ue.address, sub, 3, false, P[2], 600);
    expect(await ethers.provider.getBalance(operator.address) - before).to.equal(P[2]);
  });

  it("charges the adaptation surcharge only when asked", async () => {
    const { m, ue } = await deploy();
    expect(await m.quote(2, true)).to.equal(P[1] + F[1]);
    await expect(m.connect(ue).purchase(2, ethers.ZeroHash, true, { value: P[1] })).to.be.revertedWith("amount");
  });

  it("rejects wrong amounts, unknown slices and price changes by others", async () => {
    const { m, ue } = await deploy();
    await expect(m.connect(ue).purchase(1, ethers.ZeroHash, false, { value: P[0] - 1n })).to.be.revertedWith("amount");
    await expect(m.quote(4, false)).to.be.revertedWith("slice");
    await expect(m.connect(ue).setPrice(1, 1, 1)).to.be.revertedWith("owner");
  });
});
