"""Builds RidePlanner's offline region maps from the Protomaps daily planet build.

For each region in regions.json it reads only the tiles it needs from the planet file (HTTP range
requests), drops the layers the app doesn't draw (buildings), and writes:

  <id>.pmtiles        the map, PMTiles v3, zoom 0 to the region's maxzoom
  <id>.places.tsv.gz  names for offline search: name, kind, lat, lon, population, search key (one per line)
  <id>.region.json    the region's manifest entry

plus manifest.json, made from every <id>.region.json in OUT_DIR, which the app reads to list the
regions and check for updates. Regions can so be built one at a time, in separate jobs, and the
manifest put together at the end with --manifest-only.

The overview (regions.json's "overview") is the whole of the regions at low zoom, in one small file
the app keeps with any region, so a zoomed-out map draws from it alone rather than from every region.
Standard library only, so it runs on a stock GitHub runner.

Usage: python build.py OUT_DIR [--build YYYYMMDD] [--only ID,ID] [--maxzoom N] [--regions FILE] [--manifest-only]
"""
import argparse, bisect, datetime, gzip, hashlib, json, math, os, re, struct, sys, tempfile, time, unicodedata, urllib.error, urllib.request

BUILDS = "https://build-metadata.protomaps.dev/builds.json"
PLANET = "https://build.protomaps.com/{key}"
UA = "rideplanner-maps/1 (monthly region build; github.com/Rindelk/rideplanner-maps)"
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- HTTP

def fetch(url, start=None, length=None, tries=6):
    headers = {"User-Agent": UA}
    if start is not None:
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                data = r.read()
            if length is not None and len(data) != length:
                raise IOError(f"short read {len(data)} of {length}")
            return data
        except Exception as e:
            if k == tries - 1:
                raise
            print(f"  retry after {e}", flush=True)
            time.sleep(2 ** k)

def latest_build():
    builds = json.loads(fetch(BUILDS))
    builds = [b for b in builds if b["key"].endswith(".pmtiles")]
    return max(builds, key=lambda b: b["key"])

# ---------------------------------------------------------------- PMTiles v3

def uvarint(buf, pos):
    shift = result = 0
    while True:
        byte = buf[pos]; pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7

def put_uvarint(out, v):
    while True:
        b = v & 0x7F; v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b); return

def read_header(b):
    assert b[:7] == b"PMTiles" and b[7] == 3, "not PMTiles v3"
    keys = ["root_offset", "root_length", "meta_offset", "meta_length", "leaf_offset", "leaf_length",
            "data_offset", "data_length", "addressed", "entries", "contents"]
    h = dict(zip(keys, struct.unpack_from("<11Q", b, 8)))
    h["clustered"], h["internal_comp"], h["tile_comp"], h["tile_type"], h["min_zoom"], h["max_zoom"] = struct.unpack_from("<6B", b, 96)
    return h

def decompress(data, comp):
    if comp in (0, 1):
        return data
    if comp == 2:
        return gzip.decompress(data)
    raise ValueError(f"compression {comp} not supported")

def parse_dir(raw):
    n, p = uvarint(raw, 0)
    ids, runs, lens, offs = [0] * n, [0] * n, [0] * n, [0] * n
    last = 0
    for i in range(n):
        d, p = uvarint(raw, p); last += d; ids[i] = last
    for i in range(n):
        runs[i], p = uvarint(raw, p)
    for i in range(n):
        lens[i], p = uvarint(raw, p)
    for i in range(n):
        v, p = uvarint(raw, p)
        offs[i] = offs[i - 1] + lens[i - 1] if (v == 0 and i > 0) else v - 1
    return ids, runs, lens, offs

def serialize_dir(entries):
    out = bytearray()
    put_uvarint(out, len(entries))
    last = 0
    for e in entries:
        put_uvarint(out, e[0] - last); last = e[0]
    for e in entries: put_uvarint(out, e[1])
    for e in entries: put_uvarint(out, e[2])
    for i, e in enumerate(entries):
        if i > 0 and e[3] == entries[i - 1][3] + entries[i - 1][2]:
            put_uvarint(out, 0)
        else:
            put_uvarint(out, e[3] + 1)
    return bytes(out)

