# Pandora's Box -- offline search hub

One search box for a whole offline library, on a Windows laptop. Type a
question once and it searches every offline source on the machine --
Wikipedia and repair guides (Kiwix), field manuals and PDFs (Recoll),
places and street addresses (offline maps), your own documents, radio
frequencies, and your supplies inventory -- and shows the results on one
page, grouped by source. It works with no internet connection, and it
can serve the library to a phone over the laptop's Wi-Fi hotspot.

This was built for a rugged field laptop, but it runs on any Windows 11
PC with enough disk. There are no accounts, no cloud services, and no
subscriptions. The code is Python's standard library only; the offline
content is downloaded separately (see "Recommended resources" below).

## What it does

- **One search, every source.** A single query searches Kiwix (ZIM
  files), Recoll (indexed PDFs and documents), the offline map
  (places and street addresses), your own files, and the frequency
  list. Results are grouped by source, not merged into one ranking.

  For ordinary searches, the hub tries your wording first. Kiwix and
  Recoll can also try a small set of known synonyms, abbreviations, and
  spelling variants. On the laptop, **Best match** quotes a short
  passage from a clearly matching article or indexed PDF page.
  Ambiguous searches fall back to the grouped results. These features
  use the existing local library and need no GPU.
- **Offline maps.** A vendored MapLibre viewer reads Protomaps vector
  extracts stored on disk. Dark, Light, Topo (USGS raster), and ATAK
  styles. Place search, street-address search (a US-wide address
  index), and saved pins.
- **Saved pins (houses and places).** Save a named place -- a house, a
  meeting point, a landmark -- with its address and coordinates. Pins
  are stored on the hub itself, not only in the browser, so they
  survive a browser data clear. A saved house shows distance and
  direction from a starting point, with a line on the map.
- **Supplies inventory.** Track stock by location and kit, count water
  and food in days for the household, flag items expiring soon, and
  build a shopping list from the gaps. Mark items used or restocked so
  the counts stay current.
- **Radio.** A local list of NOAA weather-radio channels and repeaters.
  Frequencies can tune SDR++ through its Rigctl server when SDR++ is
  running on the laptop.
- **Phone over the hotspot.** The laptop's Wi-Fi hotspot serves the hub
  to a phone browser. Nothing to install on the phone. Sharing is
  deny-by-default: choose what the phone may see in Settings, or it
  sees nothing.
- **Honest limits.** A source that is down shows as unavailable instead
  of failing the whole search. Results are grouped by source because a
  map place and a manual passage cannot be ranked against each other.

## Hardware

Runs on any Windows 11 PC that meets the disk and memory needs
below. It also runs on Linux or macOS for testing (see
`run_hub.sh`), but the setup scripts and the Recoll/kiwix-serve steps
in this README are written for Windows.

- **Disk:** plan for **at least 300 GB free** for the full library.
  The datasets add up to roughly 260 GB once downloaded and built
  (see "Recommended resources"), plus working space for downloads and
  the search-index build. A 500 GB drive is the practical minimum; a
  1 TB or larger drive is comfortable.
- **Memory:** 8 GB is enough to run the hub. Building the Recoll index
  and the address index is faster and smoother with 16 GB or more.
- **Software:** Python 3.12 (tick "Add Python to PATH" during install),
  Kiwix (Desktop or kiwix-tools for `kiwix-serve`), and Recoll for
  Windows. The hub itself needs no pip packages to run. Building the
  US address index needs `pip install osmium` (see below).

## Quick start

