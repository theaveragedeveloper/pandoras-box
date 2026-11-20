# Search Hub — one search box for the whole offline library

The search hub for **Pandora's Box Mk1** (the Dell 5424). One query searches every
offline source on the laptop — Wikipedia / WikiMed (Kiwix), field manuals
and PDFs (Recoll), places (offline maps), the owner's own files, and repeaters
& NOAA frequencies — and the results merge on one page, grouped by source.
See `COVERAGE.md` for the full table: every resource in the manifest and
the resource pack, whether the one search box reaches it, and what is
still needed at setup.

- **Runs on the laptop only.** Python 3.10+, standard library only — no
  pip installs, no downloads, no outbound network calls, ever.
- **Also serves the phone** over the laptop's Wi-Fi hotspot (Windows
  Mobile Hotspot): the phone joins the hotspot and opens the hub in its
  browser. Nothing to install on the phone.
- **Plug-in architecture.** Each source is a plug-in. New ZIM, new PDF
  folder, new tool — add a plug-in, don't rewrite the hub.
- **Honest limits.** Results are grouped by source, not one merged
  ranking: a map place and a manual passage can't be ranked against each
  other. A source that is down shows as unavailable instead of failing
  the whole search.

## Quick start (on the 5424)

1. Copy this whole `search-hub` folder onto the laptop, e.g. to
   `C:\Library\search-hub\` (any folder works).
2. Double-click `run_hub.bat`. It starts kiwix-serve first (if installed
   and the library exists), waits for port 8080, then starts the hub.
   The hub binds **localhost-only by default** (127.0.0.1:8899). To
   serve the phone over the hotspot, run `run_hub.bat --serve`
   (binds 0.0.0.0) — or use `scripts\serve_toggle.ps1 -Start`, which
   also switches to the Performance plan and checks the hotspot network
   is Private. First serving run: Windows Firewall will ask — allow it
   on **private** networks so the hotspot can reach the hub.
3. Open http://localhost:8899/ in Edge/Firefox. The demo query
   "tourniquet" runs on load. With the library up it searches the real
   ZIMs; before that the Kiwix and Recoll sources report unavailable
   (sample data is OFF by default — see "Demo mode").
4. Stop it with Ctrl+C in the console window.

Setup scripts (in `scripts/`, run once at setup — see each file's header):
- `build_recoll_index.bat` — builds the Recoll index, plugged in. Copies
  the `fixtures/recoll.conf.template` if no recoll.conf exists yet, and
  refuses to build until `topdirs` names folders to index.
- `hash_library.ps1` — records (`-Write`) and checks (`-Verify`) SHA-256
  hashes of the library, so a restore can be proven good.
- `setup_hygiene.ps1` — excludes the library folders from Windows Search
  indexing, pauses Windows Update for 7 days, and sets a durable offline
  posture (metered connection + notify-before-download). Run as
  Administrator, AFTER the library folders exist — it fails loudly if
  a folder is missing. Re-run after the library download.
- `serve_toggle.ps1` — `-Start` switches to the Performance plan, starts
  the Mobile Hotspot, and checks the hotspot network is Private;
  `-Stop` stops the hotspot and switches back to the Field plan.
- `purge_fixtures.ps1` — moves the sample fixture docs out of the
  searchable tree into `fixtures/_archived-samples/` once the real docs
  folder exists and the hub config points at it.
- `install_hub_autostart.ps1` — starts the hub at logon via Task
  Scheduler (run as Administrator; `-DesktopShortcut` also adds a
  desktop shortcut). It registers `run_hub.bat`, which also starts
  kiwix-serve first.

To try it right now (any machine with Python 3):

```bat
python hub.py --host 127.0.0.1 --port 8899
```

or on Linux/macOS: `./run_hub.sh`. Then open http://127.0.0.1:8899/.

Run the tests with `python3 tests/smoke_test.py`. The suite uses local fixtures.

### Check the library offline

Disconnect Wi-Fi on the laptop, open **Settings → Offline readiness**, and run
the checks. Choose the installed map archive and a term that occurs in both a
local ZIM article and an indexed PDF. The check reads the article and PDF header,
fetches a tile, loads the radio list, and reads saved pins. A sample result cannot
pass the article or PDF check. The last successful check times are saved in that
browser.

Use **Open PDF** in search results to jump to the matching page when Recoll
supplies a page number. Use **Bookmark** beside an article, document, saved pin,
or radio entry to keep it in **Quick access** on the home page.

### Tune SDR++ from the hub

On the laptop, open SDR++ once. In its **Rigctl Server** module, set **Host**
to `127.0.0.1` and **Port** to `4532`, select the receive VFO, enable tuning,
and enable **Listen on startup**. To let a frequency click start SDR++ when it
is closed, set `sdrpp_executable` in the laptop's `config.json` to the absolute
path of `sdrpp.exe` and restart the hub. If you chose another port, set
`sdrpp_rigctl_port` to the same port in the hub configuration.

Open the hub using its localhost URL on the laptop and select a frequency.
The hub confirms the new frequency with SDR++. Use **Search** in the radio
list for frequency details. Start the receiver in SDR++ when you want audio.

## Demo mode

Sample data is **OFF by default** in the shipped `config.json`
(`"demo_fallback": false`). Before the library exists, Kiwix and Recoll
simply report unavailable — they do not serve sample results, because a
sample medical result must never be mistaken for the real library.

To try the UI with sample data before the library exists, run the hub
with `--demo` (or set `"demo_fallback": true` in `config.json`). Sample
results are tagged **sample** in the UI and the sources rail shows them
amber ("demo"). `--no-demo` forces it off. Maps and My Files already run
on real local data (the gazetteer and the sample docs folder).

## Reliability (round 9, 2026-11-14)

The hub runs on one used drive in a battery-powered laptop in the
field; when a search is run it must answer. The design, in order of
what saves the owner:

- **Safe-mode startup.** A corrupt or unreadable `config.json` never
  stops the hub: the bad file is backed up to
  `config.json.corrupt-<timestamp>`, the last-known-good copy
  (`config.json.good`, written after every successful save) is
  restored if it exists, and otherwise the hub starts from built-in
  defaults (the five shipped sources, demo off). It starts and serves
  even with zero backends available — search returns empty groups,
  not an error page.
- **Graceful degradation.** Sources are queried in parallel with a
  shared 30 s search deadline (10 s for status). A dead, slow, or
  garbage-returning backend is marked unavailable in the rail and the
  other sources still answer; a plug-in that raises, returns a
  non-list, or has a failing `status()` cannot kill a search or the
  server. At most 4 searches run at once — a 5th gets HTTP 503
  ("hub busy, try again") instead of queuing and saturating the CPU.
- **Port fallback.** If 8899 is busy at startup the hub takes the next
  free port up to 8908 and writes the actual port to `hub.port` in the
  hub folder. `serve_toggle.ps1` and the watchdog read that file, so
  they never knock on the wrong port.
- **Watchdog + autostart.** Two layers, both logon tasks:
  - `install_hub_autostart.ps1` registers the hub to start at logon
    with restart-on-failure (3 tries, 1 minute apart) and no execution
    time limit, launching `run_hub.bat --no-pause` so the task sees
    the hub's real exit code.
  - `scripts/watchdog.ps1` polls `http://127.0.0.1:<port>/api/status`
    every 30 s and restarts the hub after 3 consecutive failed checks
    (one miss is noise — a reboot, a slow Recoll query). Install it
    with `.\scripts\watchdog.ps1 -Install` (Administrator); it logs
    to `scripts/watchdog.log` and refuses to run twice (named mutex).
    `.\watchdog.ps1 -Once` does a single check, for testing.
  The autostart task catches a hub that *exits*; the watchdog catches
  one that *hangs or is killed* without exiting.