def zxy_to_id(z, x, y):
    acc = ((1 << (2 * z)) - 1) // 3
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x; y = n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d

class Archive:
    """A remote PMTiles file, read by range requests. Leaf directories are cached."""
    def __init__(self, url):
        self.url = url
        head = fetch(url, 0, 16384)
        self.h = read_header(head)
        h = self.h
        self.root = parse_dir(decompress(head[h["root_offset"]:h["root_offset"] + h["root_length"]], h["internal_comp"]))
        self.leaves = {}

    def leaf(self, off, length):
        key = (off, length)
        if key not in self.leaves:
            self.leaves[key] = parse_dir(decompress(fetch(self.url, self.h["leaf_offset"] + off, length), self.h["internal_comp"]))
        return self.leaves[key]

    def find(self, tid):
        """(offset, length) of the tile in the data section, or None."""
        d = self.root
        for _ in range(4):
            ids, runs, lens, offs = d
            i = bisect.bisect_right(ids, tid) - 1
            if i < 0:
                return None
            if runs[i] == 0:
                d = self.leaf(offs[i], lens[i])
                continue
            return (offs[i], lens[i]) if tid < ids[i] + runs[i] else None
        return None

def write_pmtiles(path, entries, data_file, data_len, h_src, maxzoom, bbox, meta):
    """entries: [tile_id, run, length, offset] sorted by tile id; data_file holds the data section."""
    root = gzip.compress(serialize_dir(entries), 9)
    leaves = b""
    size = 4096
    while len(root) > 16384 - 127:
        chunks, leaf_entries, at = [], [], 0
        for k in range(0, len(entries), size):
            part = gzip.compress(serialize_dir(entries[k:k + size]), 9)
            leaf_entries.append([entries[k][0], 0, len(part), at])
            chunks.append(part); at += len(part)
        leaves = b"".join(chunks)
        root = gzip.compress(serialize_dir(leaf_entries), 9)
        size *= 2
    meta = gzip.compress(json.dumps(meta).encode(), 9)
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    addressed = sum(e[1] for e in entries)
    contents = len({e[3] for e in entries})
    # Clustered: each tile's data first appears in tile id order, packed end to end.
    clustered, seen, nxt = 1, set(), 0
    for e in entries:
        if e[3] in seen:
            continue
        if e[3] != nxt:
            clustered = 0; break
        seen.add(e[3]); nxt = e[3] + e[2]
    hdr = bytearray(b"PMTiles") + bytes([3])
    hdr += struct.pack("<11Q", root_off, len(root), meta_off, len(meta), leaf_off, len(leaves), data_off, data_len,
                       addressed, len(entries), contents)
    hdr += struct.pack("<6B", clustered, 2, h_src["tile_comp"], h_src["tile_type"], 0, maxzoom)
    hdr += struct.pack("<4i", *[round(v * 1e7) for v in bbox])
    hdr += struct.pack("<B2i", min(maxzoom, 8), round((bbox[0] + bbox[2]) / 2 * 1e7), round((bbox[1] + bbox[3]) / 2 * 1e7))
    assert len(hdr) == 127
    with open(path, "wb") as f:
        f.write(hdr); f.write(root); f.write(meta); f.write(leaves)
        data_file.seek(0)
        while True:
            buf = data_file.read(1 << 20)
            if not buf:
                break
            f.write(buf)

# ---------------------------------------------------------------- vector tiles (protobuf)

def pb_fields(b):
    p = 0
    n = len(b)
    while p < n:
        k, p = uvarint(b, p); f, t = k >> 3, k & 7
        start = p
        if t == 0:
            v, p = uvarint(b, p)
        elif t == 2:
            ln, p = uvarint(b, p); v = b[p:p + ln]; p += ln
        elif t == 5:
            v = b[p:p + 4]; p += 4
        elif t == 1:
            v = b[p:p + 8]; p += 8
        else:
            raise ValueError(f"wire type {t}")
        yield f, t, v

