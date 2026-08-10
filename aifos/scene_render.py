"""Deterministic render contracts for the 3D scene viewer.

The panorama and the reconstructed scene model are deliberately kept as two
different fact sources.  A panorama is authoritative only for pixels that are
actually visible; the scene model is authoritative only for geometry it has
successfully reconstructed.  This module never completes occluded geometry.
"""
from __future__ import annotations

import math


SCHEMA = "aifos.scene-render-contract/v1"


# These are neutral viewport presets, not observations about the photographed
# object.  Keeping them category-driven makes rendering deterministic while the
# low confidence communicates that a visual model did not verify the material.
_PBR_PRESETS = {
    "furniture": {"roughness": 0.68, "metalness": 0.04,
                  "emissive_intensity": 0.0},
    "prop": {"roughness": 0.56, "metalness": 0.10,
             "emissive_intensity": 0.0},
    "opening": {"roughness": 0.42, "metalness": 0.02,
                "emissive_intensity": 0.0},
    "light": {"roughness": 0.38, "metalness": 0.06,
              "emissive_intensity": 1.0},
    "decor": {"roughness": 0.64, "metalness": 0.03,
              "emissive_intensity": 0.0},
    "structure": {"roughness": 0.76, "metalness": 0.01,
                  "emissive_intensity": 0.0},
    "surface": {"roughness": 0.74, "metalness": 0.01,
                "emissive_intensity": 0.0},
    "unknown": {"roughness": 0.65, "metalness": 0.0,
                "emissive_intensity": 0.0},
}


# The physics layer intentionally keeps one conservative collision box per
# object.  The viewer, however, should not make a desk, a lattice window and a
# tea cup look like the same translucent cuboid.  These deterministic semantic
# prefabs only change render geometry; they never rewrite measured positions,
# footprints or collision dimensions.
_PREFAB_RULES = (
    ("lantern", ("灯笼", "宫灯", "油灯", "烛台", "蜡烛")),
    ("brazier", ("香炉", "熏炉", "火盆", "炉")),
    ("vessel", ("茶盏", "茶杯", "酒杯", "水杯", "小碗", "瓷碗", "杯", "盏")),
    ("cabinet", ("药柜", "书架", "书柜", "卷柜", "格柜", "抽屉", "柜")),
    ("paper", ("宣纸", "信笺", "纸卷", "书卷", "卷册", "书册", "竹简", "文书")),
    ("bed", ("床榻", "卧榻", "床", "榻")),
    ("chair", ("椅", "凳", "坐墩")),
    ("lattice", ("格心窗", "格栅", "隔扇", "花窗", "槛窗", "支摘窗")),
    ("doorway", ("门洞", "门道", "入口", "出口")),
    ("door", ("板门", "木门", "房门", "车门", "门扇")),
    ("curtain", ("竹帘", "布帘", "纱帘", "垂帘", "帷幔", "纱帐", "帐幔")),
    ("column", ("方柱", "圆柱", "檐柱", "立柱", "柱")),
    ("screen", ("屏风", "屏障", "围屏")),
    ("table", ("书案", "画案", "条案", "矮案", "琴案", "案几", "方几", "茶几",
               "桌", "台几")),
    # instrument 必须排在 table 之后:琴案/琴桌先按案桌归位,余下的琴才是乐器
    ("instrument", ("古琴", "瑶琴", "琵琶", "琴")),
    ("rug", ("地毯", "毛毯", "地衣", "草席", "凉席", "竹席", "蒲团")),
    ("plant", ("盆栽", "盆景", "绿植", "翠竹", "竹丛", "兰草", "花木")),
)


