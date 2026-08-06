"""Resolve story locations to one physical set without losing shot-local zones.

Writers naturally describe a continuous room with progressively narrower
labels (``卧室`` → ``卧室床侧`` → ``卧室至盥洗室门内``).  Those labels are
useful staging zones, but they must not become independent scene universes.
This module gives every scene a stable physical-scene id while preserving the
original ``location`` for dialogue, action and shot descriptions.
"""

from __future__ import annotations


EXPLICIT_SCENE_ROOT_KEYS = (
    "physical_scene_id", "base_location", "scene_family",
)

# A strict suffix allow-list prevents an ordinary shared prefix from merging
# unrelated places.  These words describe a zone/view/route *inside or at the
# boundary of* an already named physical set.
ZONE_PREFIXES = (
    "床", "床侧", "床边", "床前", "床尾", "床头",
    "门", "门内", "门外", "门口", "入口", "出口",
    "窗", "窗边", "窗前", "桌", "桌边", "桌前",
    "沙发", "座位", "角落", "内侧", "外侧", "里侧",
    "盥洗", "浴室", "卫生间", "洗手间", "衣帽间",
    "走廊", "通道", "楼梯", "电梯", "玄关", "阳台",
    "至", "通往", "靠近", "旁", "边", "侧", "前", "后", "内", "外",
)


def _clean(value):
    return str(value or "").strip()


def _location(scene):
    if not isinstance(scene, dict):
        return ""
    return _clean(scene.get("location") or scene.get("name"))


def _explicit_root(scene):
    if not isinstance(scene, dict):
        return ""
    for key in EXPLICIT_SCENE_ROOT_KEYS:
        value = _clean(scene.get(key))
        if value:
            return value
    return ""


def _zone_suffix(location, candidate):
    if not location.startswith(candidate) or location == candidate:
        return ""
    return location[len(candidate):].lstrip(" ·/\\|-—–_：:")


def _looks_like_zone(location, candidate):
    suffix = _zone_suffix(location, candidate)
    return bool(suffix and suffix.startswith(ZONE_PREFIXES))


def scene_family_map(script_or_scenes):
    """Return ``{visible_location: physical_scene_id}``.

    Explicit structured fields always win.  Legacy scripts are upgraded by a
    conservative prefix rule: a narrower location only inherits a known
    broader location when its remainder is a recognised in-set zone.
    """
    if isinstance(script_or_scenes, dict):
        scenes = script_or_scenes.get("scenes") or []
    else:
        scenes = script_or_scenes or []
    scenes = [scene for scene in scenes if isinstance(scene, dict)]
    locations = list(dict.fromkeys(
        location for location in map(_location, scenes) if location))
    explicit = {
        _location(scene): _explicit_root(scene)
        for scene in scenes
        if _location(scene) and _explicit_root(scene)
    }
    mapping = {}
    for location in locations:
        if explicit.get(location):
            mapping[location] = explicit[location]
            continue
        candidates = [
            candidate for candidate in locations
            if _looks_like_zone(location, candidate)]
        # The longest valid parent is the nearest declared physical set.  A
        # transitive pass below then resolves that parent to its ultimate root.
        mapping[location] = max(candidates, key=len) if candidates else location
    for _ in range(len(mapping) + 1):
        changed = False
        for location, parent in tuple(mapping.items()):
            root = mapping.get(parent, parent)
            if root != parent:
                mapping[location] = root
                changed = True
        if not changed:
            break
    return mapping


def canonical_scene_location(script_or_scenes, location):
    location = _clean(location)
    if not location:
        return ""
    return scene_family_map(script_or_scenes).get(location, location)


def annotate_scene_families(script):
    """Persist deterministic scene-family facts into a script in place.

    Returns ``True`` only when fields changed, allowing the caller to create a
    new version exactly once during legacy migration.
    """
    if not isinstance(script, dict):
        return False
    scenes = script.get("scenes") or []
    mapping = scene_family_map(scenes)
    changed = False
    for scene in scenes:
        location = _location(scene)
        if not location:
            continue
        root = mapping.get(location, location)
        for key in ("physical_scene_id", "base_location"):
            if _clean(scene.get(key)) != root:
                scene[key] = root
                changed = True
        if root != location:
            if _clean(scene.get("scene_zone")) != location:
                scene["scene_zone"] = location
                changed = True
        elif scene.pop("scene_zone", None) is not None:
            changed = True
    return changed


def scene_family_groups(script_or_scenes):
    """Return ordered ``{physical_scene_id: [visible locations...]}``."""
    mapping = scene_family_map(script_or_scenes)
    groups = {}
    for location, root in mapping.items():
        groups.setdefault(root, []).append(location)
    return groups
