# Verify the library-sources feature live against the running hub. ASCII-only.
import json
import re
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:8899"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")

# 1. Homepage HTML: no target=_blank on hub-internal links.
status, html = get("/")
print("homepage status:", status)
print("has source-browser section:", 'id="source-browser"' in html)
# Find all target=_blank occurrences and show their context.
blanks = re.findall(r'.{0,80}target="_blank".{0,40}', html)
print("target=_blank occurrences in homepage:", len(blanks))
for b in blanks[:5]:
    print("  BLANK:", b.replace("\n", " ")[:140])
# Check source rows specifically.
src_rows = re.findall(r'<a class="src"[^>]*>', html)
print("source-row anchors found in static HTML:", len(src_rows))
# The rows are rendered by JS, so check the JS template instead.
print("JS source-row template has target=_blank:",
      'class=\\"src\\" href=\\"${esc(href)}\\"${scope} target=\\"_blank\\"' in html
      or 'target=\\"_blank\\" rel=\\"noopener\\"><span class=\\"icon source-icon' in html)
print("JS source-row template same-tab:",
      'class=\\"src\\" href=\\"${esc(href)}\\"${scope}><span class=\\"icon source-icon' in html)
# Map tile buttons.
print("map US button same-tab:", 'href="/map?src=/tiles/conus.pmtiles">' in html)
print("map ATAK button same-tab:", 'href="/map?style=trex">' in html)
print("Add a map pin same-tab:", 'class="saved-add" href="/map">' in html)
# File links keep _blank via helper.
print("fileTabAttributes helper present:", "fileTabAttributes" in html)
print("opensFile helper present:", "opensFile" in html)

# 2. My Files browse.
status, body = get("/api/source-files?source=myfiles")
data = json.loads(body)
print("myfiles browse status:", status)
print("myfiles roots:", data.get("roots"), "root:", data.get("root"))
print("myfiles entries:", len(data.get("entries", [])))
for e in data.get("entries", [])[:5]:
    print("  ENTRY:", e)

# 3. Recoll (field manuals) browse.
status, body = get("/api/source-files?source=recoll")
data = json.loads(body)
print("recoll browse status:", status)
print("recoll roots:", data.get("roots"), "root:", data.get("root"))
print("recoll entries:", len(data.get("entries", [])))
for e in data.get("entries", [])[:5]:
    print("  ENTRY:", e)

# 4. Recoll scoped search with no query (should still return / not be empty-home).
status, body = get("/api/search?source=recoll")
data = json.loads(body)
print("recoll search (no q) status:", status)
print("recoll search groups:", len(data.get("groups", [])))
for g in data.get("groups", []):
    print("  GROUP:", g.get("source"), "results:", len(g.get("results", [])))
