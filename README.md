# Pay-before-slice: a 3GPP CHF with an Ethereum slice market (branch `dlt-slice-market`)

Code of the experimental testbed described in the paper *"Pay-Before-Slice: A 3GPP Charging Function with an Ethereum
Slice Market for End-to-End Network Slicing over Converged 3GPP and Non-3GPP 5G Access"* (under review).

This branch extends the Smart Farming O-RAN testbed of the `main` branch with a slice market. Three farm services
(weather telemetry, crop imagery and drone video) run on their own 5G network slices across two radio accesses in
sequence: an untrusted non-3GPP (UN3GPP) uplink over a TV white space link of about 8 km, attached to an Open5GS core
through an N3IWF, and an OpenAirInterface (OAI) gNB downlink to a commercial phone. A FlexRIC near-RT RIC and a Double
Deep Q-Network (DDQN) agent allocate both radios per slice. On top of it:

- a **Charging Function (CHF)** added to Open5GS as a standard network function: it registers in the NRF as
  `nfType CHF` with the `nchf-convergedcharging` v3 service and serves charging-data create, update and release over
  HTTP/2, writing charging data records;
- an **SMF patch** that makes the Open5GS SMF the Charging Trigger Function: it discovers the CHF through the NRF,
  requests a quota before the N4 session and rejects the PDU session when no quota is granted;
- the **SliceMarketV2 smart contract** on Ethereum Sepolia: a purchase is a session credit, the CHF charges one tenth
  of it per minute to the operator and refunds the rest at release, and the buyer can recover an unused credit alone;
- **Smart Farm Pay**, an Android app that holds the farmer's wallet on the phone, buys a slice and follows the
  metered session;
- the **pricing model** (price per minute from the DDQN radio-resource cost, adaptation surcharge, flat-tariff baseline)
  and the measurement and analysis scripts of the paper.

## What this branch adds to `main`

| Path | Content |
|---|---|
| `dlt/contracts/` | `SliceMarket.sol` (v1, prepaid) and `SliceMarketV2.sol` (v2, metered: credit, per-minute `charge`, `close` with refund, buyer recovery) |
| `dlt/test/` | Hardhat tests of both contracts |
| `dlt/chf/chf.js` | CHF network function: NRF registration and heartbeat, Nchf create/update/release over h2c, chain watcher, grant rules, per-minute charging, CDRs and a status endpoint |
| `dlt/scripts/` | Wallet creation, deployment, scripted purchases with timings, wallet funding and the negative (enforcement) tests |
| `deployment/open5gs/smf-chf/` | Open5GS SMF patches (`Nchf_ConvergedCharging` consumer, about 260 lines) on top of Open5GS v2.7.5 and the AMF patch of `deployment/open5gs/` |
| `deployment/chf/` | Installer of the patched SMF binary (keeps the original for `--revert`) |
| `deployment/dlt/` | Experiment drivers: gate, RAN defaults with 33 or 132 uplink RBs (`e1.sh`, `ran_fixed.sh`), purchased slices (`e2.sh`), degraded position with online adaptation (`e3.sh`), ledger timing (`e3b_chain.sh`), gNB channel logger |
| `deployment/scripts/dlt_report.py` | Results, flat-tariff comparison and figures of the paper (writes `docs/dlt/figures/`) |
| `deployment/scripts/dlt_evidence.py` | Text and image evidence of the NRF profile, Nchf exchanges and SMF log (subscriber identities masked) |
| `src/smartfarm/orchestrator/wallet.py` | Wallet gateway of the app on the operator network: price quote and subscriber hash, JSON-RPC relay to the Ethereum node (the app signs locally) and the CHF status view |
| `android/pay/` | Source of Smart Farm Pay (wallet, slice purchase, live session and refund) |

The rest of the tree (O-RAN applications, E2 proxy, DDQN agent, orchestration, OAI patches, Smart Farm demo app) is the
same as in `main`; see the README of `main` for its layout.

## Not included

- **UN3GPP radio (PHY and MAC).** The GFDM 2x2 MIMO TV white space transceiver (GNU Radio PHY and its MAC, including
  the run-time uplink MCS/RB actuator) is proprietary to the National Institute of Telecommunications (Inatel) and is
  **not available** in this repository. The software that interfaces with it (the E2 proxy and the helper scripts in
  `deployment/un3gpp/`) is included.
- **N3IWF and non-3GPP UE.** The free5GC-derived N3IWF and N3IWUE adapted to Open5GS are not part of this release.
- **Keys.** The CHF agent, operator and funder keys are test keys kept outside the repository
  (`~/.config/smartfarm/dlt-wallets.json`, created by `dlt/scripts/new_wallets.js`). The only key in the code is the
  well-known Hardhat development account used for a local chain.
- **Hardware.** USRP SDRs for both accesses (USRP B210 for the gNB), a commercial 5G phone (OnePlus 8T) and three
  computers.

## Public ledger

All prices, purchases, charges and refunds of the paper are public on the Ethereum Sepolia test network:
SliceMarketV2 `0x2767cCC84EBe3093b62fFBd4dC1421945cd9f99b`, operator account
`0xEDf5a37D7A12Fd466eEde3019C92D60077FDbaE5` (see any Sepolia block explorer). Subscriber identities appear on the chain
only as hashes.

## Dataset and app

The measured data of the paper (`experiments/dlt` and `experiments/chf`: run directories, CHF state, CDRs and logs, with
subscriber identities masked) and the Smart Farm Pay APK are attached to the GitHub release `dlt-v1.0` of this
repository. Extract the data at the repository root and regenerate the results and figures:

```bash
zstd -dc dlt-slice-market-experiments-20261115.tar.zst | tar -xf -
.venv/bin/python deployment/scripts/dlt_report.py
```

## Quick start (software only)

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q   # Python package
cd dlt && npm ci && npx hardhat test                                                     # smart contracts
```

## Running on a testbed (outline)

1. The O-RAN testbed of `main` (Open5GS 2.7.5 with the AMF patch, FlexRIC, OAI gNB, UN3GPP access).
2. Build Open5GS v2.7.5 with `deployment/open5gs/0001-amf-select-slice-by-dnn.patch` and the patches of
   `deployment/open5gs/smf-chf/`, and install the SMF with `deployment/chf/install_smf_chf.sh`
   (`SMF_BUILD=<path to open5gs-smfd>`).
3. Create the wallets, fund them from a Sepolia faucet and deploy SliceMarketV2 (`dlt/scripts/new_wallets.js`,
   `dlt/scripts/deploy-v2.js`); this writes `~/.config/smartfarm/dlt-config.json`.
4. Start the CHF (`cd dlt && node chf/chf.js`; NRF address, bind address and confirmations through the `CHF_*`
   environment variables at the top of the file); it registers in the NRF and the SMF finds it on the next PDU session.
5. Install Smart Farm Pay on the phone, buy a service and open its slice, or run the scripted experiments of
   `deployment/dlt/`.

The scripts contain the addresses and paths of the original testbed and have to be adapted to a new setup.

## Contact

Diego Gabriel Soares Pivoto, National Institute of Telecommunications (Inatel), Brazil (diegopivoto@gea.inatel.br).
