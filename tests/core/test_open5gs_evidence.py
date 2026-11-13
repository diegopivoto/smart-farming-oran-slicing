import asyncio
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import UeIdentity
from smartfarm.contracts.service import Snssai
from smartfarm.core.evidence import (
    EvidenceTimeout,
    LogCursor,
    Open5gsLogEvidenceProvider,
    correlate,
)

UN3GPP = "imsi-0010100007488"
ONEPLUS = "imsi-001010123456789"
IDENTITIES = {
    EndpointRole.UN3GPP_UE: UeIdentity(supi=UN3GPP, device_ref="un3gpp-ue-lab"),
    EndpointRole.ONEPLUS_8T: UeIdentity(supi=ONEPLUS, device_ref="oneplus-8t-lab"),
}
CROP = Snssai(sst=1, sd="000002")


def amf_request(at: str, supi: str, dnn: str, sd: int) -> str:
    return (
        f"11/02 {at}: [gmm] INFO: UE SUPI[{supi}] DNN[{dnn}] S_NSSAI[SST:1 SD:0x{sd:x}] smContextRef[NULL] "
        "smContextResourceURI[NULL] (../src/amf/gmm-handler.c:1398)"
    )


def amf_reject(at: str, supi: str) -> str:
    return f"11/02 {at}: [gmm] WARNING: [{supi}] DNN Not Supported OR Not Subscribed in the Slice (../src/amf/gmm-handler.c:1361)"


def smf_added(at: str, supi: str, dnn: str, ipv4: str) -> str:
    return f"11/02 {at}: [smf] INFO: UE SUPI[{supi}] DNN[{dnn}] IPv4[{ipv4}] IPv6[] (../src/smf/npcf-handler.c:580)"


def smf_removed(at: str, supi: str, dnn: str, ipv4: str) -> str:
    return (
        f"11/02 {at}: [smf] INFO: Removed Session: UE IMSI:[{supi}] DNN:[{dnn}:1] IPv4:[{ipv4}] IPv6:[] "
        "(../src/smf/context.c:1693)"
    )


def test_interleaved_ues_are_attributed_by_supi_and_dnn() -> None:
    amf = [
        amf_request("21:00:04.440", ONEPLUS, "crop", 2),
        amf_request("21:00:04.441", UN3GPP, "internet", 2),
    ]
    smf = [
        smf_added("21:00:04.444", UN3GPP, "internet", "10.45.0.93"),
        smf_added("21:00:04.445", ONEPLUS, "crop", "10.45.0.94"),
    ]

    (phone,) = correlate(amf, smf, ONEPLUS)
    (un3gpp,) = correlate(amf, smf, UN3GPP)

    assert (phone.snssai, phone.dnn, phone.ipv4) == ((1, "000002"), "crop", IPv4Address("10.45.0.94"))
    assert (un3gpp.snssai, un3gpp.ipv4) == ((1, "000002"), IPv4Address("10.45.0.93"))


def test_session_released_right_after_setup_is_marked_removed() -> None:
    amf = [amf_request("20:39:58.229", ONEPLUS, "crop", 2), amf_request("20:43:53.874", ONEPLUS, "crop", 2)]
    smf = [
        smf_added("20:39:58.235", ONEPLUS, "crop", "10.45.0.66"),
        smf_removed("20:39:58.326", ONEPLUS, "crop", "10.45.0.66"),
        smf_added("20:43:53.880", ONEPLUS, "crop", "10.45.0.89"),
    ]

    first, second = correlate(amf, smf, ONEPLUS)

    assert first.removed and first.ipv4 == IPv4Address("10.45.0.66")
    assert not second.removed and second.ipv4 == IPv4Address("10.45.0.89")


def test_rejected_request_has_no_session_and_malformed_lines_are_ignored() -> None:
    amf = [amf_reject("20:10:10.334", ONEPLUS), "garbage line", amf_request("20:10:26.179", ONEPLUS, "crop", 1)]

    (candidate,) = correlate(amf, ["11/02 broken [smf]"], ONEPLUS)

    assert candidate.ipv4 is None and candidate.snssai == (1, "000001")


def test_cursor_reads_only_new_complete_lines_and_survives_rotation(tmp_path: Path) -> None:
    log = tmp_path / "smf.log"
    log.write_text("old line\n", encoding="utf-8")
    cursor = LogCursor.at_end(log)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("new 1\npartial")

    lines, cursor = cursor.read()
    assert lines == ["new 1"]

    with log.open("a", encoding="utf-8") as handle:
        handle.write(" done\n")
    log.rename(tmp_path / "smf.log.1")
    log.write_text("after rotation\n", encoding="utf-8")

    lines, cursor = cursor.read()
    assert lines == ["partial done", "after rotation"]


