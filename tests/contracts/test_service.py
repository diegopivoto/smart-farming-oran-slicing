from typing import cast

import pytest
from pydantic import ValidationError

from smartfarm.contracts.service import (
    SERVICE_CATALOG,
    RadioBounds,
    RadioSetpoint,
    ServiceId,
    Snssai,
    get_service_profile,
)


def test_catalog_defines_the_three_smart_farming_services() -> None:
    assert set(SERVICE_CATALOG) == {
        ServiceId.WEATHER,
        ServiceId.CROP,
        ServiceId.DRONE,
    }

    expected = {
        ServiceId.WEATHER: {
            "sd": "000001",
            "rate": 100_000,
            "dl_prb": (24, 52),
            "ul_rb": (33, 66),
            "mcs": (0, 10),
            "initial": (24, 33, 3),
        },
        ServiceId.CROP: {
            "sd": "000002",
            "rate": 20_000_000,
            "dl_prb": (24, 52, 78),
            "ul_rb": (33, 66, 99),
            "mcs": (0, 15),
            "initial": (52, 66, 6),
        },
        ServiceId.DRONE: {
            "sd": "000003",
            "rate": 40_000_000,
            "dl_prb": (52, 78, 106),
            "ul_rb": (66, 99, 132),
            "mcs": (0, 14),
            "initial": (78, 99, 9),
        },
    }

    for service_id, values in expected.items():
        profile = SERVICE_CATALOG[service_id]
        assert profile.service_id is service_id
        assert profile.snssai == Snssai(sst=1, sd=cast(str, values["sd"]))
        assert profile.dnn == "internet"
        assert profile.offered_rate_bps == values["rate"]
        assert profile.radio_bounds.dl_prb == values["dl_prb"]
        assert profile.radio_bounds.ul_rb == values["ul_rb"]
        assert (
            profile.radio_bounds.ul_mcs_min,
            profile.radio_bounds.ul_mcs_max,
        ) == values["mcs"]
        assert (
            profile.initial_radio.dl_prb,
            profile.initial_radio.ul_rb,
            profile.initial_radio.ul_mcs,
        ) == values["initial"]


def test_catalog_models_are_immutable() -> None:
    with pytest.raises(ValidationError):
        SERVICE_CATALOG[ServiceId.WEATHER].offered_rate_bps = 1


@pytest.mark.parametrize("sd", ["1", "00001", "0000001", "00000g", "-00001"])
def test_snssai_rejects_non_six_digit_hex_sd(sd: str) -> None:
    with pytest.raises(ValidationError):
        Snssai(sst=1, sd=sd)


def test_radio_bounds_reject_values_outside_global_action_space() -> None:
    with pytest.raises(ValidationError):
        RadioBounds(dl_prb=(25,), ul_rb=(33,), ul_mcs_min=0, ul_mcs_max=17)


def test_radio_setpoint_rejects_values_outside_global_action_space() -> None:
    with pytest.raises(ValidationError):
        RadioSetpoint(dl_prb=24, ul_rb=34, ul_mcs=3)


def test_service_lookup_accepts_enum_and_string_and_rejects_unknown() -> None:
    assert get_service_profile(ServiceId.DRONE) is SERVICE_CATALOG[ServiceId.DRONE]
    assert get_service_profile("weather") is SERVICE_CATALOG[ServiceId.WEATHER]

    with pytest.raises(ValueError, match="unknown service_id"):
        get_service_profile("unknown")
