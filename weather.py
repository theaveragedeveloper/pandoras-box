"""Cached NOAA weather forecast for the daily briefing.

The Command Center's weather tile shows today + tomorrow from the
National Weather Service (api.weather.gov, free, no key). The hub
fetches the forecast only when the owner taps Refresh — never on page
load — and caches it in weather.json next to hub.py, so the tile
keeps showing the last fetched forecast when the laptop is offline.
When no forecast has ever been fetched, the tile says so plainly;
the hub never invents weather.

Fetch flow (two calls, stdlib urllib only):
1. GET https://api.weather.gov/points/{lat},{lon} -> the forecast URL
   for that gridpoint + the place name (relativeLocation).
2. GET that forecast URL -> the 7-day periods.

Only the fields the tile shows are kept (name, start time, day/night,
temperature + unit, wind speed/direction, short forecast, chance of
precipitation), capped at 8 periods (today + tomorrow, day and night,
plus a day of slack so a day-old cache still covers tomorrow).

Storage: one JSON file, stdlib only. No new dependencies.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

# NOAA asks callers to identify themselves; without a User-Agent the
# API can refuse the request.
USER_AGENT = "PandoraBoxMk1/1.0 (offline hub; contact: local user)"
POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

# Maximum periods kept in the cache (today + tomorrow, day and night,
# plus a day of slack so a day-old cache still covers tomorrow).
MAX_PERIODS = 8

# Connection + read timeout per NOAA call (seconds). Two calls, so a
# refresh takes at most ~2x this when the network is down.
REQUEST_TIMEOUT = 15


class WeatherError(Exception):
    """The forecast could not be fetched (network down, bad response)."""


def clean_period(period):
    """Keep only the fields the briefing tile shows.

    A missing precipitation chance stays None ("unknown") instead of
    0 — the API sends null when a chance is not computed, and 0 would
    claim "no rain" where NOAA said nothing.
    """
    precip = period.get("probabilityOfPrecipitation")
    if isinstance(precip, dict):
        precip = precip.get("value")
    if precip is not None:
        try:
            precip = int(precip)
        except (TypeError, ValueError):
            precip = None
    return {
        "name": period.get("name") or "",
        "startTime": period.get("startTime") or "",
        "isDaytime": bool(period.get("isDaytime")),
        "temperature": period.get("temperature"),
        "temperatureUnit": period.get("temperatureUnit") or "",
        "windSpeed": period.get("windSpeed") or "",
        "windDirection": period.get("windDirection") or "",
        "shortForecast": period.get("shortForecast") or "",
        "probabilityOfPrecipitation": precip,
    }


def build_forecast(point_payload, forecast_payload, lat, lon, fetched_at=None):
    """Assemble the cached forecast from the two NOAA responses.

    fetched_at is the UTC ISO timestamp of the fetch; callers pass it
    in tests so the cache is deterministic.
    """
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc).isoformat()
    point = (point_payload or {}).get("properties") or {}
    relative = (point.get("relativeLocation") or {}).get("properties") or {}
    periods = ((forecast_payload or {}).get("properties") or {}).get("periods") or []
    if not isinstance(periods, list):
        raise WeatherError("The NOAA forecast had no periods.")
    return {
        "fetched_at": fetched_at,
        "location": {
            "city": relative.get("city") or "",
            "state": relative.get("state") or "",
            "lat": lat,
            "lon": lon,
        },
        "periods": [clean_period(p) for p in periods[:MAX_PERIODS]
                    if isinstance(p, dict)],
    }


def _get_json(url, urlopen):
    """One NOAA GET, decoded as JSON. Any failure is a WeatherError."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except WeatherError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WeatherError(f"Could not reach NOAA ({exc}).") from exc


def fetch_forecast(lat, lon, urlopen=None):
    """Fetch the cached-forecast dict for a lat/lon from NOAA.

    urlopen is injectable (tests pass a stub); otherwise
    urllib.request.urlopen. Raises WeatherError on any failure — the
    caller decides what the tile shows, never a half forecast.
    """
    opener = urlopen if urlopen is not None else urllib.request.urlopen
    point = _get_json(POINTS_URL.format(lat=lat, lon=lon), opener)
    forecast_url = (point.get("properties") or {}).get("forecast")
    if not forecast_url:
        raise WeatherError("NOAA did not return a forecast link for this location.")
    forecast = _get_json(forecast_url, opener)
    return build_forecast(point, forecast, lat, lon)


def _validate_cache(data):
    """A cache dict is only usable if it has the shape build_forecast makes."""
    if not isinstance(data, dict):
        raise ValueError("Weather cache is not a forecast.")
    if not isinstance(data.get("fetched_at"), str) or not data["fetched_at"]:
        raise ValueError("Weather cache has no fetch time.")
    if not isinstance(data.get("location"), dict):
        raise ValueError("Weather cache has no location.")
    if not isinstance(data.get("periods"), list):
        raise ValueError("Weather cache has no periods.")
    return data


class WeatherStore:
    """The cached forecast on disk (weather.json next to hub.py).

    read() returns the cache dict, or None when no forecast has ever
    been saved. A corrupt cache raises ValueError — the hub turns that
    into a 503, never a silently empty tile.
    """

    def __init__(self, cache_path):
        self.cache_path = cache_path

    def read(self):
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                return _validate_cache(json.load(fh))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise ValueError(f"Weather cache could not be read ({exc}).") from exc

    def refresh(self, lat, lon, fetcher=None):
        """Fetch a fresh forecast and atomically replace the cache.

        fetcher is injectable (tests pass a stub returning a cache
        dict); otherwise fetch_forecast. Returns the new cache dict.
        """
        fetch = fetcher if fetcher is not None else fetch_forecast
        data = _validate_cache(fetch(lat, lon))
        tmp_path = self.cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.cache_path)
        return data
