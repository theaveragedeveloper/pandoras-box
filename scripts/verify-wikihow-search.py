# Verify a WikiHow search through the hub API. ASCII-only.
import json
import urllib.request
import urllib.parse

q = "how to tie shoes"
url = "http://127.0.0.1:8899/api/search?" + urllib.parse.urlencode({"q": q})
with urllib.request.urlopen(url, timeout=120) as r:
    data = json.load(r)

print("keys:", sorted(data.keys()))
groups = data.get("groups", [])
print("num groups:", len(groups))
for g in groups:
    src = g.get("source") or g.get("id") or g.get("name")
    res = g.get("results", [])
    print("GROUP source=%r count=%d" % (src, len(res)))
    if src and "wikihow" in str(src).lower():
        for item in res[:3]:
            print("  WIKIHOW:", item.get("title"))
# Also print any result whose title/url mentions wikihow, regardless of group.
found = 0
for g in groups:
    for item in g.get("results", []):
        blob = json.dumps(item).lower()
        if "wikihow" in blob:
            found += 1
            if found <= 3:
                print("WIKIHOW RESULT:", item.get("title"), "|", item.get("url", "")[:120])
print("total results mentioning wikihow:", found)
