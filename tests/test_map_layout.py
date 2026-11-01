import struct

from palpal.map_layout import compare_region, decode_map
from tests.support import make_debug_state, make_state


def mkf(chunks):
    offset = (len(chunks) + 1) * 4
    offsets = [offset]
    for data in chunks:
        offset += len(data)
        offsets.append(offset)
    return struct.pack('<' + 'I' * len(offsets), *offsets) + b''.join(chunks)


def map_chunk(tiles):
    data = struct.pack('<16384I', *tiles)
    blocks = b''.join(struct.pack('<HH', 16384, 0) + data[i:i+16384] for i in range(0, 65536, 16384))
    return b'YJ_1' + struct.pack('<IIHBB', 65536, 16 + len(blocks), 4, 0, 0) + blocks


def assets(path, *, changed=False, uniform=False):
    # Different sprite table order; visual comparison must not use tile IDs.
    pictures = [bytes([i, i]) for i in range(4)]
    def sprites(images):
        return struct.pack('<5H', 5, 6, 7, 8, 0) + b''.join(images)
    a = [0 if uniform else i % 4 for i in range(16384)]
    b = [(3 - v) | 0x2000 | 0x100 for v in a]
    if changed:
        b[(17 * 64 + 16) * 2] = 0x2000 | 0x100
    (path / 'MAP.MKF').write_bytes(mkf([b'', map_chunk(a), map_chunk(b)]))
    (path / 'GOP.MKF').write_bytes(mkf([b'', sprites(pictures), sprites(pictures[::-1])]))


def query(path):
    return compare_region(path, dict(source_map_id=1, target_map_id=2, x=528, y=280))


def test_same_layout_with_different_sprite_ids_collision_and_height(tmp_path):
    assets(tmp_path)
    result = query(tmp_path)
    assert result['status'] == 'same_coordinate_local_layout'
    assert result['coordinate_candidate'] == {'x': 528, 'y': 280}
    assert result['blocking_differences'] == result['compared_tiles']
    assert result['height_differences'] == result['compared_tiles']


def test_partial_match_does_not_establish_correspondence(tmp_path):
    assets(tmp_path, changed=True)
    result = query(tmp_path)
    assert result['status'] == 'not_established'
    assert result['coordinate_candidate'] is None


def test_uniform_patches_are_not_landmark_evidence(tmp_path):
    assets(tmp_path, uniform=True)
    assert query(tmp_path)['status'] == 'not_established'


def test_missing_invalid_and_unsupported_resources(tmp_path):
    assert query(tmp_path)['error'] == 'map_layout_unavailable'
    assert not compare_region(tmp_path, dict(source_map_id=True, target_map_id=2, x=0, y=0))['ok']
    assets(tmp_path)
    (tmp_path / 'MAP.MKF').write_bytes(b'bad')
    assert query(tmp_path)['error'] == 'map_layout_unavailable'


def test_decoder_roundtrip_and_truncation():
    values = [i % 65536 for i in range(16384)]
    assert decode_map(map_chunk(values)) == tuple(values)
    import pytest
    with pytest.raises(ValueError):
        decode_map(map_chunk(values)[:-1])


def test_tool_is_read_only_and_does_not_merge_rooms(tmp_path):
    debug = make_debug_state(tmp_path)
    debug.asset_dir.mkdir(parents=True, exist_ok=True)
    assets(debug.asset_dir)
    state = make_state()
    before = debug.room_identity(state)
    result = debug.run_agent_tool('compare_map_region', dict(source_map_id=1, target_map_id=2, x=528, y=280), state)
    assert result['status'] == 'same_coordinate_local_layout'
    assert debug.room_identity(state) == before
    assert not (debug.runtime_dir / 'action.txt').exists()