1. Clone or copy this whole `search-hub` folder onto the laptop, for
   example to `C:\Library\search-hub\` (any folder works; the rest of
   this README uses `C:\Library` as the recommended library location).
2. Download the offline resources (see "Recommended resources" below).
   The hub starts without them, but Kiwix and Recoll will report
   unavailable until their data is in place. You can start with just
   the small resources and add the large ones later.
3. Double-click `run_hub.bat`. It starts `kiwix-serve` first (if
   installed and the library exists), waits for port 8080, then starts
   the hub. The hub binds **localhost-only by default**
   (127.0.0.1:8899). To serve the phone over the hotspot, run
   `run_hub.bat --serve` (binds 0.0.0.0) -- or use
   `scripts\serve_toggle.ps1 -Start`, which also switches to the
   Performance plan and checks the hotspot network is Private. First
   serving run: Windows Firewall will ask -- allow it on **private**
   networks so the hotspot can reach the hub.
4. Open http://127.0.0.1:8899/ in Edge or Firefox. The demo query
   "tourniquet" runs on load. With the library up it searches the real
   ZIMs; before that the Kiwix and Recoll sources report unavailable
   (sample data is OFF by default -- see "Demo mode").
5. Stop it with Ctrl+C in the console window.

To try it right now (any machine with Python 3):

```bat
python hub.py --host 127.0.0.1 --port 8899
```

or on Linux/macOS: `./run_hub.sh`. Then open http://127.0.0.1:8899/.

Run the tests with `python3 tests/smoke_test.py`. The suite uses local
fixtures and prints the check count when it finishes.

Setup scripts (in `scripts/`, run once at setup -- see each file's
header):

- `setup-download-resources.ps1` -- creates the library folders and
  downloads the smaller resources; prints the steps for the large and
  manual ones. Start here.
- `build_recoll_index.bat` -- builds the Recoll index, plugged in.
  Copies `fixtures/recoll.conf.template` if no recoll.conf exists yet,
  and refuses to build until `topdirs` names folders to index.
- `build-address-index.py` -- builds the US street-address index from
  an OpenStreetMap extract (see "Address index" below). Needs
  `pip install osmium`. Takes about 2.5 hours for the full US file.
- `build-quad-index.py` -- builds the USGS topo-quad index
  (`fixtures/usgs-quads-ma.json`) from the National Map API.
- `hash_library.ps1` -- records (`-Write`) and checks (`-Verify`)
  SHA-256 hashes of the library, so a restore can be proven good.
- `setup_hygiene.ps1` -- excludes the library folders from Windows
  Search indexing, pauses Windows Update for 7 days, and sets a
  durable offline posture (metered connection +
  notify-before-download). Run as Administrator, AFTER the library
  folders exist -- it fails loudly if a folder is missing. Re-run
  after the library download.
- `serve_toggle.ps1` -- `-Start` switches to the Performance plan,
  starts the Mobile Hotspot, and checks the hotspot network is
  Private; `-Stop` stops the hotspot and switches back to the Field
  plan.
- `purge_fixtures.ps1` -- moves the sample fixture docs out of the
  searchable tree into `fixtures/_archived-samples/` once the real
  docs folder exists and the hub config points at it.
- `install_hub_autostart.ps1` -- starts the hub at logon via Task
  Scheduler (run as Administrator; `-DesktopShortcut` also adds a
  desktop shortcut). It registers `run_hub.bat`, which also starts
  kiwix-serve first.
- `watchdog.ps1` -- polls `/api/health` and restarts the hub if it
  stops answering (see "Reliability" below).

## Recommended resources

The code in this repo is the hub. The offline content is not in the
repo -- it is too large -- so you download it yourself. This table lists
every dataset the hub is built to use, where to get it, how big it is,
and what it is for. Sizes are approximate and grow as new snapshots
are published; pick the latest snapshot for each item.

| Resource | Source | Size | What it is for |
|---|---|---|---|
| Wikipedia, English, full with images (`wikipedia_en_all_maxi`) | Kiwix library -- https://library.kiwix.org or https://download.kiwix.org/zim/wikipedia/ | ~119 GB | General reference; the main Kiwix source |
| iFixit (`ifixit_en_all`) | https://download.kiwix.org/zim/ifixit/ | ~3.3 GB | Repair guides |
| Ham Stack Exchange (`ham.stackexchange.com_en_all`) | https://download.kiwix.org/zim/stackexchange/ | ~74 MB | Ham radio questions and answers |
| Drones Stack Exchange (`drones.stackexchange.com_en_all`) | https://download.kiwix.org/zim/stackexchange/ | ~12 MB | Drone questions and answers |
| T.REX Field Manual Vault (torrent) | https://www.trex-arms.com/field-manual-vault/download | ~108 GB download; ~97 GB unzipped | US military field manuals, doctrine, and technical publications (Recoll) |
| Protomaps US extract (`conus.pmtiles`) | Protomaps daily planet build -- https://build.protomaps.com (cut with the `pmtiles` CLI) | ~17.6 GB | The US map (default) |
| Protomaps regional extract (`ne.pmtiles`) | Protomaps daily planet build (cut with the `pmtiles` CLI) | ~1.2 GB | A smaller regional map; the shipped file covers New England as an example |
| USGS topo quads, GeoPDF | US National Map -- https://apps.nationalmap.gov/downloader/ (or the TNM Access API) | ~5.7 GB for 369 quads | Printable topo quadrangle maps |
| USGS topo raster tiles | Built from USGS topo (see `scripts/` notes) | ~188 MB | Topo map style |
| Hesperian health guides, chapter PDFs | https://hesperian.org/ | 46 chapter PDFs | Medical reference (Recoll and My Files) |
| US street-address index | Geofabrik US extract -- https://download.geofabrik.de/north-america/us-latest.osm.pbf -- plus `scripts/build-address-index.py` | 11.4 GB download; ~7.1 GB index database | Street-address search on the map |
| Map glyph files (2 small files) | https://demotiles.maplibre.org/font/Noto%20Sans%20Regular/ | under 1 MB | Map place labels (the map works without them, but labels do not render) |

Notes on the large items:

- **Wikipedia maxi (~119 GB).** Download it from the Kiwix library
  inside Kiwix Desktop, or by direct link / torrent from
  `download.kiwix.org/zim/wikipedia/`. Use a resumable download
  (the Kiwix app, aria2, or a torrent client). It is a dated snapshot;
  record the snapshot date in `config.json` (see "Library snapshot
  dates") and replace it every 6-12 months.
- **T.REX vault (~108 GB).** Shared peer-to-peer over BitTorrent.
  Get the `.torrent` from the download page above and open it in a
  torrent client (qBittorrent or similar). It contains the original
  PDFs, converted EPUBs, and text-only MD files. After downloading,
  verify integrity with `sha256sum -c SHA256SUMS --ignore-missing`
  (the `--ignore-missing` skips files you chose not to download).
  Unzip into the manuals folder and build the Recoll index (below).
  There is also a 21.8 GB single-file ZIM version of the vault on the
  same download page if you prefer to read it in Kiwix instead.
- **Protomaps extracts.** Protomaps publishes a daily planet build.
  Find a recent build date at https://maps.protomaps.com/builds/,
  then cut the area you need with the `pmtiles` CLI
  (github.com/protomaps/go-pmtiles -- a single binary, no account).
  It range-reads only the tiles in your area, so you do not download
  the whole planet:
  ```bat
  pmtiles extract https://build.protomaps.com/20261117.pmtiles conus.pmtiles --bbox=-125,24.5,-66.9,49.4 --maxzoom=15
  pmtiles extract https://build.protomaps.com/20261117.pmtiles ne.pmtiles --bbox=-73.73,40.98,-66.95,47.46 --maxzoom=15
  ```
  Replace the date with a recent one from the builds index. The
  second line cuts a regional extract (New England, as an example) --
  use your own area's bounding box. Put the
  finished `.pmtiles` files in the maps folder (`C:\Library\Maps`)
  and set `maps_dir` in `config.json`.
- **US address index.** Download the US extract from Geofabrik
  (11.4 GB), install the Python `osmium` package
  (`pip install osmium`), then run the build from the geocode folder
  (it writes temporary files into the current directory):
  ```bat
  cd C:\Library\geocode
  python C:\Library\search-hub\scripts\build-address-index.py us-latest.osm.pbf addresses.db
  ```
  This takes about 2.5 hours for the full US file and produces a
  ~7.1 GB database. Verify the download first: Geofabrik publishes
  an MD5 file alongside the extract (the plain `.md5` URL redirects,
  so fetch the dated checksum file).
- **USGS quads.** Download the GeoPDFs for your area from the
  National Map downloader (link in the table) into the quads folder,
  then run `scripts/build-quad-index.py` to build the hub's quad
  index (`fixtures/usgs-quads-ma.json`). The shipped index covers
  Massachusetts (369 quads, newest edition each) as an example --
  build one for your area with the same script.
- **Hesperian.** Download the chapter PDFs from hesperian.org (the
  "Where There Is No Doctor" family of titles) into the My Files
  folder. Recoll indexes them alongside the manuals.

`scripts/setup-download-resources.ps1` automates the small downloads
(glyphs, the small ZIMs) and prints the exact steps for the large and
manual ones. Run it first; it tells you what is left to do by hand.

## Wiring up the real backends (on the laptop, plugged in)

### 1. Kiwix -- Wikipedia, iFixit, Stack Exchange

Install Kiwix (Desktop or `kiwix-serve`), download the ZIMs, then
start the server so the hub can query it:

```bat
kiwix-serve --port 8080 --library C:\Library\kiwix\library.xml
```

Build the library file with `kiwix-manage` (part of kiwix-tools),
adding each ZIM:

```bat
kiwix-manage C:\Library\kiwix\library.xml add C:\Library\kiwix\wikipedia_en_all_maxi_2026-08.zim
```

Keep the ZIM set small and indexed: a few large ZIMs with pre-built
full-text indexes. `kiwix-serve` opens every ZIM at startup, so
hundreds of small files cost 30-60 s and battery on every launch.
**Verify each ZIM actually searches** -- some ship without a full-text
index and silently return nothing.

Point the hub at it in `config.json` (`kiwix` source -> `base_url`).
Kiwix result links go through the hub itself (`/kiwix/...`, proxied to
kiwix-serve) so they work from the phone on the hotspot too -- a direct
`127.0.0.1:8080` link would point at the phone. `run_hub.bat` starts
kiwix-serve before the hub, so both are up after a reboot.

### 2. Recoll -- field manuals, Hesperian, your PDFs

Install Recoll for Windows, point it at the library folders, and build
the index once (plugged in -- indexing is a heavy one-time job). The
`scripts/build_recoll_index.bat` script does the build:

```bat
scripts\build_recoll_index.bat C:\Library\recoll-conf
```

The hub shells out to `recollq` per search -- and falls back to
`recoll -t` (same behaviour) if `recollq` is not installed, because the
Recoll manual says recollq "is not always built or installed by
default" on Windows. The query uses `-A` so recollq returns the
abstract lines the hub shows as snippets. If neither is on PATH, the
source reports unavailable. The build script copies
`fixtures/recoll.conf.template` if no `recoll.conf` exists yet, and
refuses to build until `topdirs` names the folders to index -- without
it, recollindex builds an index of nothing. Set `config_dir` in
`config.json` if the Recoll config lives somewhere unusual. After
building the index, record the date in `config.json`
(`library.recoll_index_date`) so the hub's status shows it -- see
"Library snapshot dates" below.

For full-text search of scanned PDFs, Recoll needs a PDF text
extractor (pdftotext from Poppler) wired into its filters. Without it,
image-only PDFs are invisible to text search.

### 3. Maps -- places + the tile server

The Maps plug-in searches a gazetteer file -- the shipped
`fixtures/gazetteer-ne.json` covers New England (301 places: state
capitals and county seats, cities and larger towns, major hospitals
including Level 1 trauma centers, water bodies, high points, airports,
and trailheads) as an example. Swap in places for your own region. See
`fixtures/gazetteer-ne.README.md` for the source note and accuracy
limits (town centers, ~1-3 km for smaller towns). To extend it, add
entries in the same JSON shape, or point the `gazetteer` key at a CSV
with columns `name,type,lat,lon,county,note` (for example a USGS GNIS
export).

For an actual map to look at (not just place names), open the offline
viewer at http://127.0.0.1:8899/map -- it is a vendored MapLibre GL JS +
pmtiles.js page (`static/map.html`, libraries in `static/vendor/`, no
external resources) reading the Protomaps extract the hub serves at
`/tiles/conus.pmtiles` from the maps folder (config `maps_dir`).
Place results link into it via `viewer_url_template` in `config.json`
(`/map?lat={lat}&lon={lon}&zoom=14`, hub-relative so it works from the
phone too). With no extract built yet the viewer says so instead of
failing silently. The style is a minimal one for Protomaps basemap
extracts (water, roads, buildings, place names, boundaries) -- not the
full official basemap style.

The USGS topo quads get their own index when downloaded:
`fixtures/usgs-quads-ma.json` lists each quad (name, state, scale,
bbox). The maps plug-in reads it to answer "which quad covers this
place" once the quads land.

### 4. Address index -- street addresses on the map

The map's go-to box accepts real US street addresses ("350 5th Ave,
New York"). That is powered by the address index built with
`scripts/build-address-index.py` (see "Recommended resources" above).
The index lives at `C:\Library\geocode\addresses.db` and is queried
through `/api/geocode`. If the index is absent, address lookup simply
returns no results; place-name and coordinate lookup still work.

### 5. My Files -- your own documents

Point the `myfiles` source's `folder` at the real docs folder (your
notes, CHIRP exports, Hesperian PDFs). It searches `.txt` / `.md` /
`.html` contents and matches PDFs by file name only -- Recoll is the
one that indexes PDF contents.

Known limits (documented, not fixed -- the split is deliberate):
- A match needs all query tokens on a **single line**; a phrase split
  across two lines does not match.
- Results are the first 8 files in alphabetical order, not the best
  matches. For ranked full-text search over the same folder, use Recoll.
- Files over 5 MB are skipped (Recoll indexes big files instead).
- The folder is re-walked on every query -- fine for a docs folder,
  not for a whole drive.

Once the real docs folder exists, run `scripts\purge_fixtures.ps1` to
move the sample fixture docs out of the searchable tree.

### 6. Frequencies -- repeaters & NOAA weather radio

The `frequencies` plug-in (`plugins/frequencies.py`) searches a local
JSON file of radio frequencies -- a repeater directory and the seven
NOAA weather-radio channels. It matches on name, kind, location, note,
and frequency, so "162.475" finds a NOAA weather channel and
"146.52" finds a repeater on 146.520 MHz (frequencies are normalised,
trailing zeros dropped).

The shipped file is `fixtures/frequencies.json`. The seven NOAA rows
are the standard NOAA weather-radio channels. The
repeater rows are **SAMPLE** -- clearly labelled in the results -- until
a real RepeaterBook / CHIRP export is swapped in. At setup, replace
the sample rows with the real export (same JSON shape: name, kind,
frequency_mhz, tone, location, note, sample) and re-run the smoke
test. Point the `frequencies` source's `data` key at the file in
`config.json`.

### 7. Inventory -- supplies on hand

The Inventory tab tracks stock by location and kit, counts water and
food in days for the household, flags items expiring soon, and builds
a shopping list from the gaps. Use the "Use" action when you take
something out of stock and "Restock" when you add it, so the counts
stay current. Data is stored in a SQLite database on the hub. The tab
ships with a small clearly-labelled sample set -- replace it with your
real stock.

### 8. Pins -- saved houses and places

Saved pins live on the hub (server-side storage), not only in the
browser, so they survive a browser data clear. Save a house with a
name and an address; the map shows distance and direction to it from
a starting point. Pins can also be exported to a JSON file and
imported back as a backup.

## Check the library offline

Disconnect Wi-Fi on the laptop, open **Settings -> Offline readiness**,
and run the checks. Enter a known article query and a manual query.
The hub opens an article from each enabled Kiwix reader, checks the
bytes at both ends of each indexed PDF, and gives you a link to the
matched page when Recoll supplies one. Open the link to verify the
page in your browser's PDF viewer. Sample results cannot pass either
check.

Choose the vector archive and enter your usual coordinates. The check
loads Dark, Light, Topo, and ATAK through the offline map viewer and
reports coverage at that point. It also checks the radio list and
saved pins. To test the actual receiver, use **Settings -> Check SDR++
connection**. The latest successful checks are recorded in the browser.

From the main search box, try a manual ID such as `FM 3-05.70`, a
frequency such as `162.475 MHz`, or coordinates such as `40.7, -74.0`.
Use **Open matched page** to jump to the indexed passage. Use
**Bookmark** beside an article, document, saved pin, or radio entry to
keep it in **Quick access**.

For ordinary searches, the hub tries your wording first. Kiwix and
Recoll can also try a small set of known synonyms, abbreviations, and
spelling variants. On the laptop, **Best match** quotes a short passage
from a clearly matching article or indexed PDF page. Ambiguous searches
fall back to the grouped results. These features use the existing local
library and need no GPU.

### Tune SDR++ from the hub

On the laptop, open SDR++ once. In its **Rigctl Server** module, set
**Host** to `127.0.0.1` and **Port** to `4532`, select the receive VFO,
enable tuning, and enable **Listen on startup**. To let a frequency
click start SDR++ when it is closed, set `sdrpp_executable` in the
laptop's `config.json` to the absolute path of `sdrpp.exe` and restart
the hub. If you chose another port, set `sdrpp_rigctl_port` to the same
port in the hub configuration. If your SDR++ shortcut already uses
`--root`, set `sdrpp_root` to that same existing profile directory so
a launch from the hub loads its Rigctl settings.

Open the hub using its localhost URL on the laptop and select a
frequency. The hub confirms the new frequency with SDR++. Use
**Search** in the radio list for frequency details. Start the receiver
in SDR++ when you want audio. Use **Settings -> Check SDR++
connection** to read the receiver frequency or find out why the local
Rigctl connection is unavailable.

SDR++ needs a working graphics driver with OpenGL support to open its
window. If it crashes on launch, check the display adapter in Device
Manager -- a machine running on the generic Microsoft Basic Display
Adapter (no vendor driver installed) will crash SDR++ at startup.

## Demo mode

Sample data is **OFF by default** in the shipped `config.json`
(`"demo_fallback": false`). Before the library exists, Kiwix and
Recoll simply report unavailable -- they do not serve sample results,
because a sample medical result must never be mistaken for the real
library.

To try the UI with sample data before the library exists, run the hub
with `--demo` (or set `"demo_fallback": true` in `config.json`).
Sample results are tagged **sample** in the UI and the sources rail
shows them as demo. `--no-demo` forces it off. Maps and My Files
already run on real local data (the gazetteer and the sample docs
folder).

## Reliability

The hub runs on one drive in a battery-powered laptop in the field;
when a search is run it must answer. The design, in order of what
saves the owner:

- **Safe-mode startup.** A corrupt or unreadable `config.json` never
  stops the hub: the bad file is backed up to
  `config.json.corrupt-<timestamp>`, the last-known-good copy
  (`config.json.good`, written after every successful save) is
  restored if it exists, and otherwise the hub starts from built-in
  defaults (the shipped sources, demo off). It starts and serves
  even with zero backends available -- search returns empty groups,
  not an error page.
- **Graceful degradation.** Sources are queried in parallel with a
  shared 30 s search deadline (10 s for status). A dead, slow, or
  garbage-returning backend is marked unavailable in the rail and the
  other sources still answer; a plug-in that raises, returns a
  non-list, or has a failing `status()` cannot kill a search or the
  server. At most 4 searches run at once -- a 5th gets HTTP 503
  ("hub busy, try again") instead of queuing and saturating the CPU.
- **Port fallback.** If 8899 is busy at startup the hub takes the next
  free port up to 8908 and writes the actual port to `hub.port` in the
  hub folder. `serve_toggle.ps1` and the watchdog read that file, so
  they can find the actual listener. Use the printed `127.0.0.1` URL
  on the laptop. Pins, bookmarks, and readiness history stay with the
  exact host and port where they were saved, including older
  `localhost` URLs. A fallback port opens a different browser storage
  area.
- **Watchdog + autostart.** Two layers, both logon tasks:
  - `install_hub_autostart.ps1` registers the hub to start at logon
    with restart-on-failure (3 tries, 1 minute apart) and no execution
    time limit, launching `run_hub.bat --no-pause` so the task sees
    the hub's real exit code.
  - `scripts/watchdog.ps1` polls `http://127.0.0.1:<port>/api/health`
    every 30 s and restarts the hub after 3 consecutive failed checks
    (one miss is noise -- a reboot, a slow Recoll query). Install it
    with `.\scripts\watchdog.ps1 -Install` (Administrator); it logs
    to `scripts/watchdog.log` and refuses to run twice (named mutex).
    `.\watchdog.ps1 -Once` checks liveness and exits. The watchdog
    re-reads `hub.port.json`, verifies the tracked hub instance, and
    preserves its hotspot mode and preferred port when relaunching.
    This check uses loopback and supports the standard local and
    `--serve` bind addresses. After updating an older installation,
    restart the hub through `run_hub.bat` once to write the new launch
    record.
  The autostart task catches a hub that *exits*; the watchdog catches
  one that *hangs or is killed* without exiting.
  The two layers above describe the installed state. They are NOT
  installed yet: the scripts exist (`install_hub_autostart.ps1`,
  `scripts/watchdog.ps1`) but no Task Scheduler task is installed --
  that step is waiting on the owner's approval. Until it is installed,
  a reboot (Windows Update, a driver install, a power cut) leaves the
  hub down until someone starts it by hand (`run_hub.bat` on the
  laptop).
