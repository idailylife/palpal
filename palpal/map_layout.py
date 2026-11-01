"""Read-only local layout comparisons, independent of navigation connectivity.

DOS YJ_1 decoding follows vendor/sdlpal/yj1.c (GPL-3.0-or-later).
No map IDs or story identities are assigned special meaning.
"""
from __future__ import annotations

import hashlib
import struct
from functools import lru_cache
from pathlib import Path


class Bits:
    def __init__(self, data):
        self.data, self.position = data, 0

    def get(self, count):
        if not 0 <= count <= 16:
            raise ValueError('invalid bit count')
        value = 0
        for _ in range(count):
            word = struct.unpack_from('<H', self.data, (self.position // 16) * 2)[0]
            value = (value << 1) | ((word >> (15 - self.position % 16)) & 1)
            self.position += 1
        return value


def decode_map(data):
    if data[:4] != b'YJ_1' or len(data) < 16:
        raise ValueError('only DOS YJ_1 map resources are supported')
    size, compressed, blocks = struct.unpack_from('<IIH', data, 4)
    if size != 65536 or compressed > len(data):
        raise ValueError('invalid map size')
    tree_size = data[15] * 2
    flags = Bits(data[16 + tree_size:16 + tree_size + ((tree_size + 15) // 16) * 2])
    tree = [(False, 0)] + [(not flags.get(1), v) for v in data[16:16 + tree_size]]
    offset = 16 + tree_size + ((tree_size + 15) // 16) * 2
    out = bytearray()
    for _ in range(blocks):
        raw_size, packed_size = struct.unpack_from('<HH', data, offset)
        start = len(out)
        if not packed_size:
            out.extend(data[offset + 4:offset + 4 + raw_size])
            offset += 4 + raw_size
        else:
            if packed_size < 24 or offset + packed_size > len(data):
                raise ValueError('invalid block')
            header = data[offset:offset + 24]
            repeats = struct.unpack_from('<4H', header, 4)
            bits = Bits(data[offset + 24:offset + packed_size])

            def loop():
                if bits.get(1):
                    return header[22]
                index = bits.get(2)
                return bits.get(header[18 + index]) if index else header[23]

            while True:
                literals = loop()
                if not literals:
                    break
                for _ in range(literals):
                    node = 0
                    for _ in range(tree_size + 1):
                        leaf, value = tree[node]
                        if leaf:
                            out.append(value)
                            break
                        node = value * 2 + 1 + bits.get(1)
                    else:
                        raise ValueError('cyclic Huffman tree')
                copies = loop()
                if not copies:
                    break
                for _ in range(copies):
                    index = bits.get(2)
                    count = (bits.get(header[15 + index]) if bits.get(1) else repeats[index]) if index else repeats[0]
                    distance = bits.get(header[12 + bits.get(2)])
                    if not 0 < distance <= len(out) or len(out) + count > size:
                        raise ValueError('invalid back reference')
                    for _ in range(count):
                        out.append(out[-distance])
                if len(out) > size:
                    raise ValueError('map overflow')
            offset += packed_size
        if len(out) - start != raw_size or len(out) > size:
            raise ValueError('invalid decompressed block length')
    if len(out) != size:
        raise ValueError('invalid decompressed map length')
    return struct.unpack('<16384I', out)


def chunk(data, index):
    if len(data) < 8:
        raise ValueError('invalid MKF')
    table_size = struct.unpack_from('<I', data)[0]
    if table_size % 4 or not 0 < index < table_size // 4 - 1:
        raise ValueError('map not present')
    start, end = struct.unpack_from('<II', data, index * 4)
    if not table_size <= start <= end <= len(data):
        raise ValueError('invalid MKF offsets')
    return data[start:end]


@lru_cache(maxsize=16)
def _load(map_path, gop_path, map_stat, gop_stat, map_id):
    tiles = decode_map(chunk(Path(map_path).read_bytes(), map_id))
    sprites = chunk(Path(gop_path).read_bytes(), map_id)
    count = struct.unpack_from('<H', sprites)[0] - 1
    offsets = list(struct.unpack_from('<' + 'H' * (count + 1), sprites))
    if offsets[-1] == 0:  # DOS GOP uses a zero terminal offset.
        offsets[-1] = len(sprites) // 2
    if count < 1 or any(a > b for a, b in zip(offsets, offsets[1:])) or offsets[-1] * 2 > len(sprites):
        raise ValueError('invalid sprite table')
    images = [hashlib.sha256(sprites[offsets[i]*2:offsets[i+1]*2]).digest() for i in range(count)]

    def layer(d, upper):
        index = (d & 255) | ((d >> 4) & 256)
        index -= upper
        return images[index] if 0 <= index < count else None

    # Compare actual image bytes, not map-local sprite indices. Retain height
    # metadata for separate reporting; neither height nor collision defines
    # whether the background pictures occupy the same coordinates.
    visuals = tuple((layer(d, 0), layer(d >> 16, 1), (d >> 8) & 15, (d >> 24) & 15) for d in tiles)
    return tiles, visuals


def compare_region(asset_dir, arguments):
    keys = ('source_map_id', 'target_map_id', 'x', 'y')
    if any(type(arguments.get(k)) is not int for k in keys):
        return {'ok': False, 'error': 'map IDs and coordinates must be integers'}
    source, target, x, y = (arguments[k] for k in keys)
    if not (source > 0 and target > 0 and 0 <= x < 2048 and 0 <= y < 2048):
        return {'ok': False, 'error': 'invalid map IDs or map coordinates'}
    try:
        paths = [Path(asset_dir) / name for name in ('MAP.MKF', 'GOP.MKF')]
        stamps = [(p.stat().st_mtime_ns, p.stat().st_size) for p in paths]
        a, va = _load(str(paths[0]), str(paths[1]), *stamps, source)
        b, vb = _load(str(paths[0]), str(paths[1]), *stamps, target)
        cx, cy = x // 32, y // 16
        x0, x1 = max(0, cx - 4), min(64, cx + 5)
        y0, y1 = max(0, cy - 4), min(128, cy + 5)
        indices = [(row * 64 + col) * 2 + h for row in range(y0, y1) for col in range(x0, x1) for h in (0, 1)]
        matches = sum(va[i][:2] == vb[i][:2] for i in indices)
        diverse = len({va[i][:2] for i in indices}) >= 4
        verified = matches == len(indices) and diverse
        return {
            'ok': True, 'source_map_id': source, 'target_map_id': target,
            'basis': 'local_MAP_GOP_resources',
            'status': 'same_coordinate_local_layout' if verified else 'not_established',
            'coordinate_candidate': {'x': x, 'y': y} if verified else None,
            'tile_bounds': {'x_min': x0, 'x_max_exclusive': x1, 'y_min': y0, 'y_max_exclusive': y1},
            'matching_tiles': matches, 'compared_tiles': len(indices),
            'distinctive_patch': diverse,
            'height_differences': sum(va[i][2:] != vb[i][2:] for i in indices),
            'blocking_differences': sum(bool(a[i] & 0x2000) != bool(b[i] & 0x2000) for i in indices),
            'meaning': '仅验证所列局部区域的同坐标布局；不确认地标身份、当前剧情状态、跨图通路或可达性。换图后用当前导航核查该坐标附近目标。',
        }
    except (OSError, ValueError, IndexError, struct.error) as exc:
        return {'ok': False, 'error': 'map_layout_unavailable', 'detail': str(exc)}
