from palpal.visual_annotations import (
    LI_XIAOYAO_SCENE_SPRITE_ANNOTATIONS,
    LI_XIAOYAO_SCENE_SPRITES,
    attach_party_visual_annotations,
)
from conftest import make_debug_state, make_state


def test_li_xiaoyao_candidate_sprite_set_includes_food_tray_state():
    assert len(LI_XIAOYAO_SCENE_SPRITES) == 27
    assert LI_XIAOYAO_SCENE_SPRITE_ANNOTATIONS[208] == {
        "label": "酒菜餐盘（独立精灵）",
        "tags": ["food_tray", "scene_sprite"],
        "render_role": "party_overlay",
        "semantic_kind": "food_tray",
        "confidence": "verified",
        "source": "mgo_visual_review_v1",
    }


def test_party_visual_annotation_is_limited_to_known_li_xiaoyao_scene_sprites():
    state = {
        "party": [
            {"role_id": 0, "scene_sprite_id": 208},
            {"role_id": 1, "scene_sprite_id": 208},
            {"role_id": 0, "scene_sprite_id": 999},
        ]
    }

    attach_party_visual_annotations(state)

    assert state["party"][0]["visual_annotation"]["label"] == "酒菜餐盘（独立精灵）"
    assert state["party"][0]["visual_overlays"] == [{
        "active": True,
        "sprite_id": 208,
        "label": "酒菜餐盘（独立精灵）",
        "semantic_kind": "food_tray",
        "confidence": "verified",
        "source": "mgo_visual_review_v1",
    }]
    assert "visual_annotation" not in state["party"][1]
    assert "visual_overlays" not in state["party"][1]
    assert "visual_annotation" not in state["party"][2]
    assert "visual_overlays" not in state["party"][2]


def test_debug_state_attaches_annotations_to_agent_context(tmp_path):
    debug = make_debug_state(tmp_path)
    state = make_state()
    state["party"] = [{"role_id": 0, "scene_sprite_id": 208}]

    debug.attach_logical_room_fields(state)
    context = debug.agent_state_summary(state)

    assert context["party"] == [{
        "role_id": 0,
        "scene_sprite_id": 208,
        "visual_annotation": {
            "label": "酒菜餐盘（独立精灵）",
            "tags": ["food_tray", "scene_sprite"],
            "confidence": "verified",
            "source": "mgo_visual_review_v1",
        },
        "visual_overlays": [{
            "active": True,
            "sprite_id": 208,
            "label": "酒菜餐盘（独立精灵）",
            "semantic_kind": "food_tray",
            "confidence": "verified",
            "source": "mgo_visual_review_v1",
        }],
        "level": None,
        "hp": None,
        "max_hp": None,
        "mp": None,
        "max_mp": None,
    }]
