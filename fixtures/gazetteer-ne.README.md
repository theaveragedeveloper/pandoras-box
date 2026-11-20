# gazetteer-ne.json — New England place-names gazetteer for the Search Hub

301 places (Nov 13, 2026): every New England state capital and county seat,
cities and larger towns, major hospitals (incl. all Level 1 trauma centers),
major water bodies, high points, airports, plus the original 26 Braintree-area
entries (Blue Hills, local hospitals, water, trailheads).

## Source and accuracy

Curated by hand from USGS GNIS place names and public gazetteer coordinates.
This is a curated list, NOT a GNIS export — a full GNIS extract would be
thousands of entries and is not needed for "find a place" search.

- Coordinates are town/city centers (or the named feature's center), good
  to roughly 1–3 km for smaller towns — fine for finding a place on the
  map and routing to it, not for surveying.
- **Verified 2026-11-14 (round 2, map.geocode, relevance 100 unless
  noted):** all 100 Cities and all 134 Towns were checked at CITY level
  (12 towns had already been verified 2026-11-13: Montpelier, Augusta,
  Presque Isle, Houlton, Caribou, Rutland, Bennington, Brattleboro,
  St. Johnsbury, Keene, Laconia, Claremont). **36 coordinates were
  corrected** — 10 hospitals, 2 cities, 24 towns — each noted in the
  entry's `note` field with the old coordinates and the offset. Worst
  cases: Putnam CT 21.8 km (latitude typo), Wellesley MA 10.4 km
  (longitude typo), Hull MA 5.8 km (old point in the water), Easton MA
  6.0 km, Hanover MA 4.6 km, Windham CT 4.6 km, Greenwich CT 4.4 km,
  Essex VT 4.7 km, Braintree Highlands 4.2 km. Thresholds: town/city
  centers fixed if >2 km off; hospitals fixed if >~300–500 m from the
  verified main street address (hospitals were checked against their
  street address, not the bare name — bare-name queries return
  satellite clinics, e.g. BIDMC's Brighton site, Baystate's home
  infusion office).
- **Hospitals: all 17 verified** against street addresses; 10
  corrected, 7 confirmed as-is.
- **Airports: all 7 checked, none changed.** Stored points are
  airfield centers; the geocoder returns terminal buildings or adjacent
  POIs (and for Burlington it returned a nearby school, a wrong match),
  so the airfield-center points stand.
- **Peaks (10) and water bodies (24): checked against USGS GNIS
  2026-11-14 (round 3) — see below.**
- **Braintree local entries (11): all geocoder-checked 2026-11-14
  (round 3) — see below. 5 corrected, 6 kept.**
- Coverage tally after round 3: **286 entries verified** (17 hospitals,
  7 airports, 100 cities, 134 towns, 10 peaks, 13 bounded water bodies,
  5 Braintree local entries), **11 rivers kept as curated
  representative points** (GNIS single point noted below), **4 kept
  unverified** (Blue Hills Reservation, Braintree Municipal Golf
  Course, 2 trailheads). 301 total.

## Round 3 verification — 2026-11-14

### Braintree local entries (9, all geocoder-checked)
- **Corrected (3):** Braintree Town Hall → 42.20616,-71.00527
  (geocoder FULL_ADDRESS 1 JFK Memorial Dr; braintreema.gov listing
  agrees at 42.20603,-71.00501 — the stored point was 1.9 km off in
  Braintree Square); Massasoit Community College → 42.18378,-71.10106
  (900 Randolph St, Canton — stored point was 7.9 km off);
  CVS Pharmacy →
  42.22565,-71.00367 (178 Washington St, store #686, per CVS's own
  store locator and the geocoder — stored point was 0.5 km off in
  the Square).
- **Kept (6):** Great Blue Hill — GNIS-verified (0.05 km);
  Chickatawbut Hill — corrected to GNIS (see Peaks section, 3.7 km);
  Blue Hills Reservation — stored interior point kept (near Great
  Blue Hill); the geocoder's HQ anchor is 695 Hillside St,
  42.21405,-71.09397, 1.7 km away, and GNIS has no record for the
  reservation; Braintree Municipal Golf Course — stored point kept
  (on the course's north side, 100 m from 99 Rex Dr); clubhouse is
  101 Jefferson St, 42.19641,-71.01044; Blue Hills Trailhead —
  Ponkapoag — kept, unverified (geocoder returned nothing;
  reverse-geocode puts the point 227 m from 900 Randolph St, in the
  Ponkapoag area); Blue Hills Trailhead — Houghton's Pond — kept,
  unverified (geocoder returned the reservation HQ instead; the
  point is 179 m from 873 Hillside St, consistent with the pond's
  east-shore lot).

### Peaks (10) — all GNIS-verified
Checked against the official USGS GNIS DomesticNames state files
(prd-tnm.s3.amazonaws.com, downloaded 2026-11-14). **8 confirmed
as-is** (within 0.5 km of the GNIS summit): Mount Washington
(0.01 km), Mount Mansfield (0.03 km), Mount Greylock (0.05 km),
Mount Katahdin (0.02 km), Mount Cardigan (0.47 km), Mount Lafayette
(0.37 km), Mount Monadnock (0.04 km — GNIS official name is
"Monadnock Mountain"), Camel's Hump (0.03 km — GNIS spells it
"Camels Hump"). **2 corrected to GNIS:** Mount Ascutney →
43.4437,-72.4546 (0.65 km) and Mount Moosilauke → 44.0245,-71.8309
(0.51 km) — both stored points were rounded hand placements.

### Water bodies (24) — GNIS, two treatments
- **Bounded water (13): corrected to the GNIS feature point.**
  Houghton's Pond → 42.2071,-71.0959 (GNIS lists it as "Houghtons
  Pond", 0.65 km); Ponkapoag Pond → 42.1924,-71.095 (0.66 km); Blue
  Hills Reservoir → 42.228,-71.0495 (5.1 km — the reservoir is in
  the eastern Blue Hills, not near Great Blue Hill as stored);
  Quabbin Reservoir → 42.391,-72.3094 (8.7 km); Wachusett Reservoir
  → 42.3803,-71.7337 (4.8 km); Lake Winnipesaukee → 43.6145,-71.344
  (4.9 km); Moosehead Lake → 45.5416,-69.6669 (15.0 km; GNIS class
  is Reservoir); Sebago Lake → 43.8562,-70.5486 (2.7 km; GNIS class
  is Reservoir); Lake Champlain → 44.5912,-73.3063 (10.2 km);
  Narragansett Bay → 41.5703,-71.3342 (6.4 km); Boston Harbor →
  42.3418,-70.9662 (1.2 km; GNIS class is Bay); Cape Cod Bay →
  42.0334,-70.4161 (23.2 km); Buzzards Bay → 41.5626,-70.7481
  (6.0 km). Large water bodies are tens of miles across, so a
  hand-placed "center" was often kilometres from the official
  point; the GNIS point is the authoritative one.
- **Rivers (11): stored points kept as curated representative
  points.** A river is a line, not a point — GNIS lists a single
  point per river (usually near the mouth), which is no more "the"
  river than the stored point. The stored points sit on the river
  near the locally relevant stretch. GNIS points for reference:
  Neponset 42.3001,-71.0412; Monatiquot 42.2232,-70.9675; Fore River
  (GNIS: "Weymouth Fore River", Bay class) 42.2751,-70.9300;
  Connecticut (mouth) 41.2723,-72.3327; Merrimack (mouth)
  42.8195,-70.8120; Charles (mouth) 42.3709,-71.0495; Housatonic
  (mouth) 41.1693,-73.1084; Penobscot 44.4409,-68.8281; Kennebec
  (mouth) 43.7351,-69.7739; Androscoggin (mouth) 43.9506,-69.8775;
  Saco (mouth) 43.4618,-70.3783. Do not treat these river points as
  geocoder-verified; they are curated, with the source noted here.
- Same-name towns in different states (Plymouth, Salem, Manchester,
  Concord, Hudson, Franklin, Milford, Auburn, Bristol, Newport, Rockland,
  Hanover) carry the state in their `aliases` field, so "manchester nh"
  and "manchester ct" find different places. A bare "manchester" returns
  both, which is correct.

## Shape

JSON array of objects: `name`, `type` (Town / City / Hospital / Water /
Peak / Airport / Landmark / Trailhead / Pharmacy), `lat`,
`lon`, `county`, `note`, `aliases` (space-separated search tokens).

The Maps plug-in (`plugins/maps.py`) reads this file (config `gazetteer`
key). A CSV with columns `name,type,lat,lon,county,note` works too, e.g.
an export from USGS GNIS. To extend it, add entries in the same shape;
the hub picks them up on restart. The original 26-place Braintree demo
set lives on in `fixtures/places.json` for reference.
