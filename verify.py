"""Checks a built region file: header, directories, every tile readable, dropped layers gone.

Usage: python verify.py FILE.pmtiles [--drop buildings]
"""
import gzip, sys
import build

def main():
    path = sys.argv[1]
    drop = set(sys.argv[sys.argv.index("--drop") + 1].split(",")) if "--drop" in sys.argv else {"buildings"}
    with open(path, "rb") as f:
        data = f.read()
    h = build.read_header(data)
    assert h["internal_comp"] == 2 and h["tile_type"] == 1, h
    def directory(off, ln):
        return build.parse_dir(gzip.decompress(data[off:off + ln]))
    tiles = 0; layers = {}; zooms = set()
    stack = [directory(h["root_offset"], h["root_length"])]
    last = -1
    while stack:
        ids, runs, lens, offs = stack.pop(0)
        for i in range(len(ids)):
            if runs[i] == 0:
                stack.append(directory(h["leaf_offset"] + offs[i], lens[i]))
                continue
            assert ids[i] > last, "tile ids out of order"
            last = ids[i] + runs[i] - 1
            raw = data[h["data_offset"] + offs[i]:h["data_offset"] + offs[i] + lens[i]]
            assert offs[i] + lens[i] <= h["data_length"]
            tile = build.decompress(raw, h["tile_comp"])
            for f, _, v in build.pb_fields(tile):
                if f == 3:
                    n = build.layer_name(v)
                    assert n not in drop, f"{n} still in tile {ids[i]}"
                    layers[n] = layers.get(n, 0) + 1
            tiles += runs[i]
    assert tiles == h["addressed"], (tiles, h["addressed"])
    print(f"ok {path}: {tiles} tiles, zoom {h['min_zoom']}-{h['max_zoom']}, clustered {h['clustered']}, layers {sorted(layers)}")

if __name__ == "__main__":
    main()