- **Hub data is a single copy -- back it up.** The hub owns data that
  exists nowhere else: household stock levels (`inventory.db`), saved
  houses/places (`pins.db`), and the hub config (`config.json`,
  `config.json.good`). All of it lives in `C:\Library\search-hub\` on
  one SSD with no second copy -- a disk failure or a corrupt DB wipes
  it permanently. (The library itself can be rebuilt from sources;
  inventory and pins cannot.) Run `scripts/backup-hub-data.ps1` to
  copy all four files to a spare drive or USB stick:
  `.\scripts\backup-hub-data.ps1 -Destination E:\hub-backup`. Each
  database is copied through the SQLite backup API, so the copy is
  consistent even while the hub is running, and each copy is verified
  with `PRAGMA integrity_check`. The Inventory view also has an
  Export button (a JSON download, mirroring the pins export). Back up
  after any big stock or pin change, and before any hub update.
- **Resource limits.** Kiwix search pages are read with a 5 MB cap
  (over it, the search fails cleanly); PMTiles are served in 8 MiB
  chunks (a Range request is still honoured); POST bodies are capped
  at 64 KiB; Recoll's own 20 s timeout sits inside the 30 s search
  deadline; My Files skips non-regular files (a FIFO can never block
  a search).
- **Data files reload without a restart.** The frequencies file and
  the gazetteer are re-read when their modification time changes, so
  a real repeater export swapped in at setup takes effect on the next
  search.
- **Logging.** Startup failures, backend failures, and config
  problems all land in `hub.log` with timestamps, and logging itself
  never raises -- a bad log message cannot kill the hub.

What this does NOT cover: real kiwix-serve / Recoll backends, the
real Task Scheduler task, a real power loss, and real Windows -- the
smoke test proves the code survives the faults on this machine, not
that the Windows pieces work. Run section 7 of
`INTEGRATION_CHECKLIST.md` on the laptop.

## Serving over the hotspot (phone)

1. `scripts\serve_toggle.ps1 -Start` (elevated): switches to the
   Performance plan, starts the Mobile Hotspot, checks the hotspot
   network is classified **Private** (if it is Public, the firewall
   blocks the hub -- the script prints the fix), and checks the hub is
   up. When done: `serve_toggle.ps1 -Stop` (hotspot off, back to
   Field).
2. The hub binds `0.0.0.0:8899` only in serving mode (`run_hub.bat
   --serve`); by default it binds localhost-only. On the phone, open
   `http://<laptop-hotspot-IP>:8899/` (find the IP with `ipconfig` on
   the laptop; the hotspot is usually 192.168.137.1). Serving mode
   listens on all interfaces. Use Windows Firewall to limit access to
   the intended private network.
