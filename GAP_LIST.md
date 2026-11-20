# Search Hub / 5424 platform — gap list

Reviewed 2026-11-13: the setup plan (`offline-shtf-laptop-setup`), GOAL.md,
the resource pack, the arrival checklist, and the T.REX Map Server concept
(military-style topo, printable maps, ATAK/Garmin export). Each item has
one recommendation: **add now / later / skip**.

**Round-3 check 2026-11-14:** the gazetteer that backs the hub's Maps
plug-in is now fully verified — all 303 places checked (geocoder for
cities/towns/hospitals/airports/Braintree locals; USGS GNIS state files
for peaks and water). 21 coordinates corrected (5 Braintree local, 2
peaks, 13 bounded water, 1 hill); 11 rivers kept as curated
representative points with GNIS points recorded. Smoke test still
28/28 after the changes. Full record:
`search-hub/fixtures/gazetteer-ne.README.md`. Remaining asset sizes
also verified this round (FSI Spanish 1.46 GB / French 1.62 GB; Coast
Pilot 2 full volume 7,906,306 bytes; CDC checklists under 1 MB for the
two primary picks) — see the resource pack.

**Round-5 check 2026-11-14 (audit fixes):** smoke tests now
81/81. The hub binds localhost-only by default (`config.json` host
127.0.0.1; `--serve` binds 0.0.0.0) and strips absolute paths from
results for hotspot clients; `save_config()` is atomic and the hub
logs to `hub.log`; the map viewer is built (see item 1); the TREX
curation list is in the resource pack; new docs: `files/tails-plan.md`,
`search-hub/INTEGRATION_CHECKLIST.md`, `files/storage-plan.md`
(recovery: rebuild-from-sources + rebuild manifest), `files/pandora-box-mk1-manifest.md`
(decided resources only), `files/platform-audit.md` (16 flaws, 12 missing-asset
proposals, 5 redundancies).

**Round-9 check 2026-11-14 (reliability — "once this is active it
cannot fail"):** smoke tests now 157/157. Config safe mode (corrupt
file backed up, last-known-good copy restored, else built-in
defaults; zero backends still serves empty groups); parallel fan-out
with a 30 s search deadline and a 4-concurrent-search limit (5th gets
503); port fallback to 8899–8908 with the actual port in `hub.port`;
watchdog (`scripts/watchdog.ps1`, polls /api/status every 30 s,
restarts after 3 failed checks) + autostart task restart-on-failure
(3 tries, 1 min apart, no time limit, `--no-pause`); kiwix 5 MB page
cap; tiles in 8 MiB chunks; frequencies/gazetteer reload by mtime;
`serve_toggle.ps1` reads the port from `hub.port` and bounds its
tethering waits at 30 s. One real bug caught by the new fault-injection
tests: a plug-in whose `search()` raised was still reported
*available* because `status()` overwrote the error — fixed, the error
now sticks. Faults the smoke test still cannot prove on this machine:
real kiwix-serve/Recoll, the real Task Scheduler task, real power
loss, real Windows — see `INTEGRATION_CHECKLIST.md` §7, to run on
the 5424.

**Round-8 check 2026-11-14 (one search box, all resources):** the
frequencies plug-in is BUILT (item 9) and the quad index is WIRED into
the maps plug-in (item 2); the coverage table is `search-hub/COVERAGE.md`
(every manifest + resource-pack resource, connected or not, and why).
Smoke tests now cover the fan-out (one search → a group per configured
source). See the new test count in `tests/smoke_test.py` output.

## Add now

### 1. Map viewer / tile server — not chosen, not built
The hub's Maps plug-in returns places and coordinates, but there is no
map to look at: `viewer_url_template` is empty. "My own maps available"
(the owner, 2026-11-13) needs a local renderer over the PMTiles extract.
**Pick one:** TileServer-GL (heavier, full styling) or a static
PMTiles viewer (lighter, a single HTML file + the .pmtiles). For this
machine, the static viewer wins on battery — no live re-rendering on
pan/zoom. Then download the US Northeast PMTiles extract and set
`viewer_url_template`.
**Recommendation: add now** — it is the core of the maps requirement.
**Round-5 check 2026-11-14:** the offline map viewer is BUILT —
`static/map.html` with vendored MapLibre GL JS v4.7.1 + pmtiles.js
v3.2.0 + Noto Sans glyphs (`static/vendor/`, no external resources),
served by the hub at `/map`, the extract at `/tiles/<name>.pmtiles`
from config `maps_dir` (HTTP Range support). `viewer_url_template`
is now the hub-relative `/map?lat={lat}&lon={lon}&zoom=14`. The style
is a minimal one for Protomaps basemap v4 extracts. What remains is
the extract itself (`pmtiles show` to measure it) — a setup-time step.