- **Resource limits.** Kiwix search pages are read with a 5 MB cap
  (over it, the search fails cleanly); PMTiles are served in 8 MiB
  chunks (a Range request is still honoured); POST bodies are capped
  at 64 KiB; Recoll's own 20 s timeout sits inside the 30 s search
  deadline; My Files skips non-regular files (a FIFO can never block
  a search).
- **Data files reload without a restart.** The frequencies file and
  the gazetteer are re-read when their modification time changes, so
  the real repeater export swapped in at setup takes effect on the
  next search.
- **Logging.** Startup failures, backend failures, and config
  problems all land in `hub.log` with timestamps, and logging itself
  never raises — a bad log message cannot kill the hub.

What this does NOT cover (still needs the laptop): real kiwix-serve /
Recoll backends, the real Task Scheduler task, a real power loss, and
real Windows — the smoke test (157 checks, `python3
tests/smoke_test.py`) proves the code survives the faults on this
machine, not that the Windows pieces work. Run section 7 of
`INTEGRATION_CHECKLIST.md` on the 5424.

## Wiring up the real backends (on the laptop, plugged in)

### 1. Kiwix — Wikipedia, WikiMed, iFixit
Install Kiwix (Desktop or `kiwix-serve`), download the ZIMs, then start
the server so the hub can query it:

