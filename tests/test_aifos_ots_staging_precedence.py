"""过肩构图 vs 空间站位投影的显式裁决(12星座 frames 阶段熔断回归)。

真实事故:j51 frames 阶段,镜头设计为过肩全景(林未=前景过肩人物),
而 3D 空间站位按场景基准机位投影出「林未 6.7 米、顾衡 5.4 米、遮挡
顾衡→林未」,两项同级事实互斥且无显式裁决条款,审核按规则阻断。
修复后:过肩构图条款自带执行裁决,空间站位的距离/遮挡数值降级为
基准机位投影的参考值。
"""

from aifos.prompt_contract import compile_shot_prompt


def _ots_shot():
    """复刻 12星座 冲突结构:过肩前景人物在基准投影中反而更远。"""
    return {
        "shot_no": 7,
        "characters": ["林未", "顾衡"],
        "description": "从林未肩后过肩取景,顾衡正从右侧楼梯下台。",
        "camera": "全景；仰拍；35mm；过肩",
        "frame_target": {"phase": "start", "state": "顾衡下台,林未中央站位"},
        "start_state": {},
        "end_state": {},
        "spatial_blocking": {
            "actors": [
                # 基准投影里林未距相机更远——与过肩前景身份矛盾
                {"name": "林未",
                 "start_3d": {"x": -1.5, "y": 0, "z": -1.3},
                 "end_3d": {"x": -1.5, "y": 0, "z": -1.3},
                 "height_m": 1.65, "facing": "面向顾衡"},
                {"name": "顾衡",
                 "start_3d": {"x": 1.0, "y": 0, "z": -2.6},
                 "end_3d": {"x": 1.0, "y": 0, "z": -2.6},
                 "height_m": 1.78, "facing": "面向林未"},
            ],
            "camera": {
                "start_3d": {"x": 0, "y": 1.5, "z": -8.0},
                "end_3d": {"x": 0, "y": 1.5, "z": -8.0},
                "target_3d": {"x": 0, "y": 1.2, "z": 0},
                "fov_degrees": 40.0,
                "movement": "固定",
            },
            "dialogue_continuity": {
                "axis_id": "S01",
                "screen_left_name": "林未",
                "screen_right_name": "顾衡",
            },
        },
    }


def test_ots_composition_detected_and_carries_execution_ruling():
    contract, prompt = compile_shot_prompt(
        _ots_shot(), location="穹顶直播棚", mode="image")
    assert contract["composition"]["composition_type"] == "over_shoulder_dialogue"

    assert "【过肩构图】" in prompt
    assert "本条是本镜唯一执行构图裁决" in prompt
    # 全景全身与前景半身的表面互斥被显式消解
    assert "只约束画面主体" in prompt
    assert "不约束过肩前景人物" in prompt
    # 站位数值被降级为基准机位投影的参考,不再是同级冲突源
    assert "基准机位投影" in prompt
    assert "不构成需要裁决的冲突" in prompt


def test_staging_geography_is_preserved_not_deleted():
    """裁决只降级相机相对数值,屏幕轴线与相对位置仍保留供核验。"""
    contract, prompt = compile_shot_prompt(
        _ots_shot(), location="穹顶直播棚", mode="image")
    staging_text = " ".join(str(v) for v in contract["spatial_staging"].values())
    assert "空间站位" in contract["spatial_staging"]
    assert "屏幕方向" in contract["spatial_staging"] or "轴线" in staging_text


def test_camera_precedence_covers_staging_projection_values():
    contract, _ = compile_shot_prompt(
        _ots_shot(), location="穹顶直播棚", mode="image")
    precedence = contract["camera_precedence"]
    assert "基准机位的投影值" in precedence
    assert "重新投影" in precedence
    assert "不构成同级互斥" in precedence
    # 广义投影族:画面内/外归属、屏幕左右与遮挡顺序一并降级
    assert "画面内/外归属" in precedence
    assert "轴线锚点合同为执行值" in precedence
    assert "population" in precedence


def test_non_ots_shot_not_polluted_by_ots_ruling():
    """普通镜头不应出现过肩裁决条款。"""
    shot = _ots_shot()
    shot["camera"] = "全景；平视；35mm"
    shot["description"] = "顾衡正从右侧楼梯下台,林未保持中央站位。"
    contract, prompt = compile_shot_prompt(
        shot, location="穹顶直播棚", mode="image")
    assert contract["composition"]["composition_type"] != "over_shoulder_dialogue"
    assert "本条是本镜唯一执行构图裁决" not in prompt
    # camera_precedence 的空间站位投影条款对普通镜头同样适用且无害
    assert "基准机位的投影值" in contract["camera_precedence"]