def layer_name(layer):
    for f, _, v in pb_fields(layer):
        if f == 1:
            return v.decode()
    return None

def drop_layers(tile, drop):
    """The tile with the named layers removed, re-encoded field by field."""
    out = bytearray()
    for f, t, v in pb_fields(tile):
        if f == 3 and layer_name(v) in drop:
            continue
        assert t == 2
        put_uvarint(out, (f << 3) | 2); put_uvarint(out, len(v)); out += v
    return bytes(out)

def pb_value(v):
    for f, _, x in pb_fields(v):
        if f == 1: return x.decode()
        if f == 2: return struct.unpack("<f", x)[0]
        if f == 3: return struct.unpack("<d", x)[0]
        if f in (4, 5): return x
        if f == 6: return (x >> 1) ^ -(x & 1)
        if f == 7: return bool(x)
    return None

def points_in(tile, layers):
    """(layer, properties, first point x, y, extent) for point features of the given layers."""
    for f, _, layer in pb_fields(tile):
        if f != 3:
            continue
        name = None; keys = []; vals = []; feats = []; extent = 4096
        for lf, _, lv in pb_fields(layer):
            if lf == 1: name = lv.decode()
            elif lf == 2: feats.append(lv)
            elif lf == 3: keys.append(lv.decode())
            elif lf == 4: vals.append(pb_value(lv))
            elif lf == 5: extent = lv
        if name not in layers:
            continue
        for ft in feats:
            tags = []; geom = []; gtype = 0
            for ff, _, fv in pb_fields(ft):
                if ff == 2 or ff == 4:
                    arr = []; p = 0
                    while p < len(fv):
                        x, p = uvarint(fv, p); arr.append(x)
                    if ff == 2: tags = arr
                    else: geom = arr
                elif ff == 3:
                    gtype = fv
            if gtype != 1 or len(geom) < 3:
                continue
            x = (geom[1] >> 1) ^ -(geom[1] & 1)
            y = (geom[2] >> 1) ^ -(geom[2] & 1)
            props = {keys[tags[j]]: vals[tags[j + 1]] for j in range(0, len(tags) - 1, 2)}
            yield name, props, x, y, extent

# ---------------------------------------------------------------- regions

def read_poly(path):
    """Rings of an Osmosis .poly file (holes are treated as outer rings; Ontario has none)."""
    rings, cur = [], None
    for line in open(path, encoding="utf-8"):
        t = line.split()
        if len(t) == 2 and cur is not None:
            cur.append((float(t[0]), float(t[1])))
        elif t == ["END"]:
            if cur is not None: rings.append(cur); cur = None
        elif len(t) == 1 and not t[0].startswith("!"):
            cur = []
    return [r for r in rings if r]

def lon_to_x(lon, n): return (lon + 180.0) / 360.0 * n
def lat_to_y(lat, n):
    r = math.radians(max(-85.0, min(85.0, lat)))
    return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
def y_to_lat(y, n): return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))

def region_rings(region):
    """The region's outline rings: one or more .poly files, less any ring wholly outside its longitude band."""
    polys = region["poly"] if isinstance(region["poly"], list) else [region["poly"]]
    lon_min, lon_max = region.get("lon_min", -180.0), region.get("lon_max", 180.0)
    rings = [r for p in polys for r in read_poly(os.path.join(HERE, p))]
    return [r for r in rings if max(q[0] for q in r) >= lon_min and min(q[0] for q in r) <= lon_max]