### 2. USGS topo quad index
The plan has "USGS 7.5-minute topo quads for New England" as a download,
but no way to find which quad covers a place, and the quads aren't
downloaded yet. Add a quad lookup to the Maps plug-in (a second data
file: quad name → bounding box), so a place result can say "covered by
the Braintree quad" and open it.
**Recommendation: add now** — cheap once the quads are downloaded; this
is the TREX-style topo half of the maps pillar.
**Round-5 check 2026-11-14:** the index structure is drafted —
`fixtures/usgs-quads-ma.json` (quad name, state, scale, bbox from the
TNM Downloader export — never hand-typed; two real quad names,
Braintree and Boston South, bboxes null until the download fills
them). The maps plug-in does not read it yet; wiring it in is a
setup-time step once the quads land.
**Round-8 check 2026-11-14:** WIRED — `plugins/maps.py` now reads the
quad index (cfg `quads`): a place result names the quad that covers it
once bboxes exist, and searching a quad name ("braintree quad") returns
the quad itself (bbox pending download until the TNM export fills it
in). Quads with null bboxes are findable by name but cannot annotate a
place yet. What remains is the download itself + filling the bboxes
from the export.

### 3. ZIM full-text index verification
Research flagged that many ZIMs ship *without* a full-text index and
silently return nothing on search. The hub's Kiwix plug-in can't tell
"no match" from "this ZIM can't search". On arrival, verify each ZIM
actually searches (search a known word in each one) before trusting the
library. Longer term, the Kiwix plug-in could surface per-ZIM
searchability in `/api/status`.
**Recommendation: add now** — a setup-time check, not code; do it while
loading the library.
**Round-2 check 2026-11-14:** confirmed in the Kiwix catalog that the
Khan Academy English ZIM (2023-03-01 snapshot, ~34.3 GB) ships with
`_ftindex:no` — no full-text index, so it would silently return nothing
on hub search even though it loads. Any ZIM added from the
additional-assets list must pass this same check at setup; Khan Academy
is browse-only unless a later snapshot adds an index.

### 4. Recoll index build script
The Recoll index is a manual plugged-in job with no documented command
in the plan. If the index is stale or missing, manual/PDF results
silently vanish and nothing says why. Write the one-liner down
(`recollindex -c <conf>` over the library folders) and re-run it
whenever folders change. The hub already reports "recollq not found";
it should also warn when the index is older than the folders — a
small later improvement.
**Done 2026-11-13:** `scripts/build_recoll_index.bat` (plugged-in
build, checks recollindex on PATH, reminds you to record the build
date in config.json). Also resolved the same day: the Recoll manual
(1.45.0 §3.4) says recollq "is not always built or installed by
default" on Windows — the hub now falls back to `recoll -t`, which
behaves identically. **Round-4 update 2026-11-14:** the script now
copies `fixtures/recoll.conf.template` if no recoll.conf exists and
refuses to build until `topdirs` names folders to index (an index of
nothing is the failure mode this guards); the hub's query now passes
`-A` so snippets come back. The remaining piece is the setup-time check:
confirm which binary is present on the 5424 and that the index date
is recorded.

### 5. Windows Search / Update hygiene on the library folders
The low-power research is explicit: Windows Search indexing a huge PDF
folder burns CPU on battery, and Windows Update will churn offline.
Disable Windows Search indexing on the library folders (Recoll is the
indexer) and pause updates while offline. This is a setup step, not
code, and it belongs on the arrival/setup checklist.
**Done 2026-11-13:** `scripts/setup_hygiene.ps1` (excludes the library
folders via the CrawlScopeManager API with a manual fallback, pauses
updates 7 days, prints the Field-plan hotspot reminder). Run as
Administrator at setup; verify in Settings afterwards.
**Round-5 update 2026-11-14 (audit F6):** the script now sets a durable
offline posture — Windows Update "notify before download" (AUOptions=2)
and Ethernet marked metered (DefaultMediaCost), plus an optional
`-MeteredWifiSsid` — and it FAILS (exit 1) if a library folder is
missing instead of skipping quietly. New order: create the folders
(step 6), run hygiene, download the library (step 7), re-run hygiene
to confirm the exclusions took. `scripts/serve_toggle.ps1` is the
serve/stop mechanism (audit F8).

