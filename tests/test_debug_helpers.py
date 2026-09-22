import json
import struct

import pytest

from palpal.debug_server import (
    indexed_to_png,
    decode_nav_grid,
    edge_key,
    grid_node,
    nav_bit,
    node_key,
    node_payload,
    pal_distance,
    parse_node_key,
    pfrm_to_png,
    read_env_file,
    read_json,
    read_optional_json,
    reconstruct_route,
    rgba_to_png,
    search_hits,
    search_samples,
    transition_candidate,
    trigger_kind,
    trigger_radius,
    websocket_frame,
    world_pos,
)


def test_json_and_env_helpers_handle_missing_invalid_and_quoted_values(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    valid = tmp_path / "valid.json"
    env_file = tmp_path / ".env"

    invalid.write_text("{bad", encoding="utf-8")
    valid.write_text(json.dumps({"ok": True}), encoding="utf-8")
    env_file.write_text(
        "\n".join(
            [
                "# ignored",
                " PALPAL_HOST = '127.0.0.1' ",
                'PALPAL_MODEL = "deepseek"',
                "BROKEN",
                "=ignored",
            ]
        ),
        encoding="utf-8",
    )

    assert read_json(missing)["status"] == "waiting_for_game"
    assert read_json(invalid)["status"] == "state_parse_error"
    assert read_json(valid) == {"ok": True}
    assert read_optional_json(missing) == {}
    assert read_optional_json(invalid) == {}
    assert read_optional_json(valid) == {"ok": True}
    assert read_env_file(tmp_path / "absent.env") == {}
    assert read_env_file(env_file) == {"PALPAL_HOST": "127.0.0.1", "PALPAL_MODEL": "deepseek"}


def test_navigation_pure_helpers_cover_grid_routes_and_triggers():
    state = {
        "nav_grid": {
            "origin": {"x": 10, "y": 20},
            "step": {"x": 16, "y": 8},
            "width": 3,
            "height": 2,
            "bits": "2f",
        }
    }
    grid = decode_nav_grid(state)

    assert trigger_kind(None) == "none"
    assert trigger_kind(2) == "search"
    assert trigger_kind(4) == "touch"
    assert trigger_radius(None) == 0
    assert trigger_radius(3) == 0
    assert trigger_radius(5) == 48
    assert transition_candidate({"trigger_mode": 4, "sprite_id": 0})
    assert not transition_candidate({"trigger_mode": 3, "sprite_id": 0})
    assert pal_distance((1, 2), (4, 4)) == 7

    assert grid == {"origin": (10, 20), "step": (16, 8), "width": 3, "height": 2, "bits": "2f"}
    assert decode_nav_grid({"nav_grid": {"width": 0, "height": 2, "bits": "f"}}) is None
    assert grid_node(grid, 26, 28) == (1, 1)
    assert grid_node(grid, 27, 28) is None
    assert grid_node(grid, 100, 100) is None
    assert world_pos(grid, (2, 1)) == (42, 28)
    assert nav_bit(grid, (1, 0))
    assert nav_bit(grid, (0, 0)) is False
    assert nav_bit(grid, (10, 10)) is False
    assert node_key((2, 1)) == "2,1"
    assert parse_node_key("2,1") == (2, 1)
    assert parse_node_key("bad") is None
    assert edge_key((0, 0), (1, 1)) == "0,0>1,1"
    assert node_payload(grid, (2, 1)) == {"grid_x": 2, "grid_y": 1, "x": 42, "y": 28}

    route = reconstruct_route(
        grid,
        (0, 0),
        (2, 1),
        {
            (1, 0): ((0, 0), "right"),
            (2, 1): ((1, 0), "down"),
        },
    )
    assert route["moves"] == ["right", "down"]
    assert route["steps"] == 2
    assert route["nodes"][-1] == {"x": 42, "y": 28}


def test_search_sampling_and_hits_cover_match_and_miss_cases():
    samples = search_samples(16, 8, 3)

    assert samples[0] == (16, 8)
    assert samples[1] == (32, 16)
    assert search_hits((16, 8), 3, {"trigger_mode": 1, "x": 32, "y": 16})
    assert not search_hits((16, 8), 3, {"trigger_mode": 0, "x": 32, "y": 16})
    assert not search_hits((16, 8), 3, {"trigger_mode": 4, "x": 32, "y": 16})
    assert not search_hits((16, 8), 3, {"trigger_mode": 1, "x": 320, "y": 160})


def test_websocket_and_png_helpers_validate_sizes_and_headers():
    assert websocket_frame(b"hi") == b"\x81\x02hi"
    assert websocket_frame(b"x" * 126)[:4] == b"\x81~\x00~"
    assert websocket_frame(b"x" * 65536)[:10] == b"\x81\x7f\x00\x00\x00\x00\x00\x01\x00\x00"

    png = rgba_to_png(1, 1, b"\x00\x00\x00\xff")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in png
    with pytest.raises(ValueError, match="invalid frame dimensions"):
        rgba_to_png(0, 1, b"")
    with pytest.raises(ValueError, match="invalid RGBA payload size"):
        rgba_to_png(1, 1, b"")

    indexed = indexed_to_png(
        2,
        1,
        b"\x00\x00\x00\xff\xff\x00\x00\xff",
        b"\x00\x01",
    )
    assert indexed.startswith(b"\x89PNG\r\n\x1a\n")
    indexed_raw = (
        b"PFRM"
        + struct.pack("<HHHH", 2, 2, 1, 2)
        + struct.pack("<I", 2)
        + struct.pack("<I", 1)
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\xff\xff\x00\x00\xff\x00\x01"
    )
    assert pfrm_to_png(indexed_raw).startswith(b"\x89PNG")

    raw = b"PFRM" + struct.pack("<HHHH", 1, 1, 1, 1) + b"\x00" * 8 + struct.pack("<I", 4) + b"\x00\x00\x00\xff"
    assert pfrm_to_png(raw).startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="invalid frame header"):
        pfrm_to_png(b"bad")
    with pytest.raises(ValueError, match="unsupported frame format"):
        pfrm_to_png(b"PFRM" + struct.pack("<HHHH", 2, 1, 1, 1) + b"\x00" * 14)
    with pytest.raises(ValueError, match="invalid frame payload length"):
        pfrm_to_png(b"PFRM" + struct.pack("<HHHH", 1, 1, 1, 1) + b"\x00" * 8 + struct.pack("<I", 8))