```bat
kiwix-serve --port 8080 --library C:\Library\kiwix\library.xml
```

Keep the ZIM set small and indexed: a few large ZIMs with pre-built
full-text indexes (Wikipedia nopic, WikiMed, Wiktionary). `kiwix-serve`
opens every ZIM at startup, so hundreds of small files cost 30–60 s and
battery on every launch. **Verify each ZIM actually searches** — some
ship without a full-text index and silently return nothing.

Point the hub at it in `config.json` (`kiwix` source → `base_url`).
Kiwix result links go through the hub itself (`/kiwix/...`, proxied to
kiwix-serve) so they work from the phone on the hotspot too — a direct
`127.0.0.1:8080` link would point at the phone. `run_hub.bat` starts
kiwix-serve before the hub, so both are up after a reboot.

### 2. Recoll — field manuals, Hesperian, USDA, vehicle manuals, PDFs
Install Recoll for Windows, point it at the library folders, and build
the index once (plugged in — indexing is a heavy one-time job). The
`scripts/build_recoll_index.bat` script does the build:

```bat
scripts\build_recoll_index.bat C:\Library\recoll-conf
```

The hub shells out to `recollq` per search — and falls back to
`recoll -t` (same behaviour) if `recollq` is not installed, because the
Recoll manual (1.45.0 §3.4) says recollq "is not always built or
installed by default" on Windows. The query uses `-A` so recollq
returns the abstract lines the hub shows as snippets. If neither is on
PATH, the source reports unavailable. The build script copies
`fixtures/recoll.conf.template` if no `recoll.conf` exists yet, and
refuses to build until `topdirs` names the folders to index — without
it, recollindex builds an index of nothing. Set `config_dir` in
`config.json` if the Recoll config lives somewhere unusual. After building
the index, record the date in `config.json`
(`library.recoll_index_date`) so the hub's status shows it — see
"Library snapshot dates" below.

### 3. Maps — places + the tile server
The Maps plug-in searches `fixtures/gazetteer-ne.json` — 301 New
England places (state capitals and county seats, cities and larger
towns, major hospitals incl. all Level 1 trauma centers, water bodies,
high points, airports, plus the Braintree-area landmarks and
trailheads). See `fixtures/gazetteer-ne.README.md` for the source note
and accuracy limits (town centers, ~1–3 km for smaller towns). To
extend it, add entries in the same JSON shape, or point the `gazetteer`
key at a CSV with columns `name,type,lat,lon,county,note` (e.g. a USGS
GNIS export).

For an actual map to look at (not just place names), open the offline
viewer at http://localhost:8899/map — it is a vendored MapLibre GL JS +
pmtiles.js page (`static/map.html`, libraries in `static/vendor/`, no
external resources) reading the Protomaps extract the hub serves at
`/tiles/ne.pmtiles` from `C:\Library\maps` (config `maps_dir`). Place
results link into it via `viewer_url_template` in `config.json`
(`/map?lat={lat}&lon={lon}&zoom=14`, hub-relative so it works from the
phone too). With no extract built yet the viewer says so instead of
failing silently. The style is a minimal one for Protomaps basemap v4
extracts (water, roads, buildings, place names, boundaries) — not the
full official basemap style.

The USGS topo quads get their own index when downloaded:
`fixtures/usgs-quads-ma.json` is the draft structure (quad name,
state, scale, bbox from the TNM Downloader export — never hand-typed);
the maps plug-in will read it to answer "which quad covers this place"
once the quads land.

See GAP_LIST.md — the printable-map and ATAK/Garmin pipelines are
setup-time steps documented in search-hub-gaps.md, not built yet.

