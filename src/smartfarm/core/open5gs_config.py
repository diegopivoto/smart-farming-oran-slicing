"""Read-only check that the Open5GS and OAI configuration serve every Smart Farming slice."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

import yaml

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, ServiceProfile

PDU_NETWORK = IPv4Network("10.45.0.0/16")
GATEWAY = IPv4Address("10.45.0.1")
_GNB_SNSSAI = re.compile(r"\{\s*sst\s*=\s*(\d+)\s*;\s*sd\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*\}")
_GNB_SNSSAI_LIST = re.compile(r"snssaiList\s*=\s*\((?P<body>[^)]*)\)")


@dataclass(frozen=True)
class CoreConfigReport:
    """Outcome of one validation; `errors` empty means every service slice is served."""

    errors: tuple[str, ...]
    checked: Mapping[str, str] = field(default_factory=dict)  # file role -> sha256

    @property
    def passed(self) -> bool:
        return not self.errors


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return document


def _snssai(entry: Mapping[str, Any]) -> tuple[int, str | None]:
    sd = entry.get("sd")
    return int(entry["sst"]), None if sd is None else f"{int(str(sd), 16):06x}"


def _section(document: Mapping[str, Any], *keys: str) -> Any:
    node: Any = document
    for key in keys:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node


def _ranges(document: Mapping[str, Any]) -> dict[str, list[tuple[IPv4Address, IPv4Address]]]:
    """SMF IPv4 allocation ranges per DNN ('' = any DNN)."""

    found: dict[str, list[tuple[IPv4Address, IPv4Address]]] = {}
    for entry in _section(document, "smf", "session") or []:
        try:
            subnet = IPv4Network(entry["subnet"], strict=False)
        except ValueError:
            continue
        spans = []
        for item in entry.get("range") or [f"{subnet.network_address + 1}-{subnet.broadcast_address - 1}"]:
            low, _, high = str(item).partition("-")
            spans.append((IPv4Address(low or subnet.network_address + 1), IPv4Address(high or subnet.broadcast_address - 1)))
        found.setdefault(str(entry.get("dnn") or ""), []).extend(spans)
    return found


def _sessions(document: Mapping[str, Any], nf: str) -> set[tuple[IPv4Network, IPv4Address]]:
    found = set()
    for entry in _section(document, nf, "session") or []:
        try:
            subnet = IPv4Network(entry["subnet"], strict=False)
        except ValueError:
            continue  # the IPv6 pool
        found.add((subnet, IPv4Address(entry["gateway"])))
    return found


def gnb_snssais(conf: str) -> set[tuple[int, str]]:
    """S-NSSAIs advertised in an OAI gNB libconfig file (`snssaiList`)."""

    found = set()
    for block in _GNB_SNSSAI_LIST.finditer(conf):
        for sst, sd in _GNB_SNSSAI.findall(block["body"]):
            found.add((int(sst), f"{int(sd, 0):06x}"))
    return found


def validate_slices(
    *,
    amf: Path,
    smf: Path,
    nssf: Path,
    upf: Path,
    gnb: Path | None = None,
    catalog: Mapping[ServiceId, ServiceProfile] = SERVICE_CATALOG,
) -> CoreConfigReport:
    """Check every service S-NSSAI/DNN against AMF, NSSF, SMF, UPF and (optionally) the OAI gNB."""

    errors: list[str] = []
    paths = {"amf": amf, "smf": smf, "nssf": nssf, "upf": upf, **({"gnb": gnb} if gnb else {})}
    checked = {role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in paths.items()}
    amf_doc, smf_doc, nssf_doc, upf_doc = (_load(p) for p in (amf, smf, nssf, upf))

    amf_slices: list[tuple[int, str | None]] = []
    for plmn in _section(amf_doc, "amf", "plmn_support") or []:
        amf_slices += [_snssai(s) for s in plmn.get("s_nssai") or []]
    nssf_slices = [_snssai(n["s_nssai"]) for n in _section(nssf_doc, "nssf", "sbi", "client", "nsi") or []]
    smf_dnns: dict[tuple[int, str | None], set[str]] = {}
    for info in _section(smf_doc, "smf", "info") or []:
        for entry in info.get("s_nssai") or []:
            smf_dnns.setdefault(_snssai(entry), set()).update(entry.get("dnn") or [])
    advertised = gnb_snssais(gnb.read_text(encoding="utf-8")) if gnb else None

    for role, slices in (("amf plmn_support", amf_slices), ("nssf nsi", nssf_slices)):
        duplicates = {s for s in slices if slices.count(s) > 1}
        if duplicates:
            errors.append(f"{role}: duplicate S-NSSAI {sorted(duplicates, key=str)}")

    for service, profile in catalog.items():
        key = (profile.snssai.sst, profile.snssai.sd.lower())
        label = f"{service.value} SST {key[0]} SD {key[1]}"
        if key not in amf_slices:
            errors.append(f"{label}: missing in amf plmn_support")
        if key not in nssf_slices:
            errors.append(f"{label}: missing in nssf nsi")
        if key not in smf_dnns:
            errors.append(f"{label}: missing in smf info")
        else:
            for dnn in sorted({profile.dnn, profile.handset_dnn} - smf_dnns[key]):
                errors.append(f"{label}: smf info does not serve DNN {dnn}")
        if advertised is not None and key not in advertised:
            errors.append(f"{label}: not advertised by the gNB snssaiList")

    handset_dnns = [p.handset_dnn for p in catalog.values()]
    if len(set(handset_dnns)) != len(handset_dnns):
        errors.append("handset DNNs must be unique per service (the DNN selects the slice)")

    # The OnePlus drops packets whose source is the network/broadcast address of its /30: UN3GPP addresses must come
    # from ranges disjoint from (and /30-aligned away from) the phone's service DNNs.
    ranges = _ranges(smf_doc)
    un3gpp_dnns = {p.dnn for p in catalog.values()}
    for handset_dnn in sorted({p.handset_dnn for p in catalog.values()} - un3gpp_dnns):
        if handset_dnn not in ranges:
            errors.append(f"smf session has no dedicated IPv4 range for handset DNN {handset_dnn}")
            continue
        for dnn in sorted(un3gpp_dnns):
            for low, high in ranges.get(dnn, ranges.get("", [])):
                for h_low, h_high in ranges[handset_dnn]:
                    if int(low) >> 2 <= int(h_high) >> 2 and int(h_low) >> 2 <= int(high) >> 2:
                        errors.append(f"smf ranges of {dnn} and handset DNN {handset_dnn} share a /30")

    expected = {(PDU_NETWORK, GATEWAY)}
    for nf, document in (("smf", smf_doc), ("upf", upf_doc)):
        if _sessions(document, nf) != expected:
            errors.append(f"{nf} session IPv4 pool must be exactly {PDU_NETWORK} gateway {GATEWAY}")

    return CoreConfigReport(errors=tuple(errors), checked=checked)
