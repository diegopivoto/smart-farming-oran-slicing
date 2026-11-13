from ipaddress import IPv4Address

import pytest

from smartfarm.contracts.service import ServiceId
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, Un3gppSliceError, parse_status


def status(slice_index: int, psi: int, ip: str, state: str = "accepted") -> str:
    return (
        f"STATUS slice={slice_index} sst=1 sd=00000{slice_index} pdu_session_id={psi} vrf=vrf-n3iwue "
        f"iface=gretun-id-{psi + 1}-1 ip={ip} status={state}"
    )


class FakeN3iwue:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.commands: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> str:
        self.commands.append(argv)
        if argv[0] == "slice":
            return "OK requested"
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def test_parse_live_status_line() -> None:
    parsed = parse_status(status(2, 2, "10.45.0.93"))

    assert parsed is not None
    assert (parsed.slice_index, parsed.sd, parsed.pdu_session_id, parsed.ipv4) == (2, "000002", 2, IPv4Address("10.45.0.93"))
    assert parse_status("STATUS none status=pending") is None
    with pytest.raises(Un3gppSliceError):
        parse_status("ERROR unknown slice")


@pytest.mark.asyncio
async def test_switch_waits_for_accepted_target_on_new_pdu_session() -> None:
    fake = FakeN3iwue([status(1, 1, "10.45.0.88"), status(1, 1, "10.45.0.88", "pending"), status(2, 2, "10.45.0.93")])
    client = Un3gppSliceClient(fake, poll_s=0)

    assert await client.request(ServiceId.CROP)
    result = await client.await_status(ServiceId.CROP, previous_pdu_id=1, timeout_s=1)

    assert ["slice", "2"] in fake.commands
    assert result.ipv4 == IPv4Address("10.45.0.93") and result.pdu_session_id == 2


@pytest.mark.asyncio
async def test_request_is_idempotent_when_slice_already_active() -> None:
    fake = FakeN3iwue([status(3, 4, "10.45.0.99")])

    assert not await Un3gppSliceClient(fake, poll_s=0).request(ServiceId.DRONE)
    assert fake.commands == [["status"]]


@pytest.mark.asyncio
async def test_rejected_switch_fails() -> None:
    fake = FakeN3iwue([status(1, 1, "10.45.0.88", "rejected")])

    with pytest.raises(Un3gppSliceError, match="rejected"):
        await Un3gppSliceClient(fake, poll_s=0).await_status(ServiceId.CROP, previous_pdu_id=1, timeout_s=1)


@pytest.mark.asyncio
async def test_old_pdu_session_or_wrong_sd_times_out() -> None:
    fake = FakeN3iwue([status(2, 2, "10.45.0.93")])
    client = Un3gppSliceClient(fake, poll_s=0.01)

    with pytest.raises(Un3gppSliceError, match="did not reach"):
        await client.await_status(ServiceId.CROP, previous_pdu_id=2, timeout_s=0.05)
    with pytest.raises(Un3gppSliceError, match="did not reach"):
        await client.await_status(ServiceId.DRONE, previous_pdu_id=None, timeout_s=0.05)


@pytest.mark.asyncio
async def test_refused_request_raises() -> None:
    async def refusing(argv: list[str]) -> str:
        return status(1, 1, "10.45.0.88") if argv == ["status"] else "ERROR unknown slice"

    with pytest.raises(Un3gppSliceError, match="refused"):
        await Un3gppSliceClient(refusing, poll_s=0).request(ServiceId.CROP)
