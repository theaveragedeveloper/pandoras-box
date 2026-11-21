"""Offline address geocoding for the Pandora's Box hub.

Pure-Python address normalisation, parsing and SQLite FTS5 search over an
address index built from an OpenStreetMap extract (see
scripts/build-address-index.py). No network, no external service: the index
lives on the laptop next to the rest of the library.

Index schema (built by build-address-index.py):
  addresses(housenumber, street_norm, street_raw, city, postcode, state,
            lat, lon, display)  -- UNIQUE(housenumber, street_norm, city, state)
  addresses_fts -- FTS5 external-content table over addresses
"""

import re

from sqlite_files import open_readonly

# Street suffixes and directionals, expanded so "Main St" and
# "Main Street" normalise to the same form. Only the LAST token of a street
# name is expanded as a suffix, and only a leading single letter as a
# directional, so a street actually named "St John" is not mangled.
SUFFIXES = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road",
    "dr": "drive", "ln": "lane", "ct": "court", "pl": "place",
    "blvd": "boulevard", "ter": "terrace", "cir": "circle",
    "hwy": "highway", "sq": "square", "pkwy": "parkway",
    "trl": "trail", "way": "way", "aly": "alley", "xing": "crossing",
    "ste": "suite", "apt": "apartment", "fl": "floor",
}
DIRECTIONALS = {"n": "north", "s": "south", "e": "east", "w": "west"}
_STREET_ENDINGS = SUFFIXES.keys() | set(SUFFIXES.values())

# Well-known name abbreviations people type instead of the full street
# name ("JFK Memorial Dr" for "John F. Kennedy Memorial Drive").
# Expanded in normalize_street, so index and query agree.
STREET_ALIASES = {
    "jfk": "john f kennedy",
    "mlk": "martin luther king",
    "fdr": "franklin d roosevelt",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[.,#]+")


def normalize_street(street):
    """Lowercase, strip punctuation, expand trailing suffix and leading
    directional. '123 Main St.' -> 'main street'; 'N Main St' -> 'north main street'."""
    if not street:
        return ""
    text = _PUNCT_RE.sub(" ", street.lower())
    tokens = [t for t in _WS_RE.split(text.strip()) if t]
    if not tokens:
        return ""
    # Expand well-known abbreviations (JFK -> John F. Kennedy).
    expanded = []
    for t in tokens:
        if t in STREET_ALIASES:
            expanded.extend(STREET_ALIASES[t].split())
        else:
            expanded.append(t)
    tokens = expanded
    if len(tokens) > 1 and tokens[0] in DIRECTIONALS:
        tokens[0] = DIRECTIONALS[tokens[0]]
    if tokens[-1] in SUFFIXES:
        tokens[-1] = SUFFIXES[tokens[-1]]
    return " ".join(tokens)


def normalize_token(text):
    """Lowercase, strip punctuation, collapse whitespace. For city names."""
    if not text:
        return ""
    text = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", text.strip())


def parse_query(query):
    """Split a free-text address into housenumber, street, city, state.

    Accepts '123 Main St', '123 Main St, Braintree', '123 Main St, Braintree, MA'.
    Returns dict with keys housenumber, street, city, state ('' when absent).
    """
    parts = [p.strip() for p in (query or "").split(",")]
    first = parts[0] if parts else ""
    m = re.match(r"^(\d+[A-Za-z]?)\s+(.*)$", first)
    if m:
        housenumber, street = m.group(1), m.group(2)
    else:
        housenumber, street = "", first
    city = parts[1] if len(parts) > 1 else ""
    state = parts[2] if len(parts) > 2 else ""
    # A trailing two-letter token on the city part may be the state.
    if city and not state:
        cm = re.match(r"^(.*?)\s+([A-Za-z]{2})$", city)
        if cm:
            city, state = cm.group(1), cm.group(2)
    return {
        "housenumber": housenumber.lower(),
        "street": normalize_street(street),
        "city": normalize_token(city),
        "state": state.upper().strip(),
    }


def _fts_escape(text):
    return text.replace('"', '""')


def _match_for(parsed):
    """FTS5 MATCH expression for the house number and street tokens."""
    clauses = []
    if parsed["housenumber"]:
        clauses.append('housenumber : "%s"' % _fts_escape(parsed["housenumber"]))
    for token in parsed["street"].split():
        clauses.append('street_norm : "%s"*' % _fts_escape(token))
    return " AND ".join(clauses)


def search(conn, query, limit=10):
    """Search the address index. Returns a list of dicts with display, lat,
    lon, housenumber, street, city, postcode, state. Empty list when the
    query has no usable street or housenumber."""
    parsed = parse_query(query)
    if not parsed["street"] and not parsed["housenumber"]:
        return []
    sql = (
        "SELECT a.housenumber, a.street_raw, a.city, a.postcode, a.state,"
        " a.lat, a.lon, a.display FROM addresses_fts f"
        " JOIN addresses a ON a.rowid = f.rowid"
        " WHERE addresses_fts MATCH ? AND (? = '' OR a.city = ?)"
        " AND (? = '' OR a.state = ?) LIMIT ?"
    )

    def match(address):
        return conn.execute(
            sql, (_match_for(address),
                  address["city"], address["city"], address["state"], address["state"], limit)
        ).fetchall()

    rows = match(parsed)
    if not rows and parsed["housenumber"] and "," not in query:
        tokens = parsed["street"].split()
        for split in range(len(tokens) - 1, 1, -1):
            if tokens[split - 1] not in _STREET_ENDINGS:
                continue
            street = " ".join(tokens[:split])
            town = " ".join(tokens[split:])
            rows = match(parse_query(f'{parsed["housenumber"]} {street}, {town}'))
            if rows:
                break
    out = []
    for hn, street_raw, city, postcode, state, lat, lon, display in rows:
        out.append({
            "housenumber": hn, "street": street_raw, "city": city,
            "postcode": postcode, "state": state, "lat": lat, "lon": lon,
            "display": display,
        })
    return out


def open_index(path):
    return open_readonly(path)
