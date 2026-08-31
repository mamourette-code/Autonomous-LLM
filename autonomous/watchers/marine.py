"""Marine conditions watcher: waves from Open-Meteo's marine API, wind from its
forecast API. Both are keyless.

It records one observation per forecast hour, so the feed builds a rolling
picture of conditions at your sailing location. Set MARINE_LATITUDE and
MARINE_LONGITUDE to enable it.
"""

from __future__ import annotations

import logging

import httpx

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.watchers.base import Observation, Watcher

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

log = logging.getLogger(__name__)


def _compass(degrees: float | None) -> str:
    if degrees is None:
        return "?"
    points = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    )
    return points[int((degrees % 360) / 22.5 + 0.5) % 16]


class MarineWatcher(Watcher):
    name = "marine"

    def __init__(self, settings: Settings, forecast_hours: int = 12) -> None:
        self.settings = settings
        self.forecast_hours = forecast_hours

    @property
    def interval_seconds(self) -> int:
        return self.settings.marine_poll_seconds

    @property
    def enabled(self) -> bool:
        return self.settings.marine_latitude is not None and (
            self.settings.marine_longitude is not None
        )

    async def poll(self) -> list[Observation]:
        s = self.settings
        coords = {"latitude": s.marine_latitude, "longitude": s.marine_longitude}
        async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
            waves = await client.get(
                MARINE_URL,
                params={
                    **coords,
                    "hourly": "wave_height,wave_period,wave_direction",
                    "forecast_days": 2,
                },
            )
            waves.raise_for_status()
            marine = waves.json()

            # Wind comes from a second service. If it is unreachable, still record
            # the wave data rather than losing the whole poll.
            try:
                wind_response = await client.get(
                    FORECAST_URL,
                    params={
                        **coords,
                        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                        "forecast_days": 2,
                    },
                )
                wind_response.raise_for_status()
                forecast = wind_response.json()
            except Exception as exc:
                log.warning("wind forecast unavailable, recording waves only: %s", describe(exc))
                forecast = {}

        hourly = marine.get("hourly", {})
        times: list[str] = hourly.get("time", [])[: self.forecast_hours]
        wind = forecast.get("hourly", {})
        wind_times: dict[str, int] = {t: i for i, t in enumerate(wind.get("time", []))}

        def at(series: list, index: int | None):
            if index is None or index >= len(series or []):
                return None
            return series[index]

        observations: list[Observation] = []
        for i, timestamp in enumerate(times):
            wave_h = at(hourly.get("wave_height", []), i)
            wave_p = at(hourly.get("wave_period", []), i)
            wave_d = at(hourly.get("wave_direction", []), i)
            wi = wind_times.get(timestamp)
            wind_s = at(wind.get("wind_speed_10m", []), wi)
            wind_d = at(wind.get("wind_direction_10m", []), wi)
            gust = at(wind.get("wind_gusts_10m", []), wi)

            summary = (
                f"{s.marine_location_name} {timestamp}: "
                f"wave {wave_h if wave_h is not None else '?'} m"
                f" @ {wave_p if wave_p is not None else '?'} s from {_compass(wave_d)}, "
                f"wind {wind_s if wind_s is not None else '?'} km/h {_compass(wind_d)}"
                f"{f' gusting {gust}' if gust is not None else ''}"
            )
            observations.append(
                Observation(
                    key=f"{s.marine_latitude},{s.marine_longitude}@{timestamp}",
                    title=summary,
                    data={
                        "time": timestamp,
                        "wave_height_m": wave_h,
                        "wave_period_s": wave_p,
                        "wave_direction_deg": wave_d,
                        "wind_speed_kmh": wind_s,
                        "wind_direction_deg": wind_d,
                        "wind_gusts_kmh": gust,
                        "location": s.marine_location_name,
                    },
                )
            )
        return observations