### 4. My Files — the owner's own documents
Point the `myfiles` source's `folder` at the real docs folder
(family emergency plan, own notes, CHIRP exports). It searches
`.txt` / `.md` / `.html` contents and matches PDFs by file name only —
Recoll is the one that indexes PDF contents.

Known limits (documented, not fixed — the split is deliberate):
- A match needs all query tokens on a **single line**; a phrase split
  across two lines does not match.
- Results are the first 8 files in alphabetical order, not the best
  matches. For ranked full-text search over the same folder, use Recoll.
- Files over 5 MB are skipped (Recoll indexes big files instead).
- The folder is re-walked on every query — fine for a docs folder,
  not for a whole drive.

Once the real docs folder exists, run `scripts\purge_fixtures.ps1` to
move the sample fixture docs out of the searchable tree.

### 5. Frequencies — repeaters & NOAA weather radio
The `frequencies` plug-in (`plugins/frequencies.py`) searches a local
JSON file of radio frequencies — the New England repeater directory and
the seven NOAA weather-radio channels. It matches on name, kind,
location, note, and frequency, so "162.475" finds the Boston NOAA
channel and "146.52" finds a repeater on 146.520 MHz (frequencies are
normalised, trailing zeros dropped).

The shipped file is `fixtures/frequencies.json`. The seven NOAA rows are
**real**, verified against the NWS Boston transmitter table
(weather.gov/box/nwr, 2026-11-14): Boston KHB35 on 162.475 (Ch 4),
Gloucester 162.425, Worcester 162.550, Providence 162.400, Hyannis
162.550. The three repeater rows are **SAMPLE** — clearly labelled in
the results — because the real RepeaterBook / CHIRP export is
unverified until arrival. At setup, replace the sample rows with the
real export (same JSON shape: name, kind, frequency_mhz, tone,
location, note, sample) and re-run the smoke test. Point the
`frequencies` source's `data` key at the file in `config.json`.

## Serving over the hotspot (phone)

1. `scripts\serve_toggle.ps1 -Start` (elevated): switches to the
   Performance plan, starts the Mobile Hotspot, checks the hotspot
   network is classified **Private** (if it is Public, the firewall
   blocks the hub — the script prints the fix), and checks the hub is
   up. When done: `serve_toggle.ps1 -Stop` (hotspot off, back to Field).
2. The hub binds `0.0.0.0:8899` only in serving mode (`run_hub.bat
   --serve`); by default it binds localhost-only. On the phone, open
   `http://<laptop-hotspot-IP>:8899/` (find the IP with `ipconfig` on the
   laptop; the hotspot is usually 192.168.137.1). In serving mode the
   hub strips absolute filesystem paths from results sent to hotspot
   clients — the phone sees file names, not the laptop's tree.
3. Trade-off, stated plainly: serving needs Wi-Fi ON. That conflicts
   with the Field power plan's radios-off posture — serving is a
   deliberate mode, not the default. Tails sessions don't serve.

## Adding a new source plug-in

1. Copy `plugins/myfiles.py` to `plugins/<name>.py` and rename the
   class internals: set `id`, `name`, `badge`, `kind`.
2. Implement two methods (see `plugins/base.py` for the contract):
   - `search(self, query)` → list of dicts with `title`, `snippet`,
     `url`, `badge`, `detail`. Never raise for a backend problem —
     return `[]` and explain in `status()`.
   - `status(self)` → `{"available": bool, "detail": str}`.
3. Register it — either in `config.json` (add an entry to `"sources"`
   with `"plugin": "<name>"`) or from the UI: **+ Add a source** in the
   sources rail, pick the kind, paste the settings JSON.
4. Restart the hub, or just add it through the UI (the hub reloads the
   registry immediately). Test with `python3 tests/smoke_test.py` if you
   changed shared code.

Rules for plug-ins: stdlib only, no network beyond localhost, short
timeouts, cap results (`max_results`, default 8–10). A slow or broken
plug-in must never take the whole search down.

## API

| Route | What it does |
|---|---|
| `GET /` | The search UI |
| `GET /map` | The offline map viewer (vendored MapLibre + pmtiles.js) |
| `GET /tiles/<name>.pmtiles` | A Protomaps extract from `maps_dir`, with HTTP Range support |
| `GET /api/search?q=...` | JSON: `{query, groups: [{source_id, name, badge, available, note, results, ms}]}` |
| `GET /api/status` | Per-source availability for the rail |
| `GET /api/config` | The configured sources |
| `POST /api/sources` | Add a source (`{"name", "plugin", ...settings}`). **Localhost-only** — hotspot clients can search but not change the config. |

