import json

import pytest

from sfcontent import protocol as P
from sfcontent.weather import THRESHOLDS, WeatherModel


def run(model: WeatherModel, seconds: float, dt: float = 0.1) -> list[dict]:
    docs = []
    for _ in range(int(round(seconds / dt))):
        model.step(dt)
        docs.append(model.document())
    return docs


def first_time(docs: list[dict], alert: str, dt: float = 0.1) -> float | None:
    return next(((i + 1) * dt for i, d in enumerate(docs) if any(a["id"] == alert for a in d["alerts"])), None)


@pytest.fixture()
def model() -> WeatherModel:
    return WeatherModel(seed=3, start_time=1_793_700_000, fixed_hour=13.0)


def test_stable_stays_within_thresholds(model: WeatherModel) -> None:
    docs = run(model, 120)
    assert all(d["state"] == "stable" and d["alerts"] == [] for d in docs)
    r = docs[-1]["readings"]
    assert 30 < r["soil_moisture_pct"] < 50 and r["air_humidity_pct"] >= 40 and r["rain_rate_mm_h"] == 0
    assert set(r) == {"air_temperature_c", "air_humidity_pct", "soil_moisture_pct", "rain_rate_mm_h", "rain_today_mm",
                      "wind_speed_kmh", "wind_direction_deg", "solar_radiation_w_m2", "uv_index", "pressure_hpa",
                      "leaf_wetness_pct"}
    assert docs[-1]["thresholds"] == THRESHOLDS


def test_drought_falls_gradually_and_raises_dry_soil(model: WeatherModel) -> None:
    run(model, 30)
    model.set_state("drought")
    docs = run(model, 60)
    t_dry, t_hum = first_time(docs, "dry_soil"), first_time(docs, "low_humidity")
    assert t_dry is not None and 8 <= t_dry <= 40  # visible change over ~20-40 s, no jump
    assert t_hum is not None and 10 <= t_hum <= 45
    alert = next(a for a in docs[-1]["alerts"] if a["id"] == "dry_soil")
    assert alert["action"] == "irrigation" and "irrigation" in alert["text"]
    soil = [d["readings"]["soil_moisture_pct"] for d in docs]
    assert max(abs(b - a) for a, b in zip(soil, soil[1:])) < 0.6  # smooth at 10 Hz


def test_irrigation_recovers_soil_first_then_clears(model: WeatherModel) -> None:
    model.set_state("drought")
    run(model, 90)
    assert {a["id"] for a in model.alerts()} >= {"dry_soil"}
    assert model.irrigate(60) == "irrigation on for 60 s"
    docs = run(model, 40)
    assert docs[0]["irrigation"] == {"active": True, "remaining_s": 60}
    assert docs[-1]["irrigation"]["active"] and 19 <= docs[-1]["irrigation"]["remaining_s"] <= 21
    assert docs[0]["state"] == "stable"  # the irrigation resolves the drought scenario
    cleared = next(i for i, d in enumerate(docs) if not any(a["id"] == "dry_soil" for a in d["alerts"]))
    assert 4 <= cleared * 0.1 <= 30  # soil rises after the dead time, the alert clears within ~20-30 s
    soil = [d["readings"]["soil_moisture_pct"] for d in docs]
    assert soil[-1] > 35 and soil[int(25 / 0.1)] > soil[int(5 / 0.1)]
    docs = run(model, 25)
    assert docs[-1]["irrigation"] == {"active": False, "remaining_s": 0}


def test_storm_raises_excess_rain_and_pressure_drop(model: WeatherModel) -> None:
    run(model, 20)
    before = model.readings["pressure_hpa"]
    model.set_state("storm")
    docs = run(model, 60)
    alert = next(a for a in docs[-1]["alerts"] if a["id"] == "excess_rain")
    assert alert["action"] is None
    r = docs[-1]["readings"]
    assert r["rain_rate_mm_h"] > 20 and r["wind_speed_kmh"] > 30 and r["pressure_hpa"] < before - 8
    assert r["rain_today_mm"] > 4.2


def test_invalid_commands_are_refused(model: WeatherModel) -> None:
    with pytest.raises(ValueError):
        model.set_state("rain")
    with pytest.raises(ValueError):
        model.irrigate(0)


def test_every_document_fits_one_datagram(model: WeatherModel) -> None:
    model.set_state("drought")
    run(model, 120)
    model.alerts_active = {"dry_soil", "low_humidity", "excess_rain"}  # worst case
    doc = model.document()
    assert len(doc["alerts"]) == 3
    data = P.weather_datagram(b"\x01" * 16, 5, 123, doc)
    assert len(data) == 1250
    body = data[P.DGRAM.size:]
    assert json.loads(body[:body.index(b"\0")].decode()) == json.loads(json.dumps(doc))
