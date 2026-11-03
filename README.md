# RidePlanner region maps

Offline maps for [RidePlanner](https://github.com/Rindelk/RidePlanner), the Android route planner for the DMD2 navigator. Once a month a GitHub Action cuts each region out of the [Protomaps](https://protomaps.com) daily planet build, removes the buildings layer, and publishes the files as a release here. RidePlanner lists the regions from the latest release's `manifest.json` and downloads them over Wi-Fi.

## Regions

| Region | Area | Zoom |
|---|---|---|
| Ontario South | Ontario up to 46.8° N: Windsor to Ottawa, cottage country, Algonquin, Sudbury and North Bay | 0–14 |
| Ontario North | Ontario from 45.5° N to Hudson Bay | 0–14 |

The two overlap between 45.5° and 46.8° N, so a ride around Sudbury, North Bay or Parry Sound needs only one of them. Both include the whole province up to zoom 8. Zooms closer than 14 enlarge zoom 14, which already has every road and track.

Each release has, per region:

- `<id>.pmtiles`: the map, as [PMTiles v3](https://github.com/protomaps/PMTiles) vector tiles in the Protomaps basemap layers (earth, water, landcover, landuse, roads, boundaries, places, pois);
- `<id>.places.tsv.gz`: names for offline search, one per line: name, kind, latitude, longitude, population. It holds towns, villages, hamlets and neighbourhoods, and named lakes, parks, campsites, peaks, fuel stations and lodging;
- `manifest.json`: the region list, sizes and SHA-256 checksums.

The two newest releases are kept, so a download already under way can finish when a new month's maps arrive.

## Building

`build.py` needs Python 3.9 or later and nothing else. It reads only the tiles it needs from the 140 GB planet file, using HTTP range requests.

```
python build.py out                      # every region in regions.json, newest planet build
python build.py out --only ontario-north --maxzoom 10
python build.py out --regions test/regions.json   # a small Sudbury cut, about 7 MB
python verify.py out/ontario-north.pmtiles
```

To add a region, add an Osmosis `.poly` outline and an entry in `regions.json`. Each map file has to stay under GitHub's 2 GB limit for a release file.

## Licence

The map data is © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the [Open Database License](https://opendatacommons.org/licenses/odbl/). The tiles are cut from the Protomaps basemap build; these files are RidePlanner's own cut and aren't published by Protomaps. `ontario.poly` is the Ontario outline from [Geofabrik](https://download.geofabrik.de/north-america/canada/ontario.html), derived from OpenStreetMap data.
