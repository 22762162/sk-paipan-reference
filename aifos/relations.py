"""生产画布关系图:人物/场景/镜头节点 + 关系线。

图片一开始生产就进入"画布状态":人物之间、人物与场景、场景与镜头之间
都有一条明确的关系线做牵引。这些线不只给人看——出图与质检提示词会带上
同一份关系线,让后续 AI 始终记得"谁和谁是什么关系、谁在哪个场景出现",
避免多镜之间人物关联性漂移。

数据落盘为 <集目录>/relations.json,前端画布(SVG)与提示词共用一份。
"""

import json

SCHEMA = "aifos.relations/v1"


def _scene_label(scene):
    location = (scene.get("location") or "").strip()
    return location or f"第{scene.get('scene_no')}场"


def build_relations(script, storyboard):
    """从剧本+分镜构建关系图:nodes(人物/场景/镜头)+ edges(关系线)。

    线的种类:
      co_scene  人物↔人物:同场关系(牵引一致性,记同过哪些场)
      appears   人物→场景:该人物在此场出现
      contains  场景→镜头:镜头属于该场
      features  人物→镜头:该镜头拍到此人
      sequence  镜头→镜头:同场相邻镜头(首尾帧帧链方向)
    """
    script = script or {}
    storyboard = storyboard or {}
    characters = script.get("characters") or []
    scenes = script.get("scenes") or []
    shots = storyboard.get("shots") or []

    nodes = []
    edges = []
    roles = {}
    for character in characters:
        name = (character.get("name") or "").strip()
        if not name:
            continue
        roles[name] = character.get("role") or ""
        nodes.append({"id": f"char:{name}", "type": "character",
                      "label": name, "role": roles[name]})
    scene_chars = {}
    for scene in scenes:
        scene_no = scene.get("scene_no")
        if scene_no is None:
            continue
        nodes.append({"id": f"scene:{scene_no}", "type": "scene",
                      "label": _scene_label(scene),
                      "scene_no": scene_no})
        present = [n for n in (scene.get("characters") or []) if n in roles]
        scene_chars[scene_no] = present
        for name in present:
            edges.append({"from": f"char:{name}",
                          "to": f"scene:{scene_no}",
                          "type": "appears", "label": "出场"})
    prev_by_scene = {}
    for shot in shots:
        shot_no = shot.get("shot_no")
        scene_no = shot.get("scene_no")
        if shot_no is None:
            continue
        nodes.append({"id": f"shot:{shot_no}", "type": "shot",
                      "label": f"镜{shot_no}", "shot_no": shot_no,
                      "scene_no": scene_no,
                      "characters": list(shot.get("characters") or [])})
        if scene_no is not None:
            edges.append({"from": f"scene:{scene_no}",
                          "to": f"shot:{shot_no}",
                          "type": "contains", "label": "所属"})
            prev = prev_by_scene.get(scene_no)
            if prev is not None:
                edges.append({"from": f"shot:{prev}",
                              "to": f"shot:{shot_no}",
                              "type": "sequence", "label": "帧链"})
            prev_by_scene[scene_no] = shot_no
        for name in shot.get("characters") or []:
            if name in roles:
                edges.append({"from": f"char:{name}",
                              "to": f"shot:{shot_no}",
                              "type": "features", "label": "入镜"})
    # 人物↔人物同场线:牵引后续 AI 记住人物之间的关联性
    pair_scenes = {}
    for scene_no, present in scene_chars.items():
        ordered = sorted(set(present))
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                pair_scenes.setdefault((left, right), []).append(scene_no)
    for (left, right), in_scenes in sorted(pair_scenes.items()):
        label = f"{roles.get(left) or '角色'}×{roles.get(right) or '角色'}"
        edges.append({
            "from": f"char:{left}", "to": f"char:{right}",
            "type": "co_scene",
            "label": f"{label}·同场{len(in_scenes)}场",
            "scenes": sorted(set(in_scenes))})
    return {"schema": SCHEMA, "nodes": nodes, "edges": edges}


def relation_lines(relations, characters):
    """给出图/质检提示词用的"人物关系线"文字:只取与本镜出场人物相关的线。

    例:「周鹿(主角)×小满(同伴):同场3场,形象关系须与此前镜头一致」。
    """
    if not relations or not characters:
        return []
    wanted = set(characters)
    if len(wanted) < 2:
        return []
    lines = []
    for edge in relations.get("edges", []):
        if edge.get("type") != "co_scene":
            continue
        left = edge["from"].split(":", 1)[1]
        right = edge["to"].split(":", 1)[1]
        if left in wanted and right in wanted:
            lines.append(f"{left}与{right}({edge.get('label', '同场')}):"
                         "两人身份与互动关系沿用此前镜头,不得互换形象")
    return lines


def write_relations(out_root, script, storyboard):
    """落盘 relations.json;返回关系图 dict(始终重算,跟随最新分镜)。"""
    relations = build_relations(script, storyboard)
    path = out_root / "relations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(relations, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)
    return relations