### 6. Recovery — rebuild from sources (there is no second copy)
ZIM snapshots go stale and Kiwix doesn't update them incrementally;
the research recommends two copies plus a check-drive habit — **but
round 7 (2026-11-14) decided the WD Blue 2TB is the only storage.**
There is no second copy. The 2TB drive decision is **settled
2026-11-13: WD Blue 2TB 2.5" SATA, bought ($159.99 used, 100% health,
LegacyTechno)** — it is the single internal drive and the only
storage. Recovery is rebuild-from-sources: `files/storage-plan.md` §2
defines it (health-check the WD Blue before Dec 8 — the gate;
two installer USBs; `files/rebuild-manifest.md` as the mechanical
rebuild list; `scripts\hash_library.ps1` for in-place hash
verification, with `hashes.txt` kept off the laptop on the Mac mini).
The hub shows library snapshot dates (ZIM dates in status) so
staleness is visible — see item 10. Remaining: fill in the rebuild
manifest at download time and rehearse one re-download before Dec 8.

## Later

### 7. Printable maps (the TREX headline feature)
The video's core promise is printing waterproof topo maps of any area.
The plan has "printed critical pages" as paper, but no print path: no
tool that turns a map area into a print-ready PDF, no printer, no
waterproof paper. Once the tile server + quads exist (items 1–2), add
a "print this area" export from the viewer (CalTopo-style PDF) and the
physical supplies.
**Recommendation: later** — needs items 1–2 first, plus a printer call.
**Round-5 note 2026-11-14:** the viewer (item 1) and the quad index
structure (item 2) now exist; the print pipeline itself is a
setup-time step — documented in search-hub-gaps.md: from the viewer,
export the visible area (browser print-to-PDF of the map page, or the
USGS GeoPDF itself printed at 1:24,000) onto waterproof paper; no
CalTopo-style PDF export is built, and no printer is chosen yet.

### 8. ATAK / Garmin export
TREX's other headline feature. The plan mentions ATAK only in passing;
there is no ATAK install, no MBTiles export pipeline, and no confirmed
need — it depends on whether the owner actually runs ATAK on his phone.
MBTiles export from the same PMTiles data is cheap once the tile
server exists, so this is a small job when/if the need is real.
**Recommendation: later** — decide when the phone side of the build
is scoped.
**Round-5 note 2026-11-14:** setup-time step, documented in
search-hub-gaps.md — from the same PMTiles data, export MBTiles
(`pmtiles` CLI can convert pmtiles → mbtiles) for ATAK, and a
Garmin-compatible .img via the OSM toolchain; no pipeline is built
and no ATAK install is in the plan until the owner confirms he runs ATAK
on his phone.

### 9. Repeater / frequency search in the hub
The repeater directory + NOAA weather frequencies live in CHIRP, not
in the hub. A tiny "frequencies" plug-in (CSV of repeaters + NOAA
channels, same shape as the gazetteer) would close the loop so a
frequency or repeater name is searchable from the same box.
**Recommendation: later** — blocked on the repeater export being
verified on arrival; a 30-minute plug-in once it exists.
**Round-8 check 2026-11-14: BUILT** — `plugins/frequencies.py` +
`fixtures/frequencies.json`, wired into `config.json` as a fifth
source ("Repeaters & Frequencies", badge Radio). The seven NOAA rows
are real, verified against the NWS Boston transmitter table
(weather.gov/box/nwr); the three repeater rows are SAMPLE, labelled
in the results, and must be replaced by the real RepeaterBook / CHIRP
export at setup (same JSON shape). The export verification is still
the setup-time step.

### 10. Library snapshot dates in hub status
Show ZIM dates and the Recoll index date in `/api/status` so staleness
is visible instead of silent. Small, but it needs the backends up
first.
**Done 2026-11-13:** `hub.status()` returns a `library` block
(`zim_snapshots` per ZIM id, `recoll_index_date`, both "not set" until
setup fills them in), and the UI rail shows them under "Library
snapshots". Fill in `config.json` "library" at setup; the dates are
only meaningful once the real ZIMs and index exist.

## Skip

### 11. Offline LLM answer layer in the hub
The plan mentions an optional Qwen 7B writing short answers from
retrieved passages. Skip it in the hub: it is slow on the i5-8350U
(a paragraph takes a minute or two), it burns battery, and small
models give wrong — sometimes dangerous — medical answers. The hub
is retrieval-first by design: search first, read the source. If an
LLM ever joins, it joins as a separate session tool, not as a hub
plug-in, and never as a medical oracle.
**Recommendation: skip** for the hub.

### 12. Typo tolerance / stemming in the hub
The hub does plain token matching; it has no fuzzy search. Skip
building it: Kiwix and Recoll already handle stemming and ranking
inside their own engines, and the hub is a fan-out, not a search
engine. If a query misses, the fix is a better query, not a new
ranker.
**Recommendation: skip.**

### 13. Hub authentication
The hub has no login, by design: it only ever runs on the laptop or
its own hotspot. The one write endpoint (`POST /api/sources`) is
already localhost-only, so hotspot clients can search but not change
the config. Adding accounts would be complexity for no threat-model
gain.
**Recommendation: skip.**