def region_tiles(z, rings, lat_min=-90.0, lat_max=90.0, lon_min=-180.0, lon_max=180.0):
    """Tiles at zoom z covering the polygon, cut to a latitude and longitude band, with a one-tile margin.

    Each tile row is scanned at its top, middle and bottom; the spans inside the polygon there,
    widened by a tile, are the row's tiles. The margin covers coastline wiggles between the scan lines.
    """
    n = 1 << z
    pts = [p for r in rings for p in r]
    lo_lat = max(lat_min, min(p[1] for p in pts)); hi_lat = min(lat_max, max(p[1] for p in pts))
    y0 = max(0, int(lat_to_y(hi_lat, n)) - 1); y1 = min(n - 1, int(lat_to_y(lo_lat, n)) + 1)
    out = set()
    for y in range(y0, y1 + 1):
        xs = set()
        for fy in (0.0, 0.5, 1.0):
            lat = y_to_lat(y + fy, n)
            if lat < lat_min - 1e-9 or lat > lat_max + 1e-9:
                lat = max(lat_min, min(lat_max, lat))
            cross = []
            for r in rings:
                j = len(r) - 1
                for i in range(len(r)):
                    (xi, yi), (xj, yj) = r[i], r[j]
                    if (yi > lat) != (yj > lat):
                        cross.append(xi + (lat - yi) * (xj - xi) / (yj - yi))
                    j = i
            cross.sort()
            for a, b in zip(cross[0::2], cross[1::2]):
                a, b = max(a, lon_min), min(b, lon_max)
                if a > b:
                    continue
                xa = int(lon_to_x(a, n)) - 1; xb = int(lon_to_x(b, n)) + 1
                xs.update(range(max(0, xa), min(n - 1, xb) + 1))
        # Rows the band edge cuts still count only if they reach into the band.
        top, bottom = y_to_lat(y, n), y_to_lat(y + 1, n)
        if bottom > lat_max or top < lat_min:
            continue
        out.update((x, y) for x in xs)
    # Tiles holding a polygon vertex (narrow points the scan lines can miss).
    for lon, lat in pts:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            out.add((min(n - 1, int(lon_to_x(lon, n))), min(n - 1, int(lat_to_y(lat, n)))))
    return out

