# Smart Farming slicing over 3GPP and non-3GPP 5G access with O-RAN and DDQN

Code of the experimental testbed described in the paper *"SLA-Driven Joint Uplink/Downlink Slice Control over 3GPP and
Non-3GPP Access for Smart Farming: An Experimental O-RAN and Double Deep Q-Network Study"* (under review).

Three farm services (weather telemetry, crop imagery and drone video) run on their own 5G network slices across two
radio accesses in sequence: an untrusted non-3GPP (UN3GPP) uplink over a TV white space link of about 8 km, attached to
an Open5GS core through an N3IWF, and an OpenAirInterface (OAI) gNB downlink to a commercial phone. A FlexRIC near-RT
RIC observes both radios over E2, and a Double Deep Q-Network (DDQN) agent jointly sets the uplink MCS and resource
blocks of the UN3GPP base station and the per-slice downlink PRB quota of the gNB, with every command verified by
read-back.

## Repository layout

| Path | Content |
|---|---|
| `src/smartfarm/` | Python package: service catalog and SLA contracts, SLA evaluation, slice orchestrator, E2 control client, calibration, DDQN agent (`rl/`), data inventory |
| `native/flexric_bridge/` | FlexRIC applications in C: the KPM bridge (per-slice E2SM-KPM subscriptions) and the control xApp `sf_control` that acts on both RAN nodes |
| `native/un3gpp_e2_proxy/` | E2 node proxy that exposes the UN3GPP base station MAC to the RIC (experimental E2SM-SF profile) |
| `schemas/` | E2SM-SF v0.1 profile and the JSON schemas of the KPM envelope and run manifests |
| `deployment/scripts/` | Testbed bring-up and shutdown, slice switching, calibration sweep, offline DDQN training (`rl_offline.py`), online agent (`rl_online.py`), reports and figures |
| `deployment/oai/` | Build of the OAI gNB with the E2 agent; `patches/` holds the changes on top of OAI commit `f8f769592a` (per-slice DL PRB quota actuator, E2 control of the quota, KPM per DRB of the subscribed slice, gNB configuration) |
| `deployment/flexric/` | Pinned FlexRIC version (`d7a71285f3`, E2AP v2, KPM v2.03) |
| `deployment/open5gs/` | Open5GS 2.7.5 AMF patch that selects the slice from the requested DNN, and its installer |
| `deployment/pc3/`, `deployment/content/`, `deployment/traffic/` | UE-side software: node agent, measurement traffic generator and the live content generator used by the app |
| `android/` | Source of the Smart Farm Android app (one slice per service, live content, SLA panel and O-RAN control card) |
| `tests/` | Unit and integration tests (`pytest`) |

## Not included

- **UN3GPP radio (PHY and MAC).** The GFDM 2x2 MIMO TV white space transceiver (GNU Radio PHY and its MAC, including
  the run-time uplink MCS/RB actuator) is proprietary to the National Institute of Telecommunications (Inatel) and is
  **not available** in this repository. The software that interfaces with it (the E2 proxy and the helper scripts in
  `deployment/un3gpp/`) is included.
- **N3IWF and non-3GPP UE.** The free5GC-derived N3IWF and N3IWUE adapted to Open5GS are not part of this release.
- **Hardware.** The testbed uses USRP SDRs for both accesses (USRP B210 for the gNB), a commercial 5G phone (OnePlus
  8T) and three computers.

## Dataset and trained model

The measured dataset (calibration cells, online sessions and the trained DDQN models) is attached to the GitHub release
of this repository as `smart-farming-oran-experiments-20261105.tar.zst`, together with the Android app APK. Extract it at
the repository root:

```bash
zstd -dc smart-farming-oran-experiments-20261105.tar.zst | tar -xf -
```

## Quick start (software only)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Offline DDQN training and evaluation on the released calibration data (no radio needed):

```bash
RL=experiments/rl/$(date +%Y%m%d_%H%M%S)-ddqn; mkdir -p "$RL"
RUNS="$PWD/experiments/calibration/20261104_071216-drone-final-4passes $PWD/experiments/calibration/20261104_121141-crop-final-4passes $PWD/experiments/calibration/20261103_123134-weather-4passes"
.venv/bin/python -u deployment/scripts/rl_offline.py --output "$RL" --episodes 3000 --action-features --hidden 64,64 \
  --batch 32 --gamma 0.5 --margin-weight 0.0 --drift 1.0 --runs $RUNS
```

The final model used in the paper is `experiments/rl/20261104_170128-ddqn-phi-g0.5-m0.0-d1.0-final-crop/model-final.npz`.

## Running on a testbed (outline)

1. Open5GS 2.7.5 with the AMF patch (`deployment/open5gs/`) and one S-NSSAI and DNN per service.
2. `deployment/scripts/sf_oran_up.sh`: FlexRIC, KPM bridge, control xApp, traffic collector and app back end; it waits
   for each RAN node.
3. OAI gNB built with `deployment/oai/build_oai_e2.sh` from the patched OAI tree.
4. UN3GPP base station, N3IWF, UN3GPP UE and N3IWUE (not included), then the UE-side software (`deployment/pc3/`).
5. Measurement mode (calibration with `calibration_sweep.py`, online agent with `rl_online.py`) or app mode (the
   Android app, with the radio control set to the RAN defaults or to the frozen DDQN agent).

The scripts contain the addresses and paths of the original testbed and have to be adapted to a new setup.

## Contact

Diego Gabriel Soares Pivoto, National Institute of Telecommunications (Inatel), Brazil (diegopivoto@gea.inatel.br).