3. Trade-off, stated plainly: serving needs Wi-Fi ON. That conflicts
   with the Field power plan's radios-off posture -- serving is a
   deliberate mode, not the default.

New installations begin with no library sources shared. On the
laptop, open **Settings -> Hotspot sharing**, select the sources and
manual folders, and click **Save sharing**. **Phone access preview**
shows the saved permissions. Changes apply immediately. Share only
the roots and collections you intend for every device that can reach
the hub.

For manual configuration, add a top-level `hotspot` section to
`config.json` and restart the hub. This example shares the standard
offline maps, the primary Kiwix reader, and the Recoll `FieldManuals`
root:

```json
"hotspot": {
  "sources": ["maps", "kiwix", "recoll"],
  "file_roots": ["manuals"]
}
```

- My Files and street-address index lookups are laptop-only. Recoll
  results and downloads on phones are limited to the selected roots.
  `manuals` is the root name for `FieldManuals`. Other roots use the
  lowercased directory name. Use unique names for shared roots.
  The command palette's separate title index stays laptop-only because
  it has no source permissions. Use the main search for shared manuals.
- Sharing a maps source shares the installed map archives and PDFs.
  For place search, mark individual reviewed gazetteer rows with
  `"public": true`. Rows not marked public remain laptop-only.
  The tracked fixture can stay as shipped.