def provider(tmp_path: Path) -> tuple[Open5gsLogEvidenceProvider, Path, Path]:
    amf, smf = tmp_path / "amf.log", tmp_path / "smf.log"
    amf.write_text(amf_request("19:00:00.000", ONEPLUS, "crop", 2) + "\n", encoding="utf-8")  # before the cursor
    smf.write_text(smf_added("19:00:00.010", ONEPLUS, "crop", "10.45.0.50") + "\n", encoding="utf-8")
    return Open5gsLogEvidenceProvider(IDENTITIES, amf_log=amf, smf_log=smf, poll_s=0.01), amf, smf


def append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


@pytest.mark.asyncio
async def test_await_session_ignores_events_before_cursor_and_requires_hold(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)
    since = evidence_provider.cursor()

    with pytest.raises(EvidenceTimeout):
        await evidence_provider.await_session(EndpointRole.ONEPLUS_8T, CROP, "crop", since, timeout_s=0.1, hold_s=0.0)

    append(amf, amf_request("21:00:04.440", ONEPLUS, "crop", 2))
    append(smf, smf_added("21:00:04.445", ONEPLUS, "crop", "10.45.0.94"))
    evidence = await evidence_provider.await_session(
        EndpointRole.ONEPLUS_8T, CROP, "crop", since, timeout_s=2.0, hold_s=0.05
    )

    assert evidence.ipv4 == IPv4Address("10.45.0.94")
    assert evidence.device_ref == "oneplus-8t-lab" and evidence.held_s >= 0.05
    assert ONEPLUS not in repr(evidence)


@pytest.mark.asyncio
async def test_session_removed_during_hold_is_not_accepted(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)
    since = evidence_provider.cursor()
    append(amf, amf_request("20:39:58.229", ONEPLUS, "crop", 2))
    append(smf, smf_added("20:39:58.235", ONEPLUS, "crop", "10.45.0.66"))

    async def release_soon() -> None:
        await asyncio.sleep(0.05)
        append(smf, smf_removed("20:39:58.326", ONEPLUS, "crop", "10.45.0.66"))

    task = asyncio.create_task(release_soon())
    with pytest.raises(EvidenceTimeout):
        await evidence_provider.await_session(EndpointRole.ONEPLUS_8T, CROP, "crop", since, timeout_s=0.4, hold_s=0.3)
    await task


@pytest.mark.asyncio
async def test_await_absent_sees_removal_of_old_address(tmp_path: Path) -> None:
    evidence_provider, _amf, smf = provider(tmp_path)
    since = evidence_provider.cursor()

    assert await evidence_provider.await_absent(
        EndpointRole.UN3GPP_UE, IPv4Address("10.45.0.88"), since, timeout_s=0.05
    ) is None
    append(smf, smf_removed("21:00:03.383", UN3GPP, "internet", "10.45.0.88"))
    removed_at = await evidence_provider.await_absent(EndpointRole.UN3GPP_UE, IPv4Address("10.45.0.88"), since, timeout_s=1)
    assert removed_at == "11/02 21:00:03.383"


def test_log_time_to_epoch_has_millisecond_resolution() -> None:
    from smartfarm.core.evidence import log_time_to_epoch

    delta = log_time_to_epoch("11/02 21:00:04.445", 2026) - log_time_to_epoch("11/02 21:00:03.383", 2026)
    assert abs(delta - 1.062) < 1e-6


def test_current_session_is_latest_active_one(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)
    append(amf, amf_request("21:00:04.440", ONEPLUS, "crop", 2))
    append(smf, smf_removed("21:00:03.383", ONEPLUS, "crop", "10.45.0.50"), smf_added("21:00:04.445", ONEPLUS, "crop", "10.45.0.94"))

    current = evidence_provider.current_session(EndpointRole.ONEPLUS_8T)

    assert current is not None and current.ipv4 == IPv4Address("10.45.0.94") and current.snssai == CROP
    assert evidence_provider.current_session(EndpointRole.UN3GPP_UE) is None


