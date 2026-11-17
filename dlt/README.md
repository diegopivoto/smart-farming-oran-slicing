# dlt/: slice market on Ethereum (branch dlt-slice-market)

| Path | Content |
|---|---|
| `contracts/SliceMarket.sol` | v1, prepaid: the price goes to the operator at purchase |
| `contracts/SliceMarketV2.sol` | v2, metered: credit, `charge` (CHF, 1/10 per minute), `close` (refund), buyer recovery after expiry |
| `test/` | Hardhat tests (`npx hardhat test`, 6 passing) |
| `chf/chf.js` | CHF network function: NRF registration (nfType CHF, nchf-convergedcharging v3), Nchf create/update/release over h2c, chain watcher, per-minute charging, CDRs |
| `scripts/new_wallets.js` | creates the test wallets in `~/.config/smartfarm/dlt-wallets.json` (mode 600) |
| `scripts/deploy.js`, `scripts/deploy-v2.js` | deploy v1 / v2 and write `~/.config/smartfarm/dlt-config.json` |
| `scripts/purchase.js` | scripted purchase with timings (`experiments/chf/purchases.jsonl`) |
| `scripts/fund.js` | top up the Smart Farm Pay wallet from the funder (`--app 0.05`), balances (`--balance`) |
| `scripts/negative_tests.js` | E4 negative tests |

Deployed on Sepolia: SliceMarket `0x359A0559c2791d3Ea49d81FBd744Be79d2D968f8`, SliceMarketV2
`0x2767cCC84EBe3093b62fFBd4dC1421945cd9f99b`, operator `0xEDf5a37D7A12Fd466eEde3019C92D60077FDbaE5`.
Keys are TEST keys and never leave `~/.config/smartfarm/`. Overview and setup: `../README.md`.

Measurements and figures: `../deployment/dlt/` (gate, `e1.sh` RAN default with `RB=33|132`, `ran_fixed.sh`, `e2.sh`,
`e3.sh`), `../deployment/scripts/dlt_report.py` (figures, fixed-tariff comparison, `docs/dlt/figures/summary.json`, created on first run).
Gas used on Sepolia: purchase 263 062, charge 56 238, close 54 623 (about 0.88 M gas per 10-min session).
