"""Frequencies plug-in — repeater + NOAA weather-radio search.

This is the "Radio" source. It searches a local data file of radio
frequencies — the New England repeater directory (from the CHIRP /
RepeaterBook export) and the NOAA weather-radio channels — and returns
matches with frequency, tone, and location.

Data file (cfg "data", default fixtures/frequencies.json):
    {"entries": [{"name", "kind", "frequency_mhz", "tone",
                  "location", "note", "sample"}, ...]}
  kind is "repeater" or "noaa". Rows with "sample": true are sample
  data — they are labelled SAMPLE in the results and must be replaced
  by the real export at setup. The seven NOAA rows are real, verified
  against the NWS Boston transmitter table (weather.gov/box/nwr,
  2026-11-14); the repeater rows are sample-only until the
  RepeaterBook / CHIRP export is verified on arrival.

Matching is token-based: every query token must appear in the name,
kind, location, note, or the frequency. Frequencies are normalised
(trailing zeros stripped, "MHz" dropped) so "146.52" matches 146.520
and "162.475" matches a NOAA channel. No network — this is a lookup
over a local file, so it costs nothing on battery.
"""

import json
import os
import re

from plugins.base import PluginBase


def exact_frequency(query):
    frequency = re.fullmatch(r"\s*([0-9]{1,4}(?:\.[0-9]{1,6})?)\s*(MHz)?\s*",
                             query, flags=re.IGNORECASE)
    if frequency and ("." in frequency[1] or frequency[2]):
        return float(frequency[1])
    return None


def _norm_freq(value):
    """Normalise a frequency for matching: '146.520' -> '146.52'."""
    try:
        text = f"{float(value):.3f}"
    except (TypeError, ValueError):
        return ""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _norm_token(token):
    """Normalise a query token the same way a frequency is normalised."""
    token = token.lower().removesuffix("mhz").removesuffix("m")
    if token and all(ch.isdigit() or ch == "." for ch in token):
        return _norm_freq(token)
    return token


class Plugin(PluginBase):
    id = "frequencies"
    name = "Repeaters & Frequencies"
    badge = "Radio"
    kind = "frequencies"

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.data_file = self.data_path("data", "fixtures/frequencies.json")
        self._entries = None
        self._entries_mtime = None

    # -- data ------------------------------------------------------------
    def _load(self):
        # Reload if the file changed since the last load (round 9):
        # the real repeater export is swapped in for the sample rows at
        # setup, and the hub must pick it up without a restart. A
        # missing file caches as empty until it appears.
        try:
            mtime = os.path.getmtime(self.data_file)
        except OSError:
            mtime = None
        if self._entries is not None and mtime == self._entries_mtime:
            return self._entries
        entries = []
        try:
            with open(self.data_file, encoding="utf-8") as fh:
                data = json.load(fh)
            rows = data.get("entries", []) if isinstance(data, dict) else []
            for row in rows:
                # One bad row must not take the whole radio source down.
                if not isinstance(row, dict):
                    continue
                norm = self._normalise(row)
                if norm is not None:
                    entries.append(norm)
        except (OSError, ValueError, KeyError):
            entries = []
        self._entries = entries
        self._entries_mtime = mtime
        return entries

    @staticmethod
    def _normalise(row):
        freq = _norm_freq(row.get("frequency_mhz"))
        if not freq:
            # A frequency with no frequency is not a frequency.
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "kind": str(row.get("kind") or "repeater").strip().lower(),
            "frequency_mhz": freq,
            "tone": str(row.get("tone") or "").strip(),
            "location": str(row.get("location") or "").strip(),
            "note": str(row.get("note") or "").strip(),
            "sample": bool(row.get("sample", False)),
        }

    # -- contract --------------------------------------------------------
    def search(self, query):
        exact = exact_frequency(query)
        tokens = [_norm_token(t) for t in query.lower().split() if t]
        if not tokens:
            return []
        scored = []
        for entry in self._load():
            if exact is not None:
                if exact == float(entry["frequency_mhz"]):
                    scored.append((4, entry))
                continue
            hay = (
                entry["name"].lower()
                + " " + entry["kind"]
                + " " + entry["location"].lower()
                + " " + entry["note"].lower()
                + " " + entry["frequency_mhz"]
            )
            if not all(t in hay for t in tokens):
                continue
            if any(entry["name"].lower().startswith(t) for t in tokens):
                score = 3
            elif any(t in entry["name"].lower() for t in tokens):
                score = 2
            else:
                score = 1
            scored.append((score, entry))
        scored.sort(key=lambda se: (-se[0], se[1]["name"]))
        out = []
        for _, entry in scored[: self.max_results]:
            freq = f"{entry['frequency_mhz']} MHz"
            tone = entry["tone"]
            snippet = freq
            if tone and tone.lower() != "none":
                snippet += f" · Tone {tone}"
            if entry["note"]:
                snippet += f" — {entry['note']}"
            detail = entry["kind"].capitalize()
            if entry["location"]:
                detail += f" · {entry['location']}"
            if entry["sample"]:
                detail += " · SAMPLE — not a real repeater yet"
            out.append(
                {
                    "title": entry["name"],
                    "snippet": snippet,
                    "url": "",
                    "badge": "Radio",
                    "frequency_mhz": entry["frequency_mhz"],
                    "exact": exact is not None,
                    "sample": entry["sample"],
                    "detail": detail,
                    "demo": False,
                }
            )
        return out

    def status(self):
        entries = self._load()
        if entries:
            noaa = sum(1 for e in entries if e["kind"] == "noaa")
            repeaters = len(entries) - noaa
            sample = sum(1 for e in entries if e["sample"])
            detail = f"{len(entries)} frequencies ({noaa} NOAA, {repeaters} repeaters)"
            if sample:
                detail += f" · {sample} sample row(s) — replace with the real export"
            return {"available": True, "detail": detail}
        return {"available": False, "detail": "frequencies file not found or empty"}