## Library snapshot dates

ZIM files are snapshots, not subscriptions — they go stale and Kiwix
does not update them incrementally. The hub shows the dates so
staleness is visible instead of silent: `/api/status` returns a
`library` block, and the UI rail shows it under "Library snapshots".
Fill it in at setup in `config.json`:

```json
"library": {
  "zim_snapshots": {
    "wikipedia_en_all_nopic": "2026-06",
    "wikimed_en_all": "2026-05"
  },
  "recoll_index_date": "2026-11-14"
}
```

Until then it reads "not set". The 6-month boot-check (see the setup
page) is when stale ZIM snapshots and the Recoll index get replaced.

## Power notes

- The hub itself is near-free: a few MB of RAM, idle CPU ~0. Kiwix
  idles at tens of MB. The costs are the screen and the backends, not
  the hub.
- Do indexing/downloads plugged in only. Never leave the hub's
  backends (kiwix-serve, Recoll) doing work on battery.
- Disable Windows Search indexing on the library folders (it will
  otherwise churn on battery trying to index hundreds of PDFs — Recoll
  is the indexer here). Pause Windows Update while offline.
  `scripts/setup_hygiene.ps1` does both (run as Administrator).

## Layout

```
hub.py               server + API + registry loading (binds localhost by
                     default; --serve binds 0.0.0.0 for the hotspot)
config.json          sources, host/port, demo flag, library snapshot dates,
                     maps_dir (edit by hand or via UI)
run_hub.bat / .sh    launchers (--serve passes through to hub.py)
scripts/
  build_recoll_index.bat    build the Recoll index (plugged in)
  setup_hygiene.ps1         Windows Search / Update hygiene (admin)
  serve_toggle.ps1          start/stop serving over the hotspot
  purge_fixtures.ps1        archive the sample fixture docs at setup
  install_hub_autostart.ps1 logon autostart task + desktop shortcut (admin)
  watchdog.ps1          polls /api/status, restarts the hub if it stops
                        answering (see "Reliability" below; -Install
                        registers it as a logon task)
plugins/
  base.py            plug-in contract
  kiwix.py           ZIM search via kiwix-serve (+ demo fixtures)
  recoll.py          PDF/doc search via recollq (falls back to recoll -t)
  maps.py            place search over the New England gazetteer
                     (+ USGS topo quad index: places name their quad)
  myfiles.py         plain-text search over a docs folder
  frequencies.py     repeater + NOAA weather-radio search (local JSON)
static/ui.html       the UI (amber-on-charcoal, no external resources)
static/map.html      the offline map viewer (vendored MapLibre + pmtiles.js)
static/vendor/       maplibre-gl.js/.css, pmtiles.js, Noto Sans glyphs
fixtures/            sample data: gazetteer, demo results, sample docs,
                     usgs-quads-ma.json (quad index), frequencies.json
                     (7 real NOAA rows + 3 SAMPLE repeater rows)
tests/smoke_test.py  end-to-end checks (see the count in the test output)
COVERAGE.md          one search box → every resource: the coverage table
INTEGRATION_CHECKLIST.md  setup-time checks against the real backends
GAP_LIST.md          what the platform still needs
```


## Map glyphs (not in the repo)

The map renders place labels from two binary glyph files that are not
stored here:

- `static/vendor/glyphs/Noto Sans Regular/0-255.pbf`
- `static/vendor/glyphs/Noto Sans Regular/256-511.pbf`

Without them the map still works, but labels do not render (the smoke
test skips the glyph check when they are absent). Fetch them from the
MapLibre demo font endpoint:

```
mkdir -p "static/vendor/glyphs/Noto Sans Regular"
curl -o "static/vendor/glyphs/Noto Sans Regular/0-255.pbf" \
  https://demotiles.maplibre.org/font/Noto%20Sans%20Regular/0-255.pbf
curl -o "static/vendor/glyphs/Noto Sans Regular/256-511.pbf" \
  https://demotiles.maplibre.org/font/Noto%20Sans%20Regular/256-511.pbf
```
