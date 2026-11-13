from pathlib import Path

import pytest

from smartfarm.core.open5gs_config import gnb_snssais, validate_slices

AMF = """
amf:
  plmn_support:
    - plmn_id: {mcc: 001, mnc: 01}
      s_nssai:
        - {sst: 1, sd: "000001"}
        - {sst: 1, sd: "000002"}
        - {sst: 1, sd: "000003"}
        - {sst: 1, sd: "000004"}
"""
NSSF = """
nssf:
  sbi:
    client:
      nsi:
        - {uri: "http://127.0.0.10:7777", s_nssai: {sst: 1, sd: "000001"}}
        - {uri: "http://127.0.0.10:7777", s_nssai: {sst: 1, sd: "000002"}}
        - {uri: "http://127.0.0.10:7777", s_nssai: {sst: 1, sd: "000003"}}
"""
SMF = """
smf:
  session:
    - {subnet: 10.45.0.0/16, gateway: 10.45.0.1, dnn: crop, range: [10.45.128.2-10.45.159.254]}
    - {subnet: 10.45.0.0/16, gateway: 10.45.0.1, dnn: drone, range: [10.45.160.2-10.45.191.254]}
    - {subnet: 10.45.0.0/16, gateway: 10.45.0.1, dnn: weather, range: [10.45.192.2-10.45.223.254]}
    - {subnet: 10.45.0.0/16, gateway: 10.45.0.1, dnn: internet, range: [10.45.0.2-10.45.127.254]}
    - {subnet: "2001:db8:cafe::/48", gateway: "2001:db8:cafe::1"}
  info:
    - s_nssai:
        - {sst: 1, sd: "000001", dnn: [internet, weather]}
        - {sst: 1, sd: "000002", dnn: [internet, crop]}
        - {sst: 1, sd: "000003", dnn: [internet, drone]}
"""
UPF = """
upf:
  session:
    - {subnet: 10.45.0.0/16, gateway: 10.45.0.1}
"""
GNB = "plmn_list = ({ mcc = 001; mnc = 01; mnc_length = 2; snssaiList = ({ sst = 1; sd = 0x1 }, " \
      "{ sst = 1; sd = 0x2 }, { sst = 1; sd = 0x3 }) });"


def write(tmp_path: Path, **overrides: str) -> dict[str, Path]:
    files = {"amf": AMF, "smf": SMF, "nssf": NSSF, "upf": UPF, "gnb": GNB} | overrides
    paths = {}
    for role, text in files.items():
        path = tmp_path / f"{role}.conf"
        path.write_text(text, encoding="utf-8")
        paths[role] = path
    return paths


def test_valid_lab_configuration_passes_and_records_checksums(tmp_path: Path) -> None:
    report = validate_slices(**write(tmp_path))

    assert report.passed, report.errors
    assert set(report.checked) == {"amf", "smf", "nssf", "upf", "gnb"}


@pytest.mark.parametrize(
    ("role", "old", "new", "expected"),
    [
        ("amf", '- {sst: 1, sd: "000003"}', "", "drone SST 1 SD 000003: missing in amf plmn_support"),
        ("nssf", 'sd: "000002"}}', 'sd: "000009"}}', "crop SST 1 SD 000002: missing in nssf nsi"),
        ("smf", "[internet, crop]", "[internet]", "crop SST 1 SD 000002: smf info does not serve DNN crop"),
        ("smf", "[internet, drone]", "[drone]", "drone SST 1 SD 000003: smf info does not serve DNN internet"),
        ("upf", "10.45.0.0/16", "10.46.0.0/16", "upf session IPv4 pool must be exactly"),
        ("smf", "gateway: 10.45.0.1, dnn: internet", "gateway: 10.45.0.254, dnn: internet", "smf session IPv4 pool must be exactly"),
        ("gnb", ", { sst = 1; sd = 0x3 }", "", "drone SST 1 SD 000003: not advertised by the gNB"),
        ("nssf", 'sd: "000003"}}', 'sd: "000002"}}', "nssf nsi: duplicate S-NSSAI"),
        ("smf", "range: [10.45.0.2-10.45.127.254]", "range: [10.45.0.2-10.45.130.254]",
         "smf ranges of internet and handset DNN crop share a /30"),
        ("smf", "dnn: weather, range: [10.45.192.2-10.45.223.254]", "dnn: other",
         "no dedicated IPv4 range for handset DNN weather"),
    ],
)
def test_detects_each_inconsistency(tmp_path: Path, role: str, old: str, new: str, expected: str) -> None:
    text = {"amf": AMF, "smf": SMF, "nssf": NSSF, "upf": UPF, "gnb": GNB}[role]
    assert old in text
    report = validate_slices(**write(tmp_path, **{role: text.replace(old, new)}))

    assert not report.passed
    assert any(expected in error for error in report.errors), report.errors


def test_extra_fourth_slice_is_allowed(tmp_path: Path) -> None:
    assert validate_slices(**write(tmp_path)).passed  # AMF fixture also lists SD 000004


def test_gnb_parser_handles_decimal_and_hex_sd() -> None:
    assert gnb_snssais("snssaiList = ({ sst = 1; sd = 0x000002 }, { sst = 1; sd = 3 })") == {(1, "000002"), (1, "000003")}
