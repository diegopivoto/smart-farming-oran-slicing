"""Weather station model for the demo: smooth first-order dynamics towards per-state targets, slow Ornstein-Uhlenbeck
fluctuations, a diurnal cycle, threshold alerts and irrigation recovery. Stdlib only, deterministic for a given seed.

States (contract v1): `stable`, `drought` (soil and air humidity fall past their thresholds -> `dry_soil` alert with
action `irrigation`), `storm` (heavy rain, strong wind, pressure drop -> `excess_rain` alert, no action).
Irrigation (`irrigate`) brings the readings back towards `stable` over ~20-30 s: soil moisture rises first (after a
short dead time, the water reaching the probe), so `dry_soil` clears first, then the slower air humidity.
"""

from __future__ import annotations

import math
import random
import time

STATES = ("stable", "drought", "storm")
THRESHOLDS = {"soil_moisture_pct": {"min": 30}, "air_humidity_pct": {"min": 40}, "rain_rate_mm_h": {"max": 20}}
HYSTERESIS = {"soil_moisture_pct": 1.5, "air_humidity_pct": 2.0, "rain_rate_mm_h": 2.0}
IRRIGATION_DEAD_TIME_S = 3.0
MAX_IRRIGATION_S = 3600

# time constants (s): ~63 % of a state change after tau, ~95 % after 3 tau
TAU = {"air_temperature_c": 14.0, "air_humidity_pct": 18.0, "soil_moisture_pct": 26.0, "rain_rate_mm_h": 7.0,
       "wind_speed_kmh": 8.0, "solar_factor": 9.0, "pressure_hpa": 15.0, "leaf_wetness_pct": 10.0}
# slow fluctuations: (standard deviation, correlation time s)
NOISE = {"air_temperature_c": (0.25, 25.0), "air_humidity_pct": (1.2, 20.0), "soil_moisture_pct": (0.25, 30.0),
         "rain_rate_mm_h": (0.12, 6.0), "wind_speed_kmh": (0.18, 4.0), "solar_factor": (0.03, 8.0),
         "pressure_hpa": (0.15, 40.0), "leaf_wetness_pct": (2.0, 15.0), "wind_direction_deg": (8.0, 20.0)}


def diurnal(hour: float) -> dict[str, float]:
    """Clear-sky baselines for a tropical highland site (Sul de Minas) at local `hour`."""

    sun = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
    return {"sun": sun, "solar_w_m2": 980.0 * sun ** 1.25,
            "temperature_c": 20.5 + 5.5 * math.sin(2 * math.pi * (hour - 9.0) / 24.0),
            "humidity_pct": 68.0 - 14.0 * math.sin(2 * math.pi * (hour - 9.0) / 24.0),
            "leaf_wetness_pct": 8.0 + (30.0 if hour < 7.5 or hour > 20.0 else 0.0)}


