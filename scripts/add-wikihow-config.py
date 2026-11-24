# Add wikihow_en_maxi 2023-03 to the LIVE hub config zim_snapshots.
# BOM-aware: read with utf-8-sig (tolerates BOM or not), write BOM-free.
# Run on the laptop via SSH. ASCII-only.
import json

path = r"C:\Library\search-hub\config.json"
with open(path, encoding="utf-8-sig") as f:
    data = json.load(f)

snaps = data.setdefault("library", {}).setdefault("zim_snapshots", {})
snaps["wikihow_en_maxi"] = "2023-03"

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")

# Verify by re-reading.
with open(path, encoding="utf-8-sig") as f:
    check = json.load(f)
print(json.dumps(check["library"]["zim_snapshots"], indent=1))
print("OK wikihow_en_maxi =", check["library"]["zim_snapshots"].get("wikihow_en_maxi"))
