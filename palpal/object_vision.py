"""Bounded, player-visible evidence for on-demand object appearance queries."""
from __future__ import annotations

import copy
import hashlib
import json
import struct
import threading
from collections import OrderedDict

MAX_OBJECT_IMAGES = 128
MAX_OBJECT_IMAGE_BYTES = 8 * 1024 * 1024


def object_frame_metadata(frame: bytes) -> tuple[dict, memoryview] | None:
    """Read the optional atomic POBJ trailer without changing PFRM decoding."""
    if len(frame) < 24:
        raise ValueError("invalid object frame")
    offset = 24 + struct.unpack_from("<I", frame, 20)[0]
    if offset == len(frame):
        return None
    if frame[offset:offset + 4] != b"POBJ" or len(frame) < offset + 8:
        raise ValueError("invalid object trailer")
    size = struct.unpack_from("<I", frame, offset + 4)[0]
    width, height = struct.unpack_from("<HH", frame, 6)
    if size > 65536 or (width, height) != (320, 200):
        raise ValueError("invalid object dimensions")
    end = offset + 8 + size
    if len(frame) != end + width * height * 2:
        raise ValueError("truncated object mask")
    try:
        metadata = json.loads(frame[offset + 8:end])
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid object metadata") from exc
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        raise ValueError("unsupported object metadata")
    return metadata, memoryview(frame)[end:]