- Sharing a Kiwix source grants access to all content in that reader.
  Sharing the frequencies source grants access to its configured list.

## Adding a new source plug-in

1. Copy `plugins/myfiles.py` to `plugins/<name>.py` and rename the
   class internals: set `id`, `name`, `badge`, `kind`.
2. Implement two methods (see `plugins/base.py` for the contract):
   - `search(self, query)` -> list of dicts with `title`, `snippet`,
     `url`, `badge`, `detail`. Never raise for a backend problem --
     return `[]` and explain in `status()`.
   - `status(self)` -> `{"available": bool, "detail": str}`.
3. Register it -- either in `config.json` (add an entry to `"sources"`
   with `"plugin": "<name>"`) or from the UI: **+ Add a source** in the
   sources rail, pick the kind, paste the settings JSON.
4. Restart the hub, or just add it through the UI (the hub reloads the
   registry immediately). Test with `python3 tests/smoke_test.py` if
   you changed shared code.

Rules for plug-ins: stdlib only, no network beyond localhost, short
timeouts, cap results (`max_results`, default 8-10). A slow or broken
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
| `GET /api/geocode?q=...` | Street-address lookup against the US address index |
| `GET /api/inventory` | Supplies inventory summary, items, and shopping list |
| `GET /api/pins` | Saved pins (houses and places) |
| `POST /api/sources` | Add a source (`{"name", "plugin", ...settings}`). Send JSON with the matching local hub `Origin` header. |