def _render_prefab(name, category):
    text = _text(name).lower()
    for prefab_type, words in _PREFAB_RULES:
        if any(word.lower() in text for word in words):
            return {
                "type": prefab_type,
                "version": 1,
                "source": "semantic_name",
                "confidence": {"score": 0.85, "level": "high",
                               "basis": ["object.name"]},
            }
    fallback = {
        "opening": "architectural_frame",
        "light": "lantern",
        "furniture": "generic_furniture",
        "prop": "generic_prop",
        "decor": "decor_panel",
    }.get(category, "generic_object")
    return {
        "type": fallback,
        "version": 1,
        "source": "category_fallback",
        "confidence": {"score": 0.3, "level": "low",
                       "basis": ["object.category"]},
    }


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    """Return a finite JSON number, never NaN/Infinity or a bool."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return round(result, 4)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _version(value):
    number = _number(value)
    if number is not None:
        return int(number) if number.is_integer() else number
    return _text(value) or None


def _warning(code, field, message, *, object_name=""):
    warning = {"code": code, "field": field, "message": message}
    if object_name:
        warning["object"] = object_name
    return warning


def _confidence(known, total, basis):
    score = round(known / total, 2) if total else 0.0
    level = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    return {"score": score, "level": level, "basis": list(basis)}


def _position(obj):
    source = _mapping(obj.get("position_3d") or obj.get("position"))
    return {axis: _number(source.get(axis)) for axis in ("x", "y", "z")}


def _rotation(obj):
    source = obj.get("rotation_deg")
    if isinstance(source, dict):
        return {
            "x_deg": _number(source.get("x_deg", source.get("x"))),
            "y_deg": _number(source.get(
                "y_deg", source.get("y", source.get("yaw")))),
            "z_deg": _number(source.get("z_deg", source.get("z"))),
        }
    yaw = _number(obj.get("rotation_y_deg"))
    if yaw is None:
        yaw = _number(obj.get("yaw_deg"))
    if yaw is None and not isinstance(source, dict):
        yaw = _number(source)
    # Pitch and roll are unknown, not implicitly zero.
    return {"x_deg": None, "y_deg": yaw, "z_deg": None}


def _footprint(obj):
    result = []
    raw = obj.get("footprint_3d")
    if not isinstance(raw, list):
        return result
    for point in raw:
        point = _mapping(point)
        x, y, z = (_number(point.get(axis)) for axis in ("x", "y", "z"))
        if x is None or z is None:
            continue
        result.append({"x": x, "y": y, "z": z})
    return result


_GEOMETRY_SOURCE_SCORES = {
    "panorama_floor_intersection": 0.95,
    "panorama_angular_span": 0.85,
    "panorama_vertical_ray": 0.85,
    "visual_annotation": 0.7,
    "category_default": 0.25,
    "radial_fallback": 0.25,
}


def _geometry_confidence(values, sources):
    """Score source quality, not merely whether a fallback value exists."""
    field_scores = {}
    for field, value in values.items():
        semantic_field = "position" if field.startswith("position.") else field
        if semantic_field == "rotation.y_deg":
            semantic_field = "rotation"
        source = _text(sources.get(semantic_field))
        field_scores[field] = (
            _GEOMETRY_SOURCE_SCORES.get(source, 0.5)
            if value is not None else 0.0)
    score = round(
        sum(field_scores.values()) / len(field_scores), 2
    ) if field_scores else 0.0
    level = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    return {
        "score": score,
        "level": level,
        "sources": dict(sources),
        "field_scores": field_scores,
    }


def _explicit_material(obj):
    source = obj.get("material")
    if isinstance(source, dict):
        return source
    return {}


def _material_contract(obj, category):
    preset_name = category if category in _PBR_PRESETS else "unknown"
    preset = _PBR_PRESETS[preset_name]
    explicit = _explicit_material(obj)
    roughness = _number(explicit.get("roughness"))
    metalness = _number(explicit.get("metalness"))
    emissive = _number(explicit.get("emissive_intensity"))
    base_color = explicit.get("base_color")
    if isinstance(base_color, str):
        base_color = base_color.strip() or None
    elif isinstance(base_color, (list, tuple)):
        channels = [_number(channel) for channel in base_color]
        base_color = channels if len(channels) in (3, 4) and all(
            channel is not None for channel in channels) else None
    else:
        base_color = None
    declared_name = _text(obj.get("material_name") or obj.get("material"))
    has_explicit = bool(explicit or declared_name)
    contract = {
        "model": "pbr-metallic-roughness",
        "preset": preset_name,
        "roughness": (roughness if roughness is not None
                      else preset["roughness"]),
        "metalness": (metalness if metalness is not None
                      else preset["metalness"]),
        "emissive_intensity": (
            emissive if emissive is not None
            else preset["emissive_intensity"]),
        "base_color": base_color,
        "declared_name": declared_name or None,
        "source": ("declared_with_category_fallback" if has_explicit
                   else "category_render_default"),
    }
    confidence = {
        "score": 0.75 if has_explicit else 0.3,
        "level": "medium" if has_explicit else "low",
        "basis": (["scene_model.material"] if has_explicit else
                  ["category_render_default_not_observed"]),
    }
    return contract, confidence


def _object_contract(raw, index):
    obj = _mapping(raw)
    name = _text(obj.get("name")) or f"object-{index}"
    category = _text(obj.get("category")).lower() or "unknown"
    position = _position(obj)
    width = _number(obj.get("width_m", obj.get("width")))
    height = _number(obj.get("height_m", obj.get("height")))
    depth = _number(obj.get("depth_m", obj.get("depth")))
    rotation = _rotation(obj)
    footprint = _footprint(obj)
    geometry_sources = {
        str(key): _text(value)
        for key, value in _mapping(obj.get("geometry_sources")).items()
        if _text(key) and _text(value)
    }
    warnings = []

    geometry_values = {
        "position.x": position["x"],
        "position.y": position["y"],
        "position.z": position["z"],
        "width": width,
        "height": height,
        "depth": depth,
        "rotation.y_deg": rotation["y_deg"],
    }
    for field, value in geometry_values.items():
        if value is None:
            warnings.append(_warning(
                "missing_geometry", field,
                f"「{name}」缺少 {field}；渲染合同保留为空，不猜测不可见几何",
                object_name=name))
    for field, source in geometry_sources.items():
        if source == "category_default":
            warnings.append(_warning(
                "category_default_geometry", field,
                f"「{name}」的 {field} 使用类别默认尺寸，仅供搭景占位，"
                "不是全景实测值",
                object_name=name))
        elif source == "radial_fallback":
            warnings.append(_warning(
                "fallback_rotation", field,
                f"「{name}」的朝向为径向回退值，并非视觉标注",
                object_name=name))
    material, material_confidence = _material_contract(obj, category)
    render_prefab = _render_prefab(name, category)
    if material["source"] == "category_render_default":
        warnings.append(_warning(
            "unverified_material", "material",
            f"「{name}」没有已验证材质，当前 PBR 参数仅为分类渲染预设",
            object_name=name))

    return {
        "name": name,
        "category": category,
        "position": position,
        "width": width,
        "height": height,
        "depth": depth,
        "rotation": rotation,
        "footprint_3d": footprint,
        "geometry_sources": geometry_sources,
        "material": material,
        "render_prefab": render_prefab,
        "geometry_confidence": _geometry_confidence(
            geometry_values, geometry_sources),
        "material_confidence": material_confidence,
        "occlusion_completion": "unverified",
        "warnings": warnings,
    }


_SUPPORT_MARKERS = (
    ("案上", ("案", "桌", "几", "台")),
    ("桌上", ("桌", "案", "几", "台")),
    ("几上", ("几", "桌", "案", "台")),
    ("台上", ("台", "几", "桌", "案")),
    ("柜上", ("柜",)),
    ("床上", ("床", "榻")),
)


def _explicit_support_words(name):
    text = _text(name)
    for marker, words in _SUPPORT_MARKERS:
        if marker in text:
            return marker, words
    return "", ()


def _render_position_on_support(obj, support):
    """Return a visual placement on an explicitly named support surface.

    This is deliberately a render-only transform.  The panorama-derived
    position remains untouched for audit and physics.  If an annotation says
    "案上" while its panorama floor ray lands just beyond the desk footprint,
    the prop is clamped onto the visible top instead of being rendered on the
    floor or floating beside the desk.
    """
    position = _mapping(obj.get("position"))
    base = _mapping(support.get("position"))
    values = [position.get("x"), position.get("z"),
              base.get("x"), base.get("z"),
              support.get("width"), support.get("depth"),
              support.get("height")]
    if any(_number(value) is None for value in values):
        return None
    ox, oz, sx, sz, width, depth, height = map(_number, values)
    yaw = _number(_mapping(support.get("rotation")).get("y_deg")) or 0.0
    radians = math.radians(yaw)
    cos_yaw, sin_yaw = math.cos(radians), math.sin(radians)
    dx, dz = ox - sx, oz - sz
    local_x = dx * cos_yaw - dz * sin_yaw
    local_z = dx * sin_yaw + dz * cos_yaw
    prop_width = _number(obj.get("width")) or 0.0
    prop_depth = _number(obj.get("depth")) or 0.0
    margin = 0.03
    limit_x = max(0.0, width / 2.0 - prop_width / 2.0 - margin)
    limit_z = max(0.0, depth / 2.0 - prop_depth / 2.0 - margin)
    local_x = max(-limit_x, min(limit_x, local_x))
    local_z = max(-limit_z, min(limit_z, local_z))
    world_x = sx + local_x * cos_yaw + local_z * sin_yaw
    world_z = sz - local_x * sin_yaw + local_z * cos_yaw
    support_y = _number(base.get("y")) or 0.0
    return {
        "x": round(world_x, 4),
        "y": round(support_y + height, 4),
        "z": round(world_z, 4),
    }


def _attach_render_supports(objects):
    for obj in objects:
        position = dict(_mapping(obj.get("position")))
        obj["render_transform"] = {
            "position": position,
            "source": "measured_geometry",
            "support": None,
        }
        marker, support_words = _explicit_support_words(obj.get("name"))
        if not marker:
            continue
        candidates = [
            candidate for candidate in objects
            if candidate is not obj
            and any(word in _text(candidate.get("name"))
                    for word in support_words)
            and _number(_mapping(candidate.get("position")).get("x"))
            is not None
            and _number(_mapping(candidate.get("position")).get("z"))
            is not None
        ]
        ox = _number(position.get("x"))
        oz = _number(position.get("z"))
        if ox is None or oz is None or not candidates:
            continue
        support = min(candidates, key=lambda candidate: math.hypot(
            ox - _number(_mapping(candidate.get("position")).get("x")),
            oz - _number(_mapping(candidate.get("position")).get("z"))))
        render_position = _render_position_on_support(obj, support)
        if render_position is None:
            continue
        obj["render_transform"] = {
            "position": render_position,
            "source": "explicit_name_support",
            "support": {
                "name": support.get("name"),
                "relation": marker,
                "confidence": {"score": 0.75, "level": "medium",
                               "basis": ["object.name", "nearest_support"]},
            },
        }
        obj["warnings"].append(_warning(
            "render_support_inference", "render_transform.position",
            f"「{obj.get('name')}」按名称中的「{marker}」放到"
            f"「{support.get('name')}」顶面；原始全景落地点仍保留用于审计",
            object_name=_text(obj.get("name"))))


def _room_contract(scene_model):
    room = _mapping(scene_model.get("room"))
    observed = {
        "width": _number(room.get("floor_width_m", room.get("width_m"))),
        "height": _number(room.get("wall_height_m", room.get("height_m"))),
        "depth": _number(room.get("floor_depth_m", room.get("depth_m"))),
    }
    defaults = {"width": 10.0, "height": 4.2, "depth": 7.0}
    fields = {
        name: value if value is not None else defaults[name]
        for name, value in observed.items()
    }
    known = [name for name, value in observed.items() if value is not None]
    fields.update({
        "units": "metres",
        "environment": _text(
            room.get("environment") or scene_model.get("environment")) or None,
        "geometry_sources": {
            name: "scene_model.room" if name in known else "render_default"
            for name in observed
        },
        "geometry_confidence": _confidence(len(known), 3, known),
    })
    return fields


def _surface_contracts(room):
    material = {
        "model": "pbr-metallic-roughness",
        "preset": "surface",
        **_PBR_PRESETS["surface"],
        "source": "category_render_default",
    }
    return [
        {
            "name": name,
            "geometry": geometry,
            "material": dict(material),
            "material_confidence": {
                "score": 0.3, "level": "low",
                "basis": ["category_render_default_not_observed"],
            },
            "occlusion_completion": "unverified",
        }
        for name, geometry in (
            ("floor", {"width": room["width"], "depth": room["depth"]}),
            ("walls", {"width": room["width"], "height": room["height"],
                       "depth": room["depth"]}),
            ("ceiling", {"width": room["width"], "depth": room["depth"],
                         "height": room["height"]}),
        )
    ]


def _lighting_contract(scene_model, room):
    source = _mapping(
        scene_model.get("lighting") or room.get("lighting"))
    ambient = _number(source.get("ambient_intensity"))
    key_intensity = _number(source.get("key_intensity"))
    color_temperature = _number(source.get("color_temperature_k"))
    known = [name for name, value in (
        ("ambient_intensity", ambient),
        ("key_intensity", key_intensity),
        ("color_temperature_k", color_temperature),
    ) if value is not None]
    direction_source = _mapping(source.get("direction"))
    direction = {
        axis: _number(direction_source.get(axis))
        for axis in ("x", "y", "z")
    } if direction_source else None
    if direction is not None and all(value is None for value in
                                     direction.values()):
        direction = None
    return {
        "ambient_intensity": ambient,
        "key_intensity": key_intensity,
        "color_temperature_k": color_temperature,
        "direction": direction,
        "source": "scene_model" if source else "unverified",
        "confidence": _confidence(len(known), 3, known),
    }


def build_scene_render_contract(scene_model, panorama_info=None, *,
                                location=""):
    """Build a JSON-safe, non-generative scene rendering contract.

    Unknown values remain ``None`` and are surfaced in ``warnings``.  In
    particular, hidden sides of objects and unobserved room areas are never
    completed from category priors.
    """
    model_missing = scene_model is None
    panorama_missing = panorama_info is None
    model_valid = isinstance(scene_model, dict)
    panorama_valid = isinstance(panorama_info, dict)
    model = scene_model if model_valid else {}
    panorama = panorama_info if panorama_valid else {}
    resolved_location = (_text(location) or _text(model.get("location")))
    panorama_url = _text(panorama.get("url") or model.get("panorama_uri"))
    warnings = []
    if model_missing:
        warnings.append(_warning(
            "missing_scene_model", "scene_model",
            "缺少场景几何模型；合同只保留可验证的全景外观层"))
    elif not model_valid:
        warnings.append(_warning(
            "invalid_scene_model", "scene_model",
            "scene_model 不是 JSON 对象；已按空场景处理"))
    if not panorama_missing and not panorama_valid:
        warnings.append(_warning(
            "invalid_panorama_info", "panorama_info",
            "panorama_info 不是 JSON 对象；已按无全景处理"))
    if not panorama_url:
        warnings.append(_warning(
            "missing_panorama", "panorama.url",
            "缺少全景母版；可见外观层不可验证"))

    raw_objects = model.get("objects")
    if not isinstance(raw_objects, list):
        if raw_objects is not None:
            warnings.append(_warning(
                "invalid_objects", "scene_model.objects",
                "objects 必须是数组；已忽略损坏的数据"))
        raw_objects = []
    objects = [_object_contract(item, index)
               for index, item in enumerate(raw_objects, 1)
               if isinstance(item, dict)]
    skipped = len(raw_objects) - len(objects)
    if skipped:
        warnings.append(_warning(
            "invalid_object", "scene_model.objects",
            f"已忽略 {skipped} 个非对象条目"))

    room_source = _mapping(model.get("room"))
    room = _room_contract(model)
    for field in ("width", "height", "depth"):
        if room["geometry_sources"][field] == "render_default":
            warnings.append(_warning(
                "missing_room_geometry", f"room.{field}",
                f"房间缺少 {field}；查看器使用明确标记的搭景默认值 "
                f"{room[field]}m，该值不是场景事实"))
    lighting = _lighting_contract(model, room_source)
    if lighting["source"] == "unverified":
        warnings.append(_warning(
            "missing_lighting", "lighting",
            "场景没有已验证灯光参数；合同不推断光源方向或强度"))
    _attach_render_supports(objects)
    for obj in objects:
        warnings.extend(obj["warnings"])

    return {
        "schema": SCHEMA,
        "location": resolved_location,
        "source_layers": {
            "panorama": {
                "authority": "visible_appearance_only",
                "url": panorama_url or None,
                "version": _version(panorama.get("version")),
                "status": "available" if panorama_url else "missing",
                "unseen_regions": "unverified",
            },
            "scene_model": {
                "authority": "reconstructed_geometry_only",
                "schema": _text(model.get("schema")) or None,
                "status": "available" if model_valid and model else "missing",
            },
            "merge_policy": (
                "全景仅证明已看见区域的外观；scene_model 仅证明已解出的"
                "位置与尺寸。冲突或缺失时保留未知，不生成遮挡区事实。"),
        },
        "room": room,
        "surfaces": _surface_contracts(room),
        "objects": objects,
        "lighting": lighting,
        "warnings": warnings,
        "occlusion_completion": "unverified",
    }
