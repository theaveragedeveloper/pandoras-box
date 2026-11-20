# Pandora's Box Mk1 — setup-time integration checklist (F12)

The smoke test (`tests/smoke_test.py`, 157 checks) is a **code test**:
it proves the hub, the plug-ins, and the parsers work against fixtures.
It is **not** a platform verdict. It cannot prove that Kiwix actually
searches your ZIMs, that Recoll actually finds your PDFs, or that the
phone can actually open a result — because it has never seen the real
backends (audit flaw #4, round 4).

This checklist is the platform test. Run it once, on the 5424, after
the library is built and the hub is up. One line per check, result
recorded here. A check that is not recorded is not done.

## How to run each check

- Hub up: `run_hub.bat` (or `run_hub.bat --serve` for the phone checks),
  then open http://localhost:8899/ and search the word.
- Kiwix checks: search the word in the hub AND in the Kiwix server
  directly (http://127.0.0.1:8080/search?pattern=<word>) — if the direct
  search works but the hub shows nothing, the hub's Kiwix parser is
  the problem, not the ZIM.
- Recoll checks: search the word in the hub AND with
  `recoll -t -A -q <word>` on the command line — same logic.
- Record the date, the word used, and PASS/FAIL with what you saw.

## 1. Kiwix — one known word per ZIM

Every ZIM must actually search. Some ZIMs ship without a full-text
index and silently return nothing — a ZIM that loads but never matches
is a dead ZIM, and the hub cannot tell "no match" from "cannot search".

| ZIM | Known word (must match) | Hub result? | Direct /search result? | Date | Notes |
|---|---|---|---|---|---|
| wikipedia_en_all_nopic | tourniquet | | | | |
| wikipedia_en_all_nopic | boston | | | | |
| wikimed_en_all | tourniquet | | | | |
| wikimed_en_all | malaria | | | | |
| wikihow_en_all (if taken) | knot | | | | |
| wikivoyage_en_all (if taken) | boston | | | | |
| wiktionary_en_all (if taken) | tourniquet | | | | |
| stackexchange_arduino (if taken) | arduino | | | | |
| stackexchange_aviation (if taken) | cessna | | | | |
| stackexchange_ee (if taken) | resistor | | | | |
| stackexchange_diy (if taken) | drywall | | | | |
| stackexchange_outdoors (if taken) | backpack | | | | |
| gutenberg slices (if taken) | shakespeare | | | | |
| _add a row per ZIM you add_ | | | | | |

If a ZIM fails: check its `_ftindex` flag in the Kiwix catalogue
(Khan Academy is the known case: `_ftindex:no`, browse-only). A
browse-only ZIM stays in the library as a reader, but the hub will
never return hits from it — record that here, do not keep re-testing it.

## 2. Recoll — one known word per folder

| Folder | Known word (must match) | Hub result? | `recoll -t` result? | Snippet renders? | Date |
|---|---|---|---|---|---|
| C:\Library\manuals (TREX vault) | tourniquet | | | | |
| C:\Library\manuals (TREX vault) | radio | | | | |
| C:\Library\medical (Hesperian) | doctor | | | | |
| C:\Library\manuals (USDA canning) | canning | | | | |
| C:\Library\manuals (vehicle manuals) | lightning | | | | |
| C:\Library\docs (own files) | _your word_ | | | | |

Also record here, once: which Recoll binary is on the machine
(`where recollq` / `where recoll`), and the index build date
(written into config.json `library.recoll_index_date`).

## 3. Maps — gazetteer + viewer

| Check | Result | Date |
|---|---|---|
| Search "braintree" — places returned with coordinates | | |
| Search "hospital" — hospitals returned | | |
| Search "washington" — Mount Washington returned | | |
| Open /map — the viewer page loads (no extract yet: shows the "no extract" message, not a blank page) | | |
| After the NE extract is built: /map shows the map at the Braintree coordinates | | |
| After the NE extract is built: a place result's viewer link opens the map at that place | | |
| `pmtiles show C:\Library\maps\ne.pmtiles` — record the real size here: ____ GB (the 1–3 GB figure is an estimate until this runs) | | |

## 4. Phone over the hotspot

| Check | Result | Date |
|---|---|---|
| `serve_toggle.ps1 -Start` — Performance plan active, hotspot on | | |
| Hotspot network profile is **Private** (not Public) | | |
| Phone opens http://192.168.137.1:8899/ — search page loads | | |
| Phone search "tourniquet" — results load, Kiwix article link opens | | |
| Phone search "braintree" — place result opens /map at that place | | |
| Phone results show file names only — no `C:\Library\...` paths visible | | |
| `serve_toggle.ps1 -Stop` — hotspot off, Field plan back | | |

## 5. Backends autostart + recovery

| Check | Result | Date |
|---|---|---|
| Reboot → logon → hub is up without touching anything (autostart task) | | |
| Reboot → kiwix-serve is up too (run_hub.bat starts it first) | | |
| Kill kiwix-serve → hub search still returns, Kiwix group shows unavailable (not a crash) | | |
| Kill Recoll index folder (rename it) → Recoll group shows unavailable, others still answer | | |

## 7. Reliability (round 9 — the hub must not fail once it is active)

Run these after sections 1–5 are green. Each one is also covered by
the smoke test against fake backends; these are the same faults
against the real ones.

| Check | Result | Date |
|---|---|---|
| Kill kiwix-serve mid-search → search still returns the other sources, Kiwix group unavailable | | |
| Kill kiwix-serve AND rename the Recoll index at once → search still returns Maps / My Files / Frequencies | | |
| Corrupt config.json (write `{broken` into it) → hub still starts from defaults on the next launch; a `config.json.corrupt-*` backup exists | | |
| Delete config.json → hub still starts from defaults | | |
| Start the hub while something else holds 8899 → hub takes the next port and `hub.port` says which one | | |
| Stop the hub process (Task Manager) → the watchdog restarts it within ~2 minutes (check `scripts/watchdog.log`) | | |
| Two searches from the phone and two from the laptop at the same time → all four answer; a fifth gets a "hub busy" message, not a hang | | |
| Search with kiwix-serve down AND Recoll missing at once → empty groups, no error page, server still up | | |
| Swap in the real repeater export (overwrite `fixtures/frequencies.json`) → next search finds the real repeaters, no restart | | |

## 8. What the smoke test does NOT cover (read this, don't skip it)

- Real kiwix-serve search HTML (the parser has only seen a fake).
- Real `recoll -t` output on Windows (the parser has only seen a fake).
- The Kiwix Desktop built-in server's `/search` endpoint (the hub
  points at kiwix-serve port 8080; if you run Desktop only, the hub's
  Kiwix source points at nothing — decide which server at setup and
  record it here: ____________________).
- Protomaps extract size, USGS quad downloads, TREX vault total size.
- The WD Blue SMART numbers, battery report numbers,
  and the Etcher installer USB actually booting — those are arrival
  checklist items, not hub items, and they carry the Dec 8 deadline.

## Sign-off

| | |
|---|---|
| Date run | |
| Hub version / commit | |
| ZIMs that failed and were dropped | |
| ZIMs that are browse-only (no index) | |
| Server in use (kiwix-serve / Kiwix Desktop) | |
| Recoll binary in use (recollq / recoll -t) | |
| Notes | |