## Library snapshot dates

ZIM files are snapshots, not subscriptions -- they go stale and Kiwix
does not update them incrementally. The hub shows the dates so
staleness is visible instead of silent: `/api/status` returns a
`library` block, and the UI rail shows it under "Library snapshots".
Fill it in at setup in `config.json`:

```json
"library": {
  "zim_snapshots": {
    "wikipedia_en_all_maxi": "2026-08",
    "ifixit_en_all": "2025-12"
  },
  "recoll_index_date": "2026-11-19"
}
```

Until then it reads "not set". Replace stale ZIM snapshots and rebuild
the Recoll index on a regular schedule (every 6 months is a reasonable
cadence).

## Power notes

- The hub itself is near-free: a few MB of RAM, idle CPU ~0. Kiwix
  idles at tens of MB. The costs are the screen and the backends, not
  the hub.
- Do indexing/downloads plugged in only. Never leave the hub's
  backends (kiwix-serve, Recoll) doing work on battery.
- Disable Windows Search indexing on the library folders (it will
  otherwise churn on battery trying to index hundreds of PDFs -- Recoll
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
  setup-download-resources.ps1  create library folders, fetch the small
                     resources, print the steps for the large ones
  build-address-index.py        build the US street-address index
  build-quad-index.py           build the USGS topo-quad index
  build_recoll_index.bat        build the Recoll index (plugged in)
  setup_hygiene.ps1             Windows Search / Update hygiene (admin)
  serve_toggle.ps1              start/stop serving over the hotspot
  purge_fixtures.ps1            archive the sample fixture docs at setup
  install_hub_autostart.ps1     logon autostart task + desktop shortcut (admin)
  watchdog.ps1                  polls /api/health, restarts the hub if it stops
                     answering (see "Reliability" above; -Install
                     registers it as a logon task)
plugins/
  base.py            plug-in contract
  kiwix.py           ZIM search via kiwix-serve (+ demo fixtures)
  recoll.py          PDF/doc search via recollq (falls back to recoll -t)
  maps.py            place search over the gazetteer (shipped example covers New England)
                     (+ USGS topo quad index: places name their quad)
  myfiles.py         plain-text search over a docs folder
  frequencies.py     repeater + NOAA weather-radio search (local JSON)
  inventory.py       supplies inventory (SQLite, days of water/food,
                     expiry, shopping list)
static/ui.html       the UI (dark navy, no external resources)
static/map.html      the offline map viewer (vendored MapLibre + pmtiles.js)
static/pins.js       shared pin store (server-backed, localStorage cache)
static/brand/        brand assets (Inter font, icons, brand mark, terrain art)
static/vendor/       maplibre-gl.js/.css, pmtiles.js, Noto Sans glyphs
fixtures/            sample data: gazetteer, demo results, sample docs,
                     usgs-quads-ma.json (quad index), frequencies.json
                     (7 real NOAA rows + sample repeater rows)
tests/smoke_test.py  end-to-end checks (see the count in the test output)
COVERAGE.md          one search box -> every resource: the coverage table
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

`scripts/setup-download-resources.ps1` fetches these for you.

## Road routing

The map's Directions box gives turn-by-turn road routes from the
offline road index (`routing.db`, built by
`scripts/build-routing-index-us.py` from an OpenStreetMap extract;
it covers the whole US).
`GET /api/route?from=<lat,lon or address>&to=<lat,lon or address>`
returns legs with road names, distances, and a polyline. Shared
(hotspot) clients get routes when Maps is shared, like `/api/geocode`.
When the index is not installed the endpoint answers 503 and the map
falls back to the straight-line distance, and says so.



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
