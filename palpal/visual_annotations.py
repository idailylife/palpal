"""Human-reviewed visual annotations for selected scene sprites.

The game engine only exposes a numeric scene sprite.  These annotations are
debug metadata and must never be treated as engine-authored game facts.
"""

LI_XIAOYAO_SCENE_SPRITES = (
    2, 3, 193, 208, 228, 232, 236, 242, 259, 273, 361, 379, 385, 394,
    511, 521, 531, 532, 538, 541, 550, 563, 607, 627, 630, 631, 632,
)

_UNKNOWN_LI_XIAOYAO_SPRITE = {
    sprite_id: {
        "label": f"李逍遥剧情造型 #{sprite_id}",
        "tags": ["li_xiaoyao", "story_sprite"],
        "confidence": "unknown",
        "source": "li_xiaoyao_scene_sprite_v1",
    }
    for sprite_id in LI_XIAOYAO_SCENE_SPRITES
}

LI_XIAOYAO_SCENE_SPRITE_ANNOTATIONS = {
    **_UNKNOWN_LI_XIAOYAO_SPRITE,
    2: {
        "label": "李逍遥常规场景造型",
        "tags": ["li_xiaoyao", "default_scene_sprite"],
        "confidence": "verified",
        "source": "li_xiaoyao_scene_sprite_v1",
    },
    208: {
        "label": "李逍遥端着酒菜餐盘",
        "tags": ["li_xiaoyao", "carrying_food_tray", "story_progress"],
        "confidence": "verified",
        "source": "li_xiaoyao_scene_sprite_v1",
    },
}


def attach_party_visual_annotations(state: dict) -> None:
    """Attach visual metadata to party roles without changing engine facts."""
    party = state.get("party") if isinstance(state, dict) else None
    if not isinstance(party, list):
        return

    for role in party:
        if not isinstance(role, dict):
            continue
        role.pop("visual_annotation", None)
        if role.get("role_id") != 0:
            continue
        try:
            sprite_id = int(role.get("scene_sprite_id"))
        except (TypeError, ValueError):
            continue
        annotation = LI_XIAOYAO_SCENE_SPRITE_ANNOTATIONS.get(sprite_id)
        if annotation is not None:
            role["visual_annotation"] = {
                "label": annotation["label"],
                "tags": list(annotation["tags"]),
                "confidence": annotation["confidence"],
                "source": annotation["source"],
            }
