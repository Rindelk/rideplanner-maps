# RidePlanner region maps

Offline maps for [RidePlanner](https://github.com/Rindelk/RidePlanner), the Android route planner for the DMD2 navigator. Once a month a GitHub Action cuts each region out of the [Protomaps](https://protomaps.com) daily planet build, removes the buildings layer, and publishes the files as a release here. RidePlanner lists the regions from the latest release's `manifest.json` and downloads them over Wi-Fi, one at a time or a whole country at once.

## Regions

All of Canada and the USA: every province, territory and state, about 14.5 GB in all. Each is one file, from about 13 MB (Hawaii) to about 950 MB (Quebec North, British Columbia). Most hold zoom 0 to 14. Closer zooms enlarge zoom 14, which already has every road and track. Neighbouring regions overlap a little at their borders, as the outlines do.

These regions differ from a plain province or state:

| Region | Area |
|---|---|
| Ontario South | Ontario up to 46.8° N: Windsor to Ottawa, cottage country, Algonquin, Sudbury and North Bay |
| Ontario North | Ontario from 45.5° N to Hudson Bay |
| Quebec South | Quebec up to 50.5° N: Montreal, Quebec City, the Townships, Saguenay–Lac-Saint-Jean, Gaspésie, the Côte-Nord to Sept-Îles |
| Quebec North | Quebec from 49.5° N: the James Bay Road, Route 389, the Trans-Taiga and Nunavik |
| Nunavut | Zoom 0–11 only. No roads join its communities, and street zoom would be over GitHub's 2 GB limit for a file |
| Maryland and DC | Maryland with Washington, DC |
| Hawaii | The main islands, Niʻihau to Hawaiʻi |
| Alaska | All but the Aleutians west of 180° |

The Ontario and Quebec halves overlap by a degree of latitude, so a ride across the line needs only one of them. Each region includes its whole area up to zoom 8.

There's also `overview.pmtiles` (about 12 MB): Canada and the USA up to zoom 6. The app downloads it with the first region and draws zoomed-out maps from it alone, so that having many regions doesn't slow the map down.

Each release has, per region:

- `<id>.pmtiles`: the map, as [PMTiles v3](https://github.com/protomaps/PMTiles) vector tiles in the Protomaps basemap layers (earth, water, landcover, landuse, roads, boundaries, places, pois);
- `<id>.places.tsv.gz`: names for offline search, one per line: name, kind, latitude, longitude, population, and the name as the app compares it (lower case, no accents or punctuation). It holds towns, villages, hamlets and neighbourhoods, and named lakes, parks, campsites, peaks, fuel stations and lodging;
- `manifest.json`: the region list, with each region's country, box, sizes, SHA-256 checksums and routing squares.

The two newest releases are kept, so a download already under way can finish when a new month's maps arrive.

## Building

`build.py` needs Python 3.9 or later and nothing else. It reads only the tiles it needs from the 140 GB planet file, using HTTP range requests.

```
python build.py out                                   # every region in regions.json, newest planet build
python build.py out --only ontario-north,quebec-south   # some of them
python build.py out --only delaware --maxzoom 10        # a quick test
python build.py out --regions test/regions.json         # a small Sudbury cut, about 7 MB
python build.py out --estimate                          # each map's size from the planet's index alone, in a minute or so
python build.py out --manifest-only                     # manifest.json from the regions already in out/
python verify.py out/ontario-north.pmtiles
```

The monthly workflow builds each region in a job of its own, uploads its files to a draft release, and publishes the release with the manifest once every region has built. A month that fails part way stays a draft; the next run starts it again.

To add a region, add an Osmosis `.poly` outline under `polys/` and an entry in `regions.json`. An entry can take several outlines, and `lat_min`, `lat_max`, `lon_min` and `lon_max` to cut it to a band. Check its size with `--estimate` first: each map file has to stay under GitHub's 2 GB limit for a release file.

## Licence

The map data is © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the [Open Database License](https://opendatacommons.org/licenses/odbl/). The tiles are cut from the Protomaps basemap build; these files are RidePlanner's own cut and aren't published by Protomaps. The outlines in `polys/` are from [Geofabrik](https://download.geofabrik.de/north-america.html), derived from OpenStreetMap data.