def inside(rings, lon, lat):
    c = False
    for r in rings:
        j = len(r) - 1
        for i in range(len(r)):
            (xi, yi), (xj, yj) = r[i], r[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                c = not c
            j = i
    return c

# ---------------------------------------------------------------- search index

PLACE_KINDS = {"locality", "neighbourhood"}

def search_key(name):
    """The name as the app compares it: lower case, accents off, anything but letters and digits a space.
    The same steps as RegionPlaces.normalize in the app."""
    s = unicodedata.normalize("NFD", name.lower())
    s = "".join(c for c in s if not unicodedata.category(c).startswith("M"))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()
POI_KINDS = {
    "water", "lake", "park", "national_park", "nature_reserve", "protected_area", "forest", "camp_site", "caravan_site",
    "peak", "viewpoint", "beach", "aerodrome", "airport", "ferry_terminal", "fuel", "hotel", "motel", "attraction",
    "picnic_site", "museum", "slipway", "marina", "waterfall", "hostel", "guest_house", "alpine_hut", "wilderness_hut",
}

def tile_point(z, tx, ty, x, y, extent):
    n = 1 << z
    lon = (tx + x / extent) / n * 360.0 - 180.0
    lat = y_to_lat(ty + y / extent, n)
    return lat, lon

class PlaceIndex:
    def __init__(self):
        self.seen = {}

    def add_tile(self, z, x, y, tile):
        for layer, props, px, py, extent in points_in(tile, ("places", "pois")):
            name = props.get("name:en") or props.get("name")
            kind = props.get("kind")
            if not isinstance(name, str) or not name.strip() or not isinstance(kind, str):
                continue
            if layer == "places" and kind not in PLACE_KINDS:
                continue
            if layer == "pois" and kind not in POI_KINDS:
                continue
            if not (0 <= px <= extent and 0 <= py <= extent):
                continue      # in the tile's buffer; the neighbouring tile has it
            if layer == "places":
                kind = props.get("kind_detail") if isinstance(props.get("kind_detail"), str) else kind
            lat, lon = tile_point(z, x, y, px, py, extent)
            key = (name, kind, round(lat, 2), round(lon, 2))
            if key not in self.seen:
                pop = props.get("population")
                self.seen[key] = (name.replace("\t", " ").strip(), kind, round(lat, 5), round(lon, 5),
                                  int(pop) if isinstance(pop, (int, float)) else 0)

    def write(self, path):
        rows = sorted(self.seen.values(), key=lambda r: (-r[4], r[0]))
        # A lake or park is labelled at a different point at each zoom: keep one per name and kind within 5 km.
        kept, near = [], {}
        for r in rows:
            spots = near.setdefault((r[0], r[1]), [])
            if any(abs(r[2] - a) < 0.045 and abs(r[3] - b) < 0.065 for a, b in spots):
                continue
            spots.append((r[2], r[3])); kept.append(r)
        rows = kept
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
            for r in rows:
                f.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{search_key(r[0])}\n")
        return len(rows)

# ---------------------------------------------------------------- routing squares

SEGMENTS = "https://brouter.de/brouter/segments4/{name}.rd5"

def square_name(lat, lon):
    lo = math.floor(lon / 5) * 5; la = math.floor(lat / 5) * 5
    return f"{'W' if lo < 0 else 'E'}{abs(lo)}_{'S' if la < 0 else 'N'}{abs(la)}"

def region_squares(rings, lat_min, lat_max, lon_min=-180.0, lon_max=180.0):
    """BRouter's 5-degree routing squares under the region, with their current download sizes.

    Squares the server has no data for (open water) are left out. Sizes change a little each week.
    """
    # A 0.05-degree grid of points inside the outline; land and water alike, since lakes have islands.
    pts = [p for r in rings for p in r]
    lat0 = max(lat_min, min(p[1] for p in pts)); lat1 = min(lat_max, max(p[1] for p in pts))
    lon0 = max(lon_min, min(p[0] for p in pts)); lon1 = min(lon_max, max(p[0] for p in pts))
    names = set()
    lat = lat0
    while lat <= lat1:
        lon = lon0
        while lon <= lon1:
            if square_name(lat, lon) not in names and inside(rings, lon, lat):
                names.add(square_name(lat, lon))
            lon += 0.05
        lat += 0.05
    out = []
    for name in sorted(names):
        try:
            req = urllib.request.Request(SEGMENTS.format(name=name), method="HEAD", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                out.append({"name": name, "bytes": int(r.headers.get("Content-Length") or 0)})
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return out

# ---------------------------------------------------------------- build

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                return h.hexdigest()
            h.update(b)

def band(region):
    return (region.get("lat_min", -90.0), region.get("lat_max", 90.0), region.get("lon_min", -180.0), region.get("lon_max", 180.0))

def region_want(arc, region, rings, maxz, quiet=False):
    """(tile id, z, x, y, (src offset, src length)) for every tile the region's file holds, by tile id."""
    full_to = maxz if region.get("overview") else region.get("whole_area_to_zoom", 8)
    t0 = time.time()
    want = []
    for z in range(0, maxz + 1):
        tiles = region_tiles(z, rings) if z <= full_to else region_tiles(z, rings, *band(region))
        for x, y in tiles:
            tid = zxy_to_id(z, x, y)
            r = arc.find(tid)
            if r:
                want.append((tid, z, x, y, r))
        if z >= 10 and not quiet:
            print(f"  z{z}: {len(want)} tiles so far ({time.time() - t0:.0f} s)", flush=True)
    want.sort()
    return want

def region_bbox(rings, region):
    lat_min, lat_max, lon_min, lon_max = band(region)
    pts = [p for r in rings for p in r]
    return [max(lon_min, min(p[0] for p in pts)), max(lat_min, min(p[1] for p in pts)),
            min(lon_max, max(p[0] for p in pts)), min(lat_max, max(p[1] for p in pts))]

# Tiles keep about this share of the planet's bytes once the buildings layer is dropped and they're re-packed
# (0.91 and 1.00 on Ontario South and North, 2026-11); cities lose more, so this overstates them a little.
KEEP_SHARE = 0.92

def estimate_region(arc, region, maxz):
    """The map file's size from the planet's directory alone: no tile is fetched."""
    rings = region_rings(region)
    want = region_want(arc, region, rings, maxz, quiet=True)
    distinct = {r for *_, r in want}
    by_zoom = {}
    for tid, z, x, y, r in want:
        by_zoom[z] = by_zoom.get(z, 0) + 1
    planet_bytes = sum(r[1] for r in distinct)
    return {"id": region["id"], "tiles": len(want), "distinct": len(distinct), "planet_mb": round(planet_bytes / 1e6, 1),
            "estimate_mb": round(planet_bytes * KEEP_SHARE / 1e6, 1), "z14_tiles": by_zoom.get(14, 0)}

def build_region(arc, region, out_dir, build, maxzoom_override=None):
    rid = region["id"]
    maxz = maxzoom_override if maxzoom_override is not None else region["maxzoom"]
    rings = region_rings(region)
    lat_min, lat_max, lon_min, lon_max = band(region)
    full_to = region.get("whole_area_to_zoom", 8)
    drop = set(region.get("drop_layers", ["buildings"]))
    index_from = region.get("index_from_zoom", 12)
    print(f"{rid}: zoom 0-{maxz}, whole area to zoom {full_to}, band {lat_min} to {lat_max}, {lon_min} to {lon_max}", flush=True)

    t0 = time.time()
    want = region_want(arc, region, rings, maxz)

    # Fetch each distinct source tile once, in data order, merging nearby ranges.
    by_src = {}
    for tid, z, x, y, r in want:
        by_src.setdefault(r, (z, x, y))
    spans = sorted(by_src)
    index = PlaceIndex()
    out_of = {}           # src range -> (offset, length) in the new data section
    by_hash = {}          # content hash -> (offset, length)
    data = tempfile.TemporaryFile(dir=out_dir)
    at = 0
    fetched = 0
    i = 0
    h = arc.h
    shown = 0
    while i < len(spans):
        start = spans[i][0]; end = start + spans[i][1]; j = i + 1
        while j < len(spans) and spans[j][0] - end < 65536 and spans[j][0] + spans[j][1] - start < 16_000_000:
            end = max(end, spans[j][0] + spans[j][1]); j += 1
        blob = fetch(arc.url, h["data_offset"] + start, end - start)
        fetched += len(blob)
        for s in spans[i:j]:
            raw = blob[s[0] - start:s[0] - start + s[1]]
            tile = decompress(raw, h["tile_comp"])
            z, x, y = by_src[s]
            if z >= index_from and not region.get("overview"):
                index.add_tile(z, x, y, tile)
            kept = drop_layers(tile, drop)
            packed = raw if len(kept) == len(tile) else gzip.compress(kept, 9, mtime=0)
            key = hashlib.sha1(packed).digest()
            if key not in by_hash:
                data.write(packed); by_hash[key] = (at, len(packed)); at += len(packed)
            out_of[s] = by_hash[key]
        i = j
        if fetched - shown > 100_000_000 or j == len(spans):
            shown = fetched
            print(f"  fetched {fetched / 1e6:.0f} MB, wrote {at / 1e6:.0f} MB ({j}/{len(spans)} tiles, {time.time() - t0:.0f} s)", flush=True)

    entries = []
    for tid, z, x, y, r in want:
        o, ln = out_of[r]
        if entries and entries[-1][0] + entries[-1][1] == tid and entries[-1][3] == o:
            entries[-1][1] += 1
        else:
            entries.append([tid, 1, ln, o])

    bbox = region_bbox(rings, region)
    meta = {
        "name": region["name"],
        "attribution": "© OpenStreetMap contributors",
        "description": f"RidePlanner region map, cut from the Protomaps basemap build {build['key']} (version {build.get('version', '?')}).",
        "planetiler:osm:osmosisreplicationtime": build.get("uploaded"),
        "rideplanner:build": build["key"].removesuffix(".pmtiles"),
        "rideplanner:dropped_layers": sorted(drop),
    }
    pm = os.path.join(out_dir, f"{rid}.pmtiles")
    write_pmtiles(pm, entries, data, at, h, maxz, bbox, meta)
    data.close()
    places = os.path.join(out_dir, f"{rid}.places.tsv.gz")
    count = index.write(places)
    print(f"{rid}: {os.path.getsize(pm) / 1e6:.1f} MB, {len(want)} tiles, {count} names, {time.time() - t0:.0f} s", flush=True)
    squares = [] if region.get("overview") else region_squares(rings, lat_min, lat_max, lon_min, lon_max)
    print(f"{rid}: routing squares {', '.join(s['name'] for s in squares)}", flush=True)
    entry = {
        "id": rid, "name": region["name"], "description": region["description"],
        "country": region.get("country", ""),
        "bbox": [round(v, 4) for v in bbox], "maxzoom": maxz,
        "map": {"file": f"{rid}.pmtiles", "bytes": os.path.getsize(pm), "sha256": sha256(pm)},
        "places": {"file": f"{rid}.places.tsv.gz", "bytes": os.path.getsize(places), "sha256": sha256(places), "count": count},
        "routing_squares": squares,
    }
    if region.get("overview"):
        entry["overview"] = True
    with open(os.path.join(out_dir, f"{rid}.region.json"), "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=1, ensure_ascii=False)
    return entry

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--build", help="planet build date, YYYYMMDD (default: the newest)")
    ap.add_argument("--only", help="build just these regions: ids, separated by commas")
    ap.add_argument("--maxzoom", type=int, help="override every region's max zoom (for quick tests)")
    ap.add_argument("--base-url", default="", help="where the files will be published, for the manifest")
    ap.add_argument("--regions", default=os.path.join(HERE, "regions.json"), help="region list (default: regions.json)")
    ap.add_argument("--estimate", action="store_true", help="only estimate each map's size, from the planet's directory, into OUT/estimate.json")
    ap.add_argument("--manifest-only", action="store_true", help="build nothing: put manifest.json together from OUT/*.region.json")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    build = latest_build() if not a.build else {"key": f"{a.build}.pmtiles"}
    print(f"Planet build {build['key']}", flush=True)
    with open(a.regions, encoding="utf-8") as f:
        listed = json.load(f)
    regions = ([listed["overview"]] if "overview" in listed else []) + listed["regions"]
    if a.manifest_only:
        return write_manifest(a.out, regions, build, a.base_url)
    only = set(a.only.split(",")) if a.only else None
    if only and only - {r["id"] for r in regions}:
        sys.exit(f"no such region: {', '.join(sorted(only - {r['id'] for r in regions}))}")
    arc = Archive(PLANET.format(key=build["key"]))
    if a.estimate:
        est = []
        for r in regions:
            if only and r["id"] not in only:
                continue
            e = estimate_region(arc, r, a.maxzoom if a.maxzoom is not None else r["maxzoom"])
            print(json.dumps(e), flush=True)
            est.append(e)
        with open(os.path.join(a.out, "estimate.json"), "w") as f:
            json.dump(est, f, indent=1)
        print(f"total {sum(e['estimate_mb'] for e in est) / 1e3:.1f} GB")
        return
    for r in regions:
        if not only or r["id"] in only:
            build_region(arc, r, a.out, build, a.maxzoom)
    write_manifest(a.out, regions, build, a.base_url)

def write_manifest(out, regions, build, base_url):
    """manifest.json from the regions built into OUT so far, in regions.json's order."""
    done = []
    for r in regions:
        path = os.path.join(out, f"{r['id']}.region.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                done.append(json.load(f))
    manifest = {
        "format": 1,
        "build": build["key"].removesuffix(".pmtiles"),
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Protomaps basemap (OpenStreetMap data)",
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL-1.0",
        "base_url": base_url,
        "regions": done,
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    print(f"manifest: {len(done)} regions, {sum(r['map']['bytes'] for r in done) / 1e9:.2f} GB of maps")

if __name__ == "__main__":
    main()
