# Search Hub — coverage: one search box, every resource?

**Pandora's Box Mk1, round 8 (2026-11-14).** the owner's requirement, restated
after the NOMAD research: **one main search function that brings him to
the resources, connected to ALL his resources.** This file is the proof:
every resource in the manifest and the resource pack, whether the hub's
one search box reaches it today, through which plug-in, and what is
still needed at setup.

Status key:
- **Connected** — the plug-in exists and reaches it; only the real data
  file / index / ZIM is still needed at setup.
- **Proposed** — the resource itself is pending the owner's yes; the plug-in
  that would reach it already exists, so connecting it is loading the
  file, not building anything.
- **Not a search target** — the thing is not searchable content (an app,
  an installer, an encrypted vault, paper, audio, a raster chart). The
  hub honestly cannot bring you to it by searching, and says why.

Kiwix covers **all** ZIMs once they are loaded in kiwix-serve — there is
no per-ZIM plug-in. Recoll covers **all** indexed PDFs and documents —
there is no per-folder plug-in. That is why most rows below say
"Kiwix" or "Recoll" rather than naming a new plug-in.

## Decided resources (from the manifest)

| Resource | Connected? | Via | Needed at setup |
|---|---|---|---|
| Wikipedia, nopic (~49 GB) | Connected | Kiwix | ZIM loaded in kiwix-serve (`wikipedia_en_all_nopic.zim`) |
| Wikipedia, maxi (~119 GB) | Connected | Kiwix | ZIM loaded instead of nopic (tier choice still pending — either/or, not both) |
| TREX Field Manual Vault (4,215 PDFs) | Connected | Recoll | PDFs downloaded to `C:\Library\FieldManuals\` + index built |
| WikiMed (~2 GB) | Connected | Kiwix | ZIM loaded (`wikimed_en_all.zim`) |
| iFixit (3.3 GiB) | Connected | Kiwix | ZIM loaded (`ifixit_en_all_2025-12.zim`) |
| Protomaps NE extract (street map) | Connected | Maps — viewer `/map` + `/tiles/` | Extract built (`pmtiles extract`, bbox `-73.73,40.98,-66.95,47.47`, maxzoom 15) |
| Place search (towns, hospitals, water, peaks) | Connected | Maps — gazetteer | Nothing — 303-place gazetteer ships with the hub, verified |
| USGS topo quads (MA-first ~190) | Connected* | Maps — quad index | Quads downloaded + bboxes filled from the TNM export. *The code is wired (round 8): a place result names its covering quad and a quad name search returns the quad. With bboxes still null, the name search works but the coverage annotation waits for the export. |
| Repeater directory + NOAA weather frequencies | Connected | Frequencies (new, round 8) | NOAA rows are real and verified (weather.gov, NWS Boston). Repeater rows are SAMPLE until the real RepeaterBook / CHIRP export is verified on arrival — swap the file, same JSON shape. |
| the owner's own data — emergency plan, local hospitals/water/routes, notes | Connected | My Files (text) + Recoll (PDFs) | Folder pointed at the real docs (`C:\Library\MyFiles\`) + sample fixtures purged |
| the owner's own data — encrypted critical documents | Not a search target | — | Encrypted on purpose: the hub cannot and must not search inside them. Their protection is the off-laptop copy (printed + on the mini). |
| Kiwix Desktop / kiwix-serve | Backend | — | Installed + serving on 127.0.0.1:8080. Not content itself. |
| Recoll | Backend | — | Installed + index built. Not content itself. |
| Arduino IDE, KiCad, PulseView, CHIRP | Not a search target | — | Apps, not content. CHIRP's repeater data is searchable via the Frequencies plug-in once exported. |

## Resource pack (proposed — the pack itself is pending the owner's yes)

| Resource | Connected? | Via | Needed at setup |
|---|---|---|---|
| USDA Complete Guide to Home Canning | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| Hesperian — *Where There Is No Doctor / Dentist* | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| Vehicle manuals — F-150 Lightning, Telluride | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| Dell 5424 drivers + offline installers | Not a search target | — | Installers are run, not read. Listed in the rebuild manifest, not the hub. |
| Local practical data (hospitals, water, evacuation) | Proposed → My Files / Recoll | My Files + Recoll | the owner assembles it (same as the decided own-data row above) |
| Foraging / edible plants reference | Not chosen yet | Recoll (once chosen) | Needs a chosen book/title first — no free authoritative source verified |
| Morale media (ebooks, music, movies) | Partial | Recoll / My Files | Ebooks (EPUB/PDF) searchable once indexed; music and movies are not text — filename match only via My Files |
| Printed critical pages | Not a search target | — | Paper. The hub cannot search paper; print after the digital pack is settled. |

## Additional assets (proposed, pending the owner's yes per item)

| Resource | Connected? | Via | Needed at setup |
|---|---|---|---|
| Wiktionary (nopic ZIM) | Proposed → Kiwix | Kiwix | ZIM loaded |
| WikiVoyage (maxi ZIM) | Proposed → Kiwix | Kiwix | ZIM loaded |
| Wikibooks (maxi ZIM) | Proposed → Kiwix | Kiwix | ZIM loaded |
| Kuphaldt, *Lessons in Electric Circuits* | Proposed → Recoll | Recoll | HTML/PDFs downloaded + indexed |
| Stack Exchange ZIMs (EE, DIY, Outdoors, Arduino, Aviation, ham, drones) | Proposed → Kiwix | Kiwix | ZIM(s) loaded |
| DevDocs ZIMs (Python, C++) | Proposed → Kiwix | Kiwix | ZIM(s) loaded |
| Khan Academy (34.3 GB) | Proposed → Kiwix, **browse-only** | Kiwix | ZIM loaded — but it ships with `_ftindex:no` (no full-text index), so the hub's search will NOT find its lessons. Browse-only; do not count it as searched. |
| Gutenberg, full (206 GB) or LCC slices | Proposed → Kiwix | Kiwix | ZIM(s) loaded |
| Stellarium | Not a search target | — | App, not content. The sky is not a document. |
| FCC question pools + study guides | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| NWS SKYWARN guides + weather-safety PDFs | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| FEMA "Are You Ready?" | Proposed → Recoll | Recoll | PDF downloaded + indexed |
| CDC emergency checklists | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| FSI language courses (Spanish, French) | Partial | Recoll | Audio is not searchable. Student text + workbook PDFs are — download and index those. |
| NOAA charts + Coast Pilot Vol. 2 | Proposed → Recoll | Recoll | PDFs downloaded + indexed (chart PDFs carry a text layer; the chart image itself is visual) |
| FAA NY sectional | Not text-searchable | Maps (viewable) | Raster chart — viewable in the map pipeline, not findable by keyword. |
| KeePassXC | Not a search target | — | App with an encrypted vault — not searchable by design. |
| FORScan | Not a search target | — | App, not content. |
| FAA handbooks (AFH, PHAK) | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| OpenStax textbooks (Physics, Biology, Anatomy) | Proposed → Recoll | Recoll | PDFs downloaded + indexed |
| ARRL bands chart | Proposed → Recoll | Recoll | PDF downloaded + indexed (primarily visual — print it; Recoll only sees a text layer if the PDF has one) |
| 3D-printing slicers (PrusaSlicer / Cura) | Not a search target | — | Apps, not content. |
| MeshCore docs + staged firmware | Proposed → Recoll / My Files | Recoll / My Files | Docs + firmware staged while online (markdown/PDF → Recoll; firmware `.bin`s are files, not text) |
| CONUS Protomaps extract | Proposed → Maps | Maps — viewer | Extract built (20–25 GB allowance, measure when built) |
| WikiHow | Moot | — | No WikiHow ZIM exists in the Kiwix catalogue (verified round 6). Nothing to connect. |

## What the one search box does NOT cover — and why

- **Apps and installers** (Arduino IDE, KiCad, PulseView, CHIRP, Stellarium,
  KeePassXC, FORScan, slicers, Dell drivers): you run them, you don't
  search them. They are launched from the Start menu / desktop, not
  found by keyword. Their *data* is searchable where it is text
  (CHIRP repeater data via Frequencies; KeePassXC vault contents are
  encrypted and stay that way).
- **Encrypted things**: the critical-documents copy and the KeePassXC
  vault are encrypted on purpose. A search hub that could read them
  would defeat the point.
- **Audio and video**: FSI audio, music, movies, Khan Academy video.
  The hub searches text. FSI's text PDFs and Khan's (absent) index are
  the honest boundary — see the rows above.
- **Raster maps**: the FAA sectional and the chart images are pictures,
  not text. The viewer shows them; keywords don't find them.
- **Paper**: printed critical pages live on paper.
- **Khan Academy**: listed separately because it *looks* like a ZIM the
  hub would search, and it isn't one — `_ftindex:no`.

## What was added in round 8

- **Frequencies plug-in** (`plugins/frequencies.py`) — the gap from
  GAP_LIST item 9. One search now reaches repeaters and NOAA channels:
  7 real NOAA rows verified against weather.gov (NWS Boston) and 3
  clearly-labelled SAMPLE repeater rows, in
  `fixtures/frequencies.json`. Swap the sample rows for the real
  RepeaterBook / CHIRP export at setup — same JSON shape.
- **USGS quad index wired into the Maps plug-in** (GAP_LIST item 2):
  place results name the quad that covers them once bboxes are filled
  from the TNM export; quad names are searchable now.
- **The UI names what the one box searches**: a line under the search
  box lists the connected sources live from `/api/status` — the box
  never promises a source it is not searching.
- **Tests** assert the fan-out: one search returns a group for every
  source in `config.json` (see `tests/smoke_test.py` §24).

## Still needed at setup (nothing here is connected until it exists)

Real ZIMs loaded in kiwix-serve; real PDFs downloaded and the Recoll
index built; the Protomaps extract built; the topo quads downloaded with
bboxes filled; the real repeater export swapped in; the My Files folder
pointed at the owner's real docs and the sample fixtures purged. Until then
the hub reports those sources unavailable (sample data is OFF by
default) — it does not pretend to search a library that isn't there.
