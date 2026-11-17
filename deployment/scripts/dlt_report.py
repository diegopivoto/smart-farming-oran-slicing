#!/usr/bin/env python3
"""Results of the DLT slice market experiments (branch dlt-slice-market): summary.json, summary.md and the figures
used by the papers, the technical report and the slides.

    dlt_report.py [--output docs/dlt/figures]

Inputs (experiments/dlt, newest valid directory of each kind; directories tagged ABORTED, SUPERSEDED, INVALID or
EXCLUDED are ignored):
  E1-<svc>-ran33 / -ran132   no purchase: RAN default = BS link adaptation with 33 or 132 UL RBs, smallest DL quota
                             (10 steps each, 2026-11-15; the 6-step 33-RB runs of 2026-11-14 are kept as day-1 data)
  E2-<svc>-paid-ddqn         paid slice, allocation by the frozen DDQN (reference phone position)
  E3-drone-adaptive-learn    degraded phone position (gNB RSRP about -111 dBm), adaptive purchase, online DDQN
  E1-drone-ran{33,132}-weakpos  the two RAN defaults at the degraded position
Payments and charges: experiments/chf/purchases.jsonl (scripted purchases, timed), the CHF state of the metered
contract and experiments/dlt/chf.log (per-minute charges); E4: experiments/dlt/e4-negative.jsonl; channel:
experiments/dlt/gnb-channel.tsv (timestamped RSRP of the phone reported by the gNB).
Figures are drawn at IEEE column width (3.5 in) or page width (7.16 in) with fonts of at least 8 pt.
"""

import argparse
import json
import re
import statistics as st
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "experiments/dlt"
SKIP = ("ABORTED", "SUPERSEDED", "INVALID", "EXCLUDED")
SVC = ["drone", "crop", "weather"]
NAME = {"drone": "Drone video", "crop": "Crop imagery", "weather": "Weather telemetry"}
KEY = {"drone": ("svc_goodput_p10", "Goodput p10 (Mbit/s)", 1e6, 34.0),
       "crop": ("svc_goodput", "Goodput (Mbit/s)", 1e6, 15.0),
       "weather": ("svc_aoi_p95", "AoI p95 (s)", 1.0, 2.0)}
BLUE, ORANGE, GREEN, GREY, RED, INK = "#2a78d6", "#eb6834", "#1baf7a", "#8a8984", "#c4302f", "#1d1d1b"
PRICE_PER_MIN = {1: 0.00015, 2: 0.00038, 3: 0.00048}

plt.rcParams.update({"font.size": 8, "axes.titlesize": 8.5, "axes.labelsize": 8, "xtick.labelsize": 7.5,
                     "ytick.labelsize": 7.5, "legend.fontsize": 7.5, "font.family": "DejaVu Sans",
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": "#e6e5e0",
                     "grid.linewidth": 0.6, "axes.edgecolor": "#55544f", "savefig.dpi": 300})


def run(pattern: str) -> Path | None:
    c = [p for p in sorted(D.glob(f"*{pattern}")) if p.is_dir() and not any(s in p.name for s in SKIP)]
    return c[-1] if c else None


def steps(p: Path | None) -> list[dict]:
    return [json.loads(x) for x in (p / "steps.jsonl").read_text().splitlines()] if p else []


def med(vals: list) -> float | None:
    v = [x for x in vals if x is not None]
    return st.median(v) if v else None


def stats(rows: list[dict], svc: str, agent: bool) -> dict:
    r = rows[1:] if agent else rows
    k, _, sc, _ = KEY[svc]
    return {"steps": len(r), "sla_met": sum(bool(x.get("sla_met")) for x in r),
            "key_median": (med([x["cell"].get(k) for x in r]) or 0) / sc,
            "cost_mean": st.mean(x["cost"] for x in r) if r else None,
            "ul_snr_db": med([x["cell"].get("un3gpp_ul_snr_db") for x in r]),
            "ul_mac_mbps": med([x["cell"].get("un3gpp_mac_mbps") for x in r]),
            "dl_rlc_delay_ms": med([x["cell"].get("kpm_rlc_delay_dl_ms") for x in r]),
            "dl_pdsch_mcs": med([x["cell"].get("kpm_pdsch_mcs_mean") for x in r]),
            "dl_quota": sorted({x["cell"].get("dl_prb") for x in r}),
            "actions": sorted({x["action"] for x in r}),
            "loss_median": med([x["cell"].get("svc_packet_loss_ratio") for x in r]),
            "t0": r[0]["decided_at"] if r else None, "t1": r[-1]["decided_at"] if r else None}


