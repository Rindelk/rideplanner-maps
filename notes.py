"""Release notes from a manifest. Usage: python notes.py manifest.json"""
import json, sys

m = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"Offline region maps for RidePlanner, from the OpenStreetMap data in planet build {m['build']}.\n")
print("| Region | Map | Search names |\n|---|---|---|")
for r in m["regions"]:
    print(f"| {r['name']} | {r['map']['bytes'] / 1e6:.0f} MB | {r['places']['count']:,} |")
print("\nMap data © OpenStreetMap contributors, available under the Open Database License (ODbL). "
      "Tiles cut from the Protomaps basemap build, with buildings removed.")