@pytest.mark.parametrize("compressed", [False, True])
def test_current_session_survives_midnight_logrotate(tmp_path: Path, compressed: bool) -> None:
    """2026-11-03 00:00:03: the rotation hid the phone session and aborted the overnight calibration."""
    import gzip

    evidence_provider, amf, smf = provider(tmp_path)
    for path, line in ((amf, amf_request("22:07:55.570", ONEPLUS, "drone", 3)),
                       (smf, smf_added("22:07:55.575", ONEPLUS, "drone", "10.45.160.2"))):
        old = "Open5GS daemon v2.7.5\n" + path.read_text(encoding="utf-8") + line + "\n"
        if compressed:
            with gzip.open(path.with_name(path.name + ".1.gz"), "wt", encoding="utf-8") as handle:
                handle.write(old)
        else:
            path.with_name(path.name + ".1").write_text(old, encoding="utf-8")
        path.write_text("", encoding="utf-8")
    append(amf, amf_request("00:05:44.571", UN3GPP, "internet", 1))
    append(smf, smf_added("00:05:44.576", UN3GPP, "internet", "10.45.0.7"))

    phone = evidence_provider.current_session(EndpointRole.ONEPLUS_8T)
    un3gpp = evidence_provider.current_session(EndpointRole.UN3GPP_UE)

    assert phone is not None and phone.ipv4 == IPv4Address("10.45.160.2") and phone.dnn == "drone"
    assert un3gpp is not None and un3gpp.ipv4 == IPv4Address("10.45.0.7")


def test_current_session_ignores_sessions_before_daemon_restart(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)  # crop session 10.45.0.50 logged before the restart
    append(amf, "Open5GS daemon v2.7.5")
    append(smf, "Open5GS daemon v2.7.5")

    assert evidence_provider.current_session(EndpointRole.ONEPLUS_8T) is None


def test_active_sessions_lists_the_default_and_the_service_session(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)  # crop 10.45.0.50 already up
    append(amf, amf_request("21:00:01.000", ONEPLUS, "internet", 1), amf_request("21:00:02.000", ONEPLUS, "drone", 3))
    append(smf, smf_added("21:00:01.005", ONEPLUS, "internet", "10.45.0.15"),
           smf_added("21:00:02.005", ONEPLUS, "drone", "10.45.160.4"),
           smf_removed("21:00:03.000", ONEPLUS, "crop", "10.45.0.50"))

    sessions = evidence_provider.active_sessions(EndpointRole.ONEPLUS_8T)

    assert [(s.dnn, str(s.ipv4)) for s in sessions] == [("internet", "10.45.0.15"), ("drone", "10.45.160.4")]
    assert sessions[1].requested_at == "11/02 21:00:02.000"


@pytest.mark.asyncio
async def test_await_active_confirms_a_session_opened_before_the_call(tmp_path: Path) -> None:
    """App mode: the phone opened its service session before asking the orchestrator (before any cursor)."""

    evidence_provider, _amf, _smf = provider(tmp_path)  # crop 10.45.0.50, established in the past (age > hold)

    evidence = await evidence_provider.await_active(
        EndpointRole.ONEPLUS_8T, CROP, "crop", IPv4Address("10.45.0.50"), timeout_s=0.5, hold_s=2.0
    )

    assert evidence.ipv4 == IPv4Address("10.45.0.50") and evidence.held_s == 2.0
    assert evidence.requested_at == "11/02 19:00:00.000" and evidence.established_at == "11/02 19:00:00.010"
    for wrong_dnn, address in (("drone", "10.45.0.50"), ("crop", "10.45.0.51")):
        with pytest.raises(EvidenceTimeout):
            await evidence_provider.await_active(
                EndpointRole.ONEPLUS_8T, CROP, wrong_dnn, IPv4Address(address), timeout_s=0.05, hold_s=0.0
            )


@pytest.mark.asyncio
async def test_await_active_waits_for_a_session_that_shows_up_later(tmp_path: Path) -> None:
    evidence_provider, amf, smf = provider(tmp_path)

    async def appear() -> None:
        await asyncio.sleep(0.05)
        append(amf, amf_request("21:00:04.440", ONEPLUS, "crop", 2))
        append(smf, smf_added("21:00:04.445", ONEPLUS, "crop", "10.45.128.2"))

    task = asyncio.create_task(appear())
    evidence = await evidence_provider.await_active(
        EndpointRole.ONEPLUS_8T, CROP, "crop", IPv4Address("10.45.128.2"), timeout_s=2.0, hold_s=0.0
    )
    await task
    assert evidence.ipv4 == IPv4Address("10.45.128.2")