def rsrp_window(t0: str | None, t1: str | None) -> float | None:
    f = D / "gnb-channel.tsv"
    if not (f.exists() and t0 and t1):
        return None
    a = datetime.fromisoformat(t0).astimezone().replace(tzinfo=None)
    b = datetime.fromisoformat(t1).astimezone().replace(tzinfo=None)
    vals = []
    for ln in f.read_text().splitlines():
        ts, _, rest = ln.partition("\t")
        m = re.search(r"average RSRP (-?\d+)", rest)
        if m and a <= datetime.fromisoformat(ts) <= b:
            vals.append(int(m[1]))
    return med(vals)


def save(fig: plt.Figure, out: Path, name: str) -> None:
    fig.tight_layout(pad=0.3)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def fixed_tariff(S: dict) -> dict:
    """Hypothetical flat tariff (not implemented in the CHF), for comparison with the DLT market price.

    An operator without per-slice control would sell fixed reservation tiers: Basic = RAN default with 33 UL RBs,
    Premium = RAN default with 132 UL RBs (the full band). Each tier gets one flat price for every service, set with the
    same rule as the market (pi x radio cost per minute), from the mean measured cost of the tier over the three
    services at the reference position. The market price of a slice is pi x C_k of the trained DDQN, plus the
    adaptation surcharge for the degraded position. The figure of merit is the price per SLA-compliant minute: the
    10-min price divided by the expected number of minutes that met the SLA (share of steps x 10)."""
    pi = 1e-3
    tiers = {}
    for t in ("ran33", "ran132"):
        tiers[t] = pi * st.mean(S["services"][s][t]["cost_mean"] for s in SVC) * 10
    market = {"weather": 0.0015, "crop": 0.0038, "drone": 0.0048}
    rows = {}
    for sc, d in [(s, S["services"][s]) for s in SVC] + [("drone_weak", S["weak"])]:
        r = {}
        for t in ("ran33", "ran132"):
            v = d[t]
            share = v["sla_met"] / v["steps"]
            r[t] = {"price_10min": tiers[t], "sla_share": share, "cost_mean": v["cost_mean"],
                    "price_per_sla_min": tiers[t] / (10 * share) if share else None}
        p = d["adaptive"] if sc == "drone_weak" else d["paid"]
        price = (0.0048 + 0.0082) if sc == "drone_weak" else market[sc]
        share = p["sla_met"] / p["steps"]
        r["market"] = {"price_10min": price, "sla_share": share, "cost_mean": p["cost_mean"],
                       "price_per_sla_min": price / (10 * share) if share else None}
        rows[sc] = r
    return {"pi_eth": pi, "tier_price_10min": tiers, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=ROOT / "docs/dlt/figures")
    a = ap.parse_args()
    out = a.output
    out.mkdir(parents=True, exist_ok=True)

    from matplotlib.patches import Patch

    R = {s: {"ran33": run(f"E1-{s}-ran33"), "ran132": run(f"E1-{s}-ran132"), "E2": run(f"E2-{s}-paid-ddqn")}
         for s in SVC}
    DAY1 = {s: run(f"20261114_*-E1-{s}-ran33") for s in SVC}       # first-day 6-step baselines (channel variation)
    W = {"E3": run("E3-drone-adaptive-learn"), "ran33": run("E1-drone-ran33-weakpos"),
         "ran132": run("E1-drone-ran132-weakpos")}
    S: dict = {"generated": datetime.now().isoformat(timespec="seconds"), "services": {}, "weak": {}, "day1_ran33": {}}
    for s in SVC:
        d = {}
        for t in ("ran33", "ran132"):
            d[t] = stats(steps(R[s][t]), s, False) | {"dir": R[s][t].name}
            d[t]["dl_rsrp_dbm"] = rsrp_window(d[t]["t0"], d[t]["t1"])
        d["paid"] = stats(steps(R[s]["E2"]), s, True) | {"dir": R[s]["E2"].name}
        d["paid"]["dl_rsrp_dbm"] = rsrp_window(d["paid"]["t0"], d["paid"]["t1"])
        d["no_purchase"] = d["ran33"]
        S["services"][s] = d
        if DAY1[s]:
            S["day1_ran33"][s] = stats(steps(DAY1[s]), s, False) | {"dir": DAY1[s].name}
    S["weak"]["adaptive"] = stats(steps(W["E3"]), "drone", True) | {"dir": W["E3"].name}
    for t in ("ran33", "ran132"):
        S["weak"][t] = stats(steps(W[t]), "drone", False) | {"dir": W[t].name}
    for v in S["weak"].values():
        v["dl_rsrp_dbm"] = rsrp_window(v["t0"], v["t1"])
    S["weak"]["no_purchase"] = S["weak"]["ran33"]

    # payments: scripted purchases (timed) on v1 + v2; per-minute charges and closes from the CHF log
    pur = [json.loads(x) for x in (ROOT / "experiments/chf/purchases.jsonl").read_text().splitlines()]
    log = (D / "chf.log").read_text()
    charges = [(m[1], int(m[2]), float(m[3])) for m in
               re.finditer(r"^(\S+) \[DLT\] charge purchase (\d+) mined block \d+ in ([\d.]+) s", log, re.M)]
    closes = [float(m[1]) for m in re.finditer(r"close purchase \d+ mined block \d+ in ([\d.]+) s", log)]
    seen = {m[2]: datetime.fromisoformat(m[1].replace("Z", "+00:00")) for m in
            re.finditer(r"^(\S+) \[DLT\] purchase (\d+) slice", log, re.M)}
    granted = {m[2]: datetime.fromisoformat(m[1].replace("Z", "+00:00")) for m in
               re.finditer(r"^(\S+) \[CHF\] grant \S+ \S+ purchase (\d+)", log, re.M)}
    state = json.loads(next((ROOT / "experiments/chf").glob("chf-state-2767*.json")).read_text())
    v2 = list(state["purchases"].values())
    app_ids = [p["id"] for p in v2 if p["buyer"].lower().startswith("0x113934")]
    S["payments"] = {"scripted": len(pur), "mined_s_median": st.median(p["minedS"] for p in pur),
                     "confirmed_s_median": st.median(p["confirmedS"] for p in pur),
                     "confirmed_s_p90": sorted(p["confirmedS"] for p in pur)[int(0.9 * (len(pur) - 1))],
                     "confirmed_s_max": max(p["confirmedS"] for p in pur),
                     "gas_eth_max": max(float(p["gasEth"]) for p in pur),
                     "charges": len(charges), "charge_mined_s_median": st.median(c[2] for c in charges),
                     "charge_mined_s_max": max(c[2] for c in charges),
                     "closes": len(closes), "close_mined_s_median": st.median(closes) if closes else None,
                     "v2_purchases": len(v2), "v2_app_purchases": len(app_ids),
                     "app_seen_to_grant_s": [round((granted[str(i)] - seen[str(i)]).total_seconds(), 1)
                                             for i in app_ids if str(i) in seen and str(i) in granted],
                     "refunded_eth_total": sum(int(p["closedOnChain"]["refundedWei"]) for p in v2 if p.get("closedOnChain")) / 1e18,
                     "charged_eth_total": sum(int(c["amountWei"]) for cs in state.get("charges", {}).values() for c in cs) / 1e18}
    neg = D / "e4-negative.jsonl"
    S["E4"] = [json.loads(x) for x in neg.read_text().splitlines()] if neg.exists() else []
    S["smf_denials"] = len(re.findall(r"create imsi-\S+ \S+ -> 403", log))
    S["chf_grants"] = len(re.findall(r"\[CHF\] grant ", log))
    S["fixed_tariff"] = fixed_tariff(S)
    (out / "summary.json").write_text(json.dumps(S, indent=1, default=str) + "\n")

    C33, C132, CP = "#f2a65a", "#b2470f", BLUE
    POL = (("ran33", C33, "RAN default, 33 UL RBs"), ("ran132", C132, "RAN default, 132 UL RBs"),
           ("paid", CP, "Purchased slice (DDQN)"))

    # Fig. 1: SLA met by the two RAN defaults and by the purchased slice (reference + degraded position)
    fig, ax = plt.subplots(figsize=(3.5, 2.25))
    groups = [(NAME[s].split()[0], S["services"][s]) for s in SVC]
    groups.append(("Drone\n(degraded)", {"ran33": S["weak"]["ran33"], "ran132": S["weak"]["ran132"],
                                          "paid": S["weak"]["adaptive"]}))
    for i, (g, d) in enumerate(groups):
        for j, (k, col, _) in enumerate(POL):
            v = d[k]
            pct = 100 * v["sla_met"] / v["steps"]
            x = i + (j - 1) * 0.27
            ax.bar(x, max(pct, 0.8), 0.25, color=col, edgecolor="white", linewidth=0.5)
            ax.text(x, pct + 2, f"{v['sla_met']}/{v['steps']}", ha="center", va="bottom", fontsize=6.3, rotation=90)
    ax.set_xticks(range(len(groups)), [g for g, _ in groups])
    ax.set_ylim(0, 128)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_ylabel("Steps meeting the SLA (%)")
    ax.legend(handles=[Patch(color=c, label=l) for _, c, l in POL], loc="upper center", bbox_to_anchor=(0.5, 1.27),
              ncol=2, frameon=False, handlelength=1.2, columnspacing=0.9, fontsize=7)
    save(fig, out, "fig-sla-purchase")

    # Fig. 2: service metric per 30-s step (page width), 10 steps per policy
    fig, axs = plt.subplots(1, 3, figsize=(7.16, 2.0))
    for ax, s in zip(axs, SVC):
        k, lab, sc, thr = KEY[s]
        for kind, col, lab2, mk in (("ran33", C33, "RAN default, 33 RBs", "s"), ("ran132", C132, "RAN default, 132 RBs", "D"),
                                    ("E2", CP, "purchased slice (DDQN)", "o")):
            r = steps(R[s][kind])
            r = r[1:] if kind == "E2" else r
            ax.plot(range(1, len(r) + 1), [(x["cell"].get(k) or float("nan")) / sc for x in r], marker=mk, color=col,
                    ms=3, lw=1.2, label=lab2)
        ax.axhline(thr, color=RED, ls="--", lw=0.9)
        ax.set_title(NAME[s])
        ax.set_xlabel("Step (30 s of traffic)")
        ax.set_ylabel(lab)
        ax.set_xticks(range(1, 11))
        if s == "weather":
            ax.set_ylim(0, 2.3)
            ax.text(0.8, thr - 0.18, "SLA target (max.)", color=RED, fontsize=7)
        elif s == "drone":
            ax.set_ylim(0, 44)
        else:
            ax.set_ylim(10, 22)
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.09), fontsize=7)
    save(fig, out, "fig-metric-per-step")

    # Fig. 3: degraded position (drone): online DDQN vs the two RAN defaults
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(3.5, 3.1), sharex=True, gridspec_kw={"height_ratios": [1.2, 1]})
    e3, w33, w132 = steps(W["E3"])[1:], steps(W["ran33"]), steps(W["ran132"])
    for r, col, mk, lab in ((w33, C33, "s", "RAN default, 33 RBs"), (w132, C132, "D", "RAN default, 132 RBs"),
                            (e3, CP, "o", "Purchased adaptive slice (online DDQN)")):
        a1.plot(range(1, len(r) + 1), [x["cell"]["svc_goodput_p10"] / 1e6 for x in r], marker=mk, color=col, ms=3,
                lw=1.2, label=lab)
    a1.axhline(34, color=RED, ls="--", lw=0.9)
    a1.set_ylabel("Goodput p10\n(Mbit/s)")
    a1.set_ylim(0, 44)
    a1.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.42), ncol=2, fontsize=6.5, columnspacing=0.8)
    for r, col, mk in ((w132, C132, "D"), (e3, CP, "o")):
        a2.semilogy(range(1, len(r) + 1), [max(x["cell"].get("kpm_rlc_delay_dl_ms") or 1, 1) for x in r], marker=mk,
                    color=col, ms=3, lw=1)
    a2.set_ylabel("DL RLC delay (ms)")
    a2b = a2.twinx()
    a2b.step(range(1, len(e3) + 1), [x["cell"]["dl_prb"] for x in e3], where="mid", color=GREEN, lw=1.4)
    a2b.step(range(1, len(w132) + 1), [x["cell"]["dl_prb"] for x in w132], where="mid", color=GREY, lw=1.2, ls=":")
    a2b.set_ylabel("DL PRB quota", color=GREEN)
    a2b.set_ylim(40, 115)
    a2b.set_yticks([52, 78, 106])
    a2b.spines["right"].set_visible(True)
    a2b.grid(False)
    a2b.text(len(e3) + 0.3, 104, "DDQN quota", color=GREEN, fontsize=6.5, ha="right", va="top")
    a2b.text(10.3, 55, "RAN default quota", color=GREY, fontsize=6.5, ha="right", va="bottom")
    a2.set_xlabel("Step (30 s of traffic)")
    a2.set_xticks([1, 4, 7, 10, 13, 16])
    save(fig, out, "fig-weak-position")

    # Fig. 4: on-chain latency of purchases, per-minute charges and closes
    fig, ax = plt.subplots(figsize=(3.5, 1.9))
    data = [[p["minedS"] for p in pur], [p["confirmedS"] for p in pur], [c[2] for c in charges], closes]
    labs = ["Purchase\nmined", "Purchase\nconfirmed", "Charge\nmined", "Close +\nrefund"]
    bp = ax.boxplot(data, tick_labels=labs, widths=0.5, patch_artist=True, showfliers=True,
                    flierprops={"markersize": 3, "markeredgecolor": GREY}, medianprops={"color": INK})
    for b, col in zip(bp["boxes"], (BLUE, BLUE, GREEN, ORANGE)):
        b.set_facecolor(col)
        b.set_alpha(0.55)
    ax.set_ylabel("Time after submission (s)")
    for i, d in enumerate(data, 1):
        ax.text(i, max(d) + 1.2, f"n={len(d)}", ha="center", fontsize=7)
    ax.set_ylim(0, max(max(d) for d in data) + 6)
    save(fig, out, "fig-chain-latency")

    # Fig. 5: the metered sessions paid from the app on the last demonstration (credit draining, operator income)
    app = sorted([p for p in v2 if p["id"] in app_ids and p.get("closedOnChain")], key=lambda p: int(p["id"]))[-3:]
    fig, ax = plt.subplots(figsize=(3.5, 1.95))
    for p, col in zip(app, (BLUE, GREEN, ORANGE)):
        cs = sorted(state["charges"].get(p["id"], []), key=lambda c: c["minute"])
        amt = int(p["amountWei"]) / 1e18
        xs, credit = [0], [amt]
        for c in cs:
            xs.append(c["minute"] - 1)
            credit.append(int(c["remainingWei"]) / 1e18)
        xs.append(len(cs) + 1.5)
        credit.append(credit[-1])
        lab = {1: "Weather", 2: "Crop", 3: "Drone"}[p["sliceId"]]
        used, ref = int(p["closedOnChain"]["usedWei"]) / 1e18, int(p["closedOnChain"]["refundedWei"]) / 1e18
        ax.step(xs, [1e3 * v for v in credit], where="post", color=col, lw=1.5,
                label=f"{lab}: paid {1e3 * amt:g}, used {1e3 * used:.2f}, refunded {1e3 * ref:.2f} mETH")
        ax.plot([xs[-1]], [1e3 * credit[-1]], marker="v", color=col, ms=4)
    ax.set_xlabel("Session minute (one charge at the start of each minute)")
    ax.set_ylabel("Session credit (mETH)")
    ax.legend(frameon=False, fontsize=6.3, loc="upper right", bbox_to_anchor=(1.0, 1.32))
    ax.set_ylim(0, 5.3)
    ax.text(4.55, 0.25, "▼ release + 90-s grace,\nthen close and refund", fontsize=6.3, ha="right", color=INK)
    save(fig, out, "fig-metered-session")

    # Fig. 6: fixed tariff (hypothetical flat tiers) vs DLT market price
    ft = S["fixed_tariff"]
    fig, (b1, b2) = plt.subplots(1, 2, figsize=(7.16, 2.1), gridspec_kw={"width_ratios": [1, 1]})
    scen = [("weather", "Weather"), ("crop", "Crop"), ("drone", "Drone"), ("drone_weak", "Drone\n(degraded)")]
    POL2 = (("ran33", C33, "Flat tier Basic (33 RBs)"), ("ran132", C132, "Flat tier Premium (132 RBs)"),
            ("market", CP, "DLT market price (DDQN cost)"))
    for i, (sc, lab) in enumerate(scen):
        for j, (k, col, _) in enumerate(POL2):
            v = ft["rows"][sc][k]
            x = i + (j - 1) * 0.27
            b1.bar(x, 1e3 * v["price_10min"], 0.25, color=col, edgecolor="white", linewidth=0.5)
            pps = v["price_per_sla_min"]
            if pps is None:
                b2.text(x, 0.08, "no SLA\nminute", ha="center", va="bottom", fontsize=5.6, color=col, rotation=90)
                b2.plot([x], [0.04], marker="x", color=col, ms=5, mew=1.5)
            else:
                b2.bar(x, 1e3 * pps, 0.25, color=col, edgecolor="white", linewidth=0.5)
                b2.text(x, 1e3 * pps + 0.03, f"{1e3 * pps:.2f}", ha="center", va="bottom", fontsize=5.8, rotation=90)
    for b in (b1, b2):
        b.set_xticks(range(len(scen)), [l for _, l in scen])
    b1.set_ylabel("Price of a 10-min session (mETH)")
    b1.set_title("(a) Price paid")
    b2.set_ylabel("Price per SLA-compliant\nminute (mETH)")
    b2.set_title("(b) Price per minute that met the SLA")
    b2.set_ylim(0, 2.1)
    fig.legend(handles=[Patch(color=c, label=l) for _, c, l in POL2], loc="upper center", ncol=3, frameon=False,
               bbox_to_anchor=(0.5, 1.08), fontsize=7)
    save(fig, out, "fig-tariff-comparison")

    # summary.md
    md = ["# DLT slice market: results", "",
          "| Service | RAN 33 RB SLA | RAN 132 RB SLA | Purchased SLA | Key metric median (33 / 132 / purchased) | Cost (33 / 132 / purchased) |",
          "|---|---|---|---|---|---|"]
    for s in SVC:
        d = S["services"][s]
        md.append(f"| {s} | {d['ran33']['sla_met']}/{d['ran33']['steps']} | {d['ran132']['sla_met']}/{d['ran132']['steps']} | "
                  f"{d['paid']['sla_met']}/{d['paid']['steps']} | {d['ran33']['key_median']:.3g} / {d['ran132']['key_median']:.3g} / "
                  f"{d['paid']['key_median']:.3g} {KEY[s][1]} | {d['ran33']['cost_mean']:.3f} / {d['ran132']['cost_mean']:.3f} / "
                  f"{d['paid']['cost_mean']:.3f} |")
    w = S["weak"]
    md += ["", f"Degraded position: online DDQN {w['adaptive']['sla_met']}/{w['adaptive']['steps']} (RSRP {w['adaptive']['dl_rsrp_dbm']}, "
           f"quota {w['adaptive']['dl_quota']}, RLC {w['adaptive']['dl_rlc_delay_ms']:.0f} ms, cost {w['adaptive']['cost_mean']:.3f}); "
           f"RAN 33 RB {w['ran33']['sla_met']}/{w['ran33']['steps']} (RSRP {w['ran33']['dl_rsrp_dbm']}); RAN 132 RB "
           f"{w['ran132']['sla_met']}/{w['ran132']['steps']} (RSRP {w['ran132']['dl_rsrp_dbm']}, RLC {w['ran132']['dl_rlc_delay_ms']:.0f} ms).",
           f"Day-1 RAN 33 RB baselines: " + ", ".join(f"{s} {v['sla_met']}/{v['steps']} ({v['key_median']:.3g})" for s, v in S['day1_ran33'].items()),
           f"Fixed tariff: {json.dumps(ft, default=str)}",
           f"Payments: {json.dumps({k: v for k, v in S['payments'].items()})}",
           f"SMF/CHF denials: {S['smf_denials']}; CHF grants: {S['chf_grants']}"]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