class WeatherModel:
    def __init__(self, seed: int | None = None, state: str = "stable", start_time: float | None = None,
                 fixed_hour: float | None = None, station: str = "field-A") -> None:
        if state not in STATES:
            raise ValueError(f"unknown weather state {state!r}")
        self.rng = random.Random(seed)
        self.station = station
        self.state = state
        self.fixed_hour = fixed_hour
        self.t = time.time() if start_time is None else start_time
        self.irrigation_start: float | None = None
        self.irrigation_end: float | None = None
        self.irrigation_duration = 0
        self.alerts_active: set[str] = set()
        self.events: list[str] = []
        base = diurnal(self.hour())
        self.x = {"air_temperature_c": base["temperature_c"], "air_humidity_pct": base["humidity_pct"],
                  "soil_moisture_pct": 38.0, "rain_rate_mm_h": 0.0, "wind_speed_kmh": 7.5, "solar_factor": 0.95,
                  "pressure_hpa": 1013.0, "leaf_wetness_pct": base["leaf_wetness_pct"]}
        self.noise = {name: 0.0 for name in NOISE}
        self.wind_dir = 135.0
        self.rain_today = 4.2
        self.day = self._day_index()
        self.gust_phase = self.rng.random() * 6.28
        self.readings: dict[str, float] = {}
        self._compute_readings()
        self._update_alerts()
        self.events.clear()
        if state != "stable":  # start already in the requested regime
            for _ in range(600):
                self.step(0.5, advance_clock=False)
            self.events.clear()

    # ------------------------------------------------------------ clock
    def hour(self) -> float:
        if self.fixed_hour is not None:
            return self.fixed_hour % 24
        lt = time.localtime(self.t)
        return lt.tm_hour + lt.tm_min / 60 + lt.tm_sec / 3600

    def _day_index(self) -> int:
        return time.localtime(self.t).tm_yday

    # ------------------------------------------------------------ commands
    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown weather state {state!r}")
        if state != self.state:
            self.events.append(f"state {self.state} -> {state}")
        self.state = state

    def irrigate(self, duration_s: float) -> str:
        if not 1 <= duration_s <= MAX_IRRIGATION_S:
            raise ValueError(f"duration_s must be within 1..{MAX_IRRIGATION_S}")
        self.irrigation_start = self.t
        self.irrigation_end = self.t + duration_s
        self.irrigation_duration = int(round(duration_s))
        if self.state == "drought":  # the irrigation resolves the drought scenario
            self.set_state("stable")
        self.events.append(f"irrigation on for {self.irrigation_duration} s")
        return f"irrigation on for {self.irrigation_duration} s"

    def irrigation_active(self) -> bool:
        return self.irrigation_end is not None and self.t < self.irrigation_end

    def irrigation_remaining(self) -> int:
        return max(0, math.ceil(self.irrigation_end - self.t)) if self.irrigation_active() else 0

    # ------------------------------------------------------------ dynamics
    def targets(self) -> dict[str, float]:
        base = diurnal(self.hour())
        stable = {"air_temperature_c": base["temperature_c"], "air_humidity_pct": base["humidity_pct"],
                  "soil_moisture_pct": 38.0, "rain_rate_mm_h": 0.0, "wind_speed_kmh": 7.5, "solar_factor": 0.95,
                  "pressure_hpa": 1013.0, "leaf_wetness_pct": base["leaf_wetness_pct"]}
        if self.state == "drought":
            target = {**stable, "air_temperature_c": base["temperature_c"] + 7.0, "air_humidity_pct": 27.0,
                      "soil_moisture_pct": 17.0, "wind_speed_kmh": 13.0, "solar_factor": 1.0, "pressure_hpa": 1016.5,
                      "leaf_wetness_pct": 0.0}
        elif self.state == "storm":
            gust = 0.5 + 0.5 * math.sin(self.t / 9.0 + self.gust_phase)
            target = {**stable, "air_temperature_c": base["temperature_c"] - 5.5, "air_humidity_pct": 96.0,
                      "soil_moisture_pct": 56.0, "rain_rate_mm_h": 34.0 + 16.0 * gust,
                      "wind_speed_kmh": 40.0 + 22.0 * gust, "solar_factor": 0.12, "pressure_hpa": 997.0,
                      "leaf_wetness_pct": 100.0}
        else:
            target = stable
        if self.irrigation_active() and self.irrigation_start is not None:
            if self.t - self.irrigation_start >= IRRIGATION_DEAD_TIME_S:
                target["soil_moisture_pct"] = max(target["soil_moisture_pct"], 44.0)
            target["leaf_wetness_pct"] = max(target["leaf_wetness_pct"], 55.0)
            target["air_humidity_pct"] = max(target["air_humidity_pct"], stable["air_humidity_pct"] + 4.0)
        return target

    def _tau(self, name: str) -> float:
        tau = TAU[name]
        if name == "soil_moisture_pct" and self.irrigation_active():
            tau = 9.0  # sprinklers wet the soil quickly
        elif name == "soil_moisture_pct" and self.state == "storm":
            tau = 16.0
        elif name == "air_humidity_pct" and self.irrigation_active():
            tau = 11.0
        return tau

    def step(self, dt: float, advance_clock: bool = True) -> None:
        if advance_clock:
            self.t += dt
        if self.irrigation_end is not None and self.t >= self.irrigation_end:
            self.events.append("irrigation finished")
            self.irrigation_start = self.irrigation_end = None
            self.irrigation_duration = 0
        target = self.targets()
        for name, value in target.items():
            self.x[name] += (value - self.x[name]) * (1.0 - math.exp(-dt / self._tau(name)))
        for name, (sigma, corr) in NOISE.items():
            n = self.noise[name]
            self.noise[name] = n - n * dt / corr + sigma * math.sqrt(2 * dt / corr) * self.rng.gauss(0.0, 1.0)
        wind_target = 220.0 if self.state == "storm" else 135.0
        self.wind_dir += ((wind_target - self.wind_dir + 180) % 360 - 180) * (1 - math.exp(-dt / 20.0))
        if self._day_index() != self.day:
            self.day, self.rain_today = self._day_index(), 0.0
        self._compute_readings()
        self.rain_today += self.readings["rain_rate_mm_h"] * dt / 3600.0
        self._update_alerts()

    def _compute_readings(self) -> None:
        x, n = self.x, self.noise
        base = diurnal(self.hour())
        solar = max(0.0, base["solar_w_m2"] * min(1.05, max(0.0, x["solar_factor"] + n["solar_factor"])))
        rain = max(0.0, x["rain_rate_mm_h"] + n["rain_rate_mm_h"] * max(1.0, x["rain_rate_mm_h"]) * 0.6)
        if rain < 0.05:
            rain = 0.0
        wind = max(0.0, x["wind_speed_kmh"] * (1.0 + n["wind_speed_kmh"]))
        self.readings = {
            "air_temperature_c": round(x["air_temperature_c"] + n["air_temperature_c"], 1),
            "air_humidity_pct": int(round(min(100.0, max(5.0, x["air_humidity_pct"] + n["air_humidity_pct"])))),
            "soil_moisture_pct": round(min(60.0, max(5.0, x["soil_moisture_pct"] + n["soil_moisture_pct"])), 1),
            "rain_rate_mm_h": round(rain, 1),
            "rain_today_mm": round(self.rain_today, 1),
            "wind_speed_kmh": round(wind, 1),
            "wind_direction_deg": int(round(self.wind_dir + n["wind_direction_deg"])) % 360,
            "solar_radiation_w_m2": int(round(solar)),
            "uv_index": round(solar / 85.0, 1),
            "pressure_hpa": round(x["pressure_hpa"] + n["pressure_hpa"], 1),
            "leaf_wetness_pct": int(round(min(100.0, max(0.0, x["leaf_wetness_pct"] + n["leaf_wetness_pct"])))),
        }

    def _outside(self, name: str, alert: str) -> bool:
        """Threshold check with hysteresis, so an alert does not flicker around its threshold."""

        value, limit, margin = self.readings[name], THRESHOLDS[name], HYSTERESIS[name]
        active = alert in self.alerts_active
        if "min" in limit:
            return value < limit["min"] + (margin if active else 0.0)
        return value > limit["max"] - (margin if active else 0.0)

    def _update_alerts(self) -> None:
        now_active = {alert for alert, name in (("dry_soil", "soil_moisture_pct"), ("low_humidity", "air_humidity_pct"),
                                                ("excess_rain", "rain_rate_mm_h")) if self._outside(name, alert)}
        for alert in sorted(now_active - self.alerts_active):
            self.events.append(f"alert raised: {alert}")
        for alert in sorted(self.alerts_active - now_active):
            self.events.append(f"alert cleared: {alert}")
        self.alerts_active = now_active

    def alerts(self) -> list[dict]:
        r = self.readings
        out = []
        if "dry_soil" in self.alerts_active:
            out.append({"id": "dry_soil", "severity": "critical" if r["soil_moisture_pct"] < 22 else "warning",
                        "text": "Soil moisture below 30 % — irrigation recommended", "action": "irrigation"})
        if "low_humidity" in self.alerts_active:
            out.append({"id": "low_humidity", "severity": "warning",
                        "text": "Air humidity below 40 % — high evapotranspiration", "action": None})
        if "excess_rain" in self.alerts_active:
            out.append({"id": "excess_rain", "severity": "critical" if r["rain_rate_mm_h"] > 40 else "warning",
                        "text": "Rain rate above 20 mm/h — runoff and erosion risk", "action": None})
        return out

    def document(self) -> dict:
        """The contract's weather JSON object."""

        return {"v": 1, "type": "weather", "station": self.station, "state": self.state,
                "readings": dict(self.readings), "thresholds": THRESHOLDS, "alerts": self.alerts(),
                "irrigation": {"active": self.irrigation_active(), "remaining_s": self.irrigation_remaining()}}

    def pop_events(self) -> list[str]:
        events, self.events = self.events, []
        return events