class ObjectVision:
    def __init__(self, debug):
        self.debug = debug
        self.lock = threading.RLock()
        self.images = OrderedDict()
        self.image_bytes = 0
        self.context = None
        self.last_frame_hash = None
        self.visible = set()
        self.available = False
        self.generation = 0

    def clear(self):
        with self.lock:
            self.generation += 1
            self.images.clear()
            self.image_bytes = 0
            self.context = None
            self.last_frame_hash = None
            self.visible.clear()
            self.available = False

    def state_context(self, state):
        identity = self.debug.room_identity(state)
        if identity is None:
            return None
        return state.get("game_instance_id"), identity["id"]

    def observe_state(self, state):
        context = self.state_context(state)
        with self.lock:
            if context != self.context:
                self.clear()
                self.context = context

    def capture(self, frame, sequence, received_at, state):
        from palpal.debug_server import pfrm_layout

        parsed = object_frame_metadata(frame)
        self.observe_state(state)
        with self.lock:
            self.visible.clear()
            self.available = parsed is not None
            if parsed is None or self.context is None:
                return
            meta, mask = parsed
            # Only borrow static room geometry from state. Object IDs, pixels,
            # sprite IDs and capture position all come from this exact frame.
            if (meta.get("game_instance_id") != state.get("game_instance_id") or
                    meta.get("scene_id") != state.get("scene_id") or
                    meta.get("map_id") != state.get("map_id") or
                    (meta.get("room") or {}).get("key") != (state.get("room") or {}).get("key")):
                return
            capture_state = {**state, "party_position": meta.get("party_position")}
            capture_state.pop("_logical_room", None)
            if self.state_context(capture_state) != self.context:
                return
            if (meta.get("available") is not True or state.get("in_battle") or
                    state.get("entering_scene") or any(
                        (state.get(key) or {}).get("active") for key in ("dialog", "choice", "shop"))):
                return
            digest = hashlib.sha256(frame).hexdigest()
            if digest == self.last_frame_hash:
                self.visible = {key for key, value in self.images.items() if value["frame_hash"] == digest}
                return
            self.last_frame_hash = digest
            width, height, fmt, pixels, palette = pfrm_layout(frame)
            if fmt != 2 or palette is None:
                return
            descriptors = {o["id"]: o for o in meta.get("objects", [])
                           if isinstance(o, dict) and isinstance(o.get("id"), int)
                           and isinstance(o.get("sprite_id"), int)}
            bounds = {}
            for index, (object_id,) in enumerate(struct.iter_unpack("<H", mask)):
                if not object_id or object_id not in descriptors:
                    continue
                x, y = index % width, index // width
                box = bounds.setdefault(object_id, [x, y, x, y, 0])
                box[0], box[1] = min(box[0], x), min(box[1], y)
                box[2], box[3] = max(box[2], x), max(box[3], y)
                box[4] += 1
            for object_id, (x0, y0, x1, y1, count) in bounds.items():
                descriptor = descriptors[object_id]
                x0, y0, x1, y1 = max(0, x0 - 6), max(0, y0 - 6), min(width, x1 + 7), min(height, y1 + 7)
                cropped = b"".join(bytes(pixels[y * width + x0:y * width + x1]) for y in range(y0, y1))
                selected = bytes(int(struct.unpack_from("<H", mask, (y * width + x) * 2)[0] == object_id)
                                 for y in range(y0, y1) for x in range(x0, x1))
                key = (object_id, descriptor["sprite_id"])
                buffers = (bytes(palette), cropped, selected)
                size = sum(map(len, buffers))
                old = self.images.pop(key, None)
                if old:
                    self.image_bytes -= old["size"]
                self.images[key] = {
                    "generation": self.generation,
                    "object_id": object_id, "sprite_id": key[1], "room_id": self.context[1],
                    "game_instance_id": self.context[0], "frame_sequence": sequence,
                    "raw_frame": struct.unpack_from("<I", frame, 16)[0],
                    "observed_at": received_at, "frame_hash": digest,
                    "screen_bounds": [x0, y0, x1 - x0, y1 - y0],
                    "visible_pixels": count,
                    "partially_occluded": count < descriptor.get("drawn_pixels", count),
                    "screen_clipped": descriptor.get("screen_clipped") is True,
                    "buffers": buffers, "size": size,
                }
                self.visible.add(key)
                self.image_bytes += size
            while len(self.images) > MAX_OBJECT_IMAGES or self.image_bytes > MAX_OBJECT_IMAGE_BYTES:
                key, old = self.images.popitem(last=False)
                self.visible.discard(key)
                self.image_bytes -= old["size"]

    def annotation(self, state, target_id):
        context = self.state_context(state)
        target = self.debug.target_object(state, target_id)
        if context is None or target is None:
            return None
        with self.debug.exploration_lock:
            data = self.debug.read_exploration()
            entry = (data.get("object_visual_annotations", {}).get(context[1], {})
                     .get(str(target_id), {}).get(str(target.get("sprite_id"))))
            return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def view(self, state, target_id):
        target = self.debug.target_object(state, target_id) or {}
        key = (target_id, target.get("sprite_id"))
        annotation = self.annotation(state, target_id)
        context = self.state_context(state)
        with self.debug.exploration_lock:
            data = self.debug.read_exploration()
            records = (data.get("object_visual_annotations", {}).get(context[1], {})
                       .get(str(target_id), {})) if context else {}
            prior = [copy.deepcopy(value) for sprite, value in records.items()
                     if sprite != str(target.get("sprite_id"))][-4:]
        with self.lock:
            image = self.images.get(key) if context == self.context else None
            historical = key not in self.visible or self.debug.live_info("frame").get("stale", True)
            return {
                "annotation": annotation,
                "prior_annotations": prior,
                "annotation_historical": bool(annotation and (historical or not image or
                    annotation["source"]["frame_hash"] != image["frame_hash"])),
                "evidence_status": ("historical" if historical else "current") if image else
                    "not_observed" if self.available else "object_visual_unavailable",
                "observed_at": image["observed_at"] if image else None,
            }

    @staticmethod
    def png(evidence):
        from palpal.debug_server import rgba_to_png

        palette, pixels, selected = evidence["buffers"]
        width, height = evidence["screen_bounds"][2:]
        # Left: unchanged visible surroundings. Right: only target pixels that
        # survived rendering, on neutral gray. No occluded pixels reconstructed.
        rows = bytearray()
        for y in range(height):
            for panel in range(2):
                for x in range(width):
                    index = y * width + x
                    color = pixels[index] * 4
                    rows.extend(palette[color:color + 4] if panel == 0 or selected[index] else b"\x80\x80\x80\xff")
        return rgba_to_png(width * 2, height, bytes(rows))

    def inspect(self, arguments, state, tool_cycle):
        target_id = arguments["target_id"]
        view = self.view(state, target_id)
        if view["annotation"]:
            return {"ok": True, "focus": "object", "authority": "visual_annotation",
                    "cached": True, "target_id": target_id,
                    "historical": view["annotation_historical"], **view}
        target = self.debug.target_object(state, target_id) or {}
        context = self.state_context(state)
        with self.lock:
            evidence = self.images.get((target_id, target.get("sprite_id"))) if context == self.context else None
            if evidence is None:
                return {"ok": False, "error": {"code": view["evidence_status"],
                        "message": "No retained player-visible image for this object. Do not infer its appearance."}}
            evidence = dict(evidence)
            self.images.move_to_end((target_id, target.get("sprite_id")))
        png = self.png(evidence)
        binding = {k: v for k, v in evidence.items() if k not in {"buffers", "size", "generation"}}
        result = self.debug.screen_inspector.inspect(
            arguments, {**state, "_visual_target": binding}, evidence["frame_sequence"],
            png, hashlib.sha256(png).hexdigest(), tool_cycle,
        )
        if not result.get("ok"):
            return result
        latest = self.debug.read_live_state()
        if latest.get("game_instance_id") != evidence["game_instance_id"]:
            return {"ok": False, "error": {"code": "game_instance_changed",
                    "message": "Game instance changed during appearance inspection."}}
        annotation = {
            "authority": "visual_annotation", "object_id": target_id,
            "sprite_id": evidence["sprite_id"], "room_id": evidence["room_id"],
            "model": result["model"], "report": result["report"], "source": binding,
        }
        with self.lock, self.debug.exploration_lock:
            if evidence["generation"] != self.generation:
                return {"ok": False, "error": {"code": "object_visual_context_changed",
                        "message": "Object evidence was cleared during inspection; result was not persisted."}}
            data = self.debug.read_exploration()
            objects = data.setdefault("object_visual_annotations", {}).setdefault(evidence["room_id"], {})
            objects.setdefault(str(target_id), {})[str(evidence["sprite_id"])] = annotation
            self.debug.write_exploration(data)
        latest_view = self.view(latest, target_id)
        return {"ok": True, "focus": "object", "target_id": target_id,
                "authority": "visual_annotation", "cached": result["cached"],
                "historical": (latest_view["annotation_historical"] or latest_view["evidence_status"] != "current" or
                               self.state_context(latest) != (evidence["game_instance_id"], evidence["room_id"]) or
                               (self.debug.target_object(latest, target_id) or {}).get("sprite_id") != evidence["sprite_id"]),
                "annotation": annotation}
