#!/usr/bin/env python3
"""Evidence of the DLT slice market for the paper: log excerpts of a time window, as text and terminal-style PNG.

    dlt_evidence.py --since "2026-11-14 15:30" [--until ...] --output experiments/dlt/evidence/<name>

Writes, for the window: smf-chf.txt (SMF Nchf consumer lines), amf-slices.txt (registration and PDU sessions of both
UEs), smf-sessions.txt (UE SUPI/DNN/IP per session), chf.txt (CHF log), cdrs.jsonl and purchases.jsonl (filtered),
nrf-chf.json (NRF discovery of the CHF) and one PNG per text file. SUPIs are masked (imsi-00101…7488).
"""

import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
LOGS = Path("/var/log/open5gs")
IMSI = re.compile(r"(imsi-|SUPI\[imsi-|suci-0-001-01-0-0-0-)(\d{5})\d*(\d{4})")


def mask(line: str) -> str:
    return IMSI.sub(lambda m: f"{m[1]}{m[2]}…{m[3]}", line)


def window(lines: list[str], since: datetime, until: datetime) -> list[str]:
    out = []
    for ln in lines:
        m = re.match(r"(\d\d)/(\d\d) (\d\d:\d\d:\d\d)", ln)
        if not m:
            continue
        t = datetime.strptime(f"{since.year}/{m[1]}/{m[2]} {m[3]}", "%Y/%m/%d %H:%M:%S")
        if since <= t <= until and not re.search(r"suci-0-(?!001-01)", ln):  # drop third-party UEs (other PLMNs)
            out.append(mask(ln.rstrip()))
    return out


def grep(path: Path, pattern: str, since: datetime, until: datetime) -> list[str]:
    text = subprocess.run(["sudo", "-A", "cat", str(path)], capture_output=True, text=True, check=False).stdout \
        if not path.exists() or not path.stat().st_mode & 0o004 else path.read_text(errors="replace")
    rx = re.compile(pattern)
    return [ln for ln in window(text.splitlines(), since, until) if rx.search(ln)]


def png(lines: list[str], title: str, path: Path, width: int = 150) -> None:
    font = ImageFont.truetype("DejaVuSansMono.ttf", 15)
    wrapped = []
    for ln in lines:
        ln = re.sub(r" \(\.\./[^)]*\)$", "", ln)  # drop the source-file suffix of Open5GS lines
        wrapped += [ln[i:i + width] for i in range(0, max(len(ln), 1), width)]
    h = 40 + 20 * len(wrapped) + 16
    img = Image.new("RGB", (int(9.1 * width) + 32, h), "#0d1117")
    d = ImageDraw.Draw(img)
    d.text((16, 10), title, font=font, fill="#7ee787")
    for i, ln in enumerate(wrapped):
        color = "#ff7b72" if re.search(r"denied|403|reject|ERROR", ln) else "#e6edf3"
        d.text((16, 40 + 20 * i), ln, font=font, fill=color)
    img.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--chf-log", type=Path, default=ROOT / "experiments/dlt/chf.log")
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since)
    until = datetime.fromisoformat(a.until) if a.until else datetime.now()
    a.output.mkdir(parents=True, exist_ok=True)
    texts = {
        "smf-chf": ("SMF (CTF): Nchf_ConvergedCharging consumer", grep(LOGS / "smf.log", r"\[CHF\]|CHF did not grant", since, until)),
        "smf-sessions": ("SMF: PDU sessions (SUPI, DNN, IPv4)", grep(LOGS / "smf.log", r"UE SUPI\[.*DNN\[", since, until)),
        "amf-slices": ("AMF: registration and PDU sessions",
                       grep(LOGS / "amf.log", r"Registration complete|Registration reject|PDU session|S_NSSAI|DNN", since, until)),
    }
    chf = [mask(ln) for ln in a.chf_log.read_text().splitlines()] if a.chf_log.exists() else []
    chf = [ln for ln in chf if ln[:19] >= since.strftime("%Y-%m-%dT%H:%M:%S") or not ln[:4].isdigit()]
    chf = [ln for ln in chf if "poll error" not in ln]   # transient public-RPC errors (retried), not evidence
    texts["chf"] = ("CHF: NRF registration, Nchf requests and DLT purchases", chf)
    key = re.compile(r"NFRegister|NFDeregister|\[DLT\] purchase|\[CHF\] grant|create .* -> 403|charge purchase .* mined|"
                     r"close purchase .* mined|paused|resumed")
    texts["chf-key"] = ("CHF: NRF registration, on-chain purchases, grants, denials and per-minute charges",
                        [ln for ln in chf if key.search(ln)])
    for name, (title, lines) in texts.items():
        (a.output / f"{name}.txt").write_text("\n".join(lines) + "\n")
        if lines:
            for k in range(0, len(lines), 45):  # 45 lines per image, every line kept
                png(lines[k:k + 45], title + (f" ({k // 45 + 1})" if len(lines) > 45 else ""),
                    a.output / (f"{name}.png" if k == 0 else f"{name}-{k // 45 + 1}.png"))
    for name in ("cdrs.jsonl", "purchases.jsonl"):
        src = ROOT / "experiments/chf" / name
        if src.exists():
            keep = [ln for ln in src.read_text().splitlines() if json.loads(ln).get("at", "")[:19] >= since.isoformat()[:19]]
            (a.output / name).write_text(mask("\n".join(keep)) + "\n")
    nrf = subprocess.run(["curl", "-s", "--http2-prior-knowledge",
                          "http://127.0.0.10:7777/nnrf-disc/v1/nf-instances?target-nf-type=CHF&requester-nf-type=SMF"],
                         capture_output=True, text=True, check=False).stdout
    (a.output / "nrf-chf.json").write_text(nrf + "\n")
    print(json.dumps({k: len(v[1]) for k, v in texts.items()}), "->", a.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
