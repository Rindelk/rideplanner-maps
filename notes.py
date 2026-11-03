"""Release notes from a manifest. Usage: python notes.py manifest.json"""
import json, sys

m = json.load(open(sys.argv[1], encoding="utf-8"))
regions = [r for r in m["regions"] if not r.get("overview")]
total = sum(r["map"]["bytes"] + r["places"]["bytes"] for r in m["regions"])
print(f"Offline region maps for RidePlanner, from the OpenStreetMap data in planet build {m['build']}: "
      f"{len(regions)} regions, {total / 1e9:.1f} GB in all.\n")
for country in sorted({r.get("country") or "Other" for r in regions}):
    print(f"### {country}\n\n| Region | Map | Search names |\n|---|---|---|")
    for r in sorted((r for r in regions if (r.get("country") or "Other") == country), key=lambda r: r["name"]):
        print(f"| {r['name']} | {r['map']['bytes'] / 1e6:.0f} MB | {r['places']['count']:,} |")
    print()
for r in m["regions"]:
    if r.get("overview"):
        print(f"`{r['map']['file']}` ({r['map']['bytes'] / 1e6:.0f} MB) is the low-zoom overview the app keeps with any region.\n")
print("Map data © OpenStreetMap contributors, available under the Open Database License (ODbL). "
      "Tiles cut from the Protomaps basemap build, with buildings removed. "
      "Region outlines from Geofabrik, derived from OpenStreetMap data.")
