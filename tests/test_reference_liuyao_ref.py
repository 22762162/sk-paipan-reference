from reference.liuyao_ref import liuyao


def test_qian_palace_golden_installation():
    result = liuyao([7, 7, 7, 7, 7, 7], "甲子")
    assert result["original_name"] == "乾为天"
    assert result["changed_name"] == "乾为天"
    assert result["palace"] == "乾"
    assert result["palace_sequence"] == "本宫"
    assert result["shi"] == 6 and result["ying"] == 3
    assert result["na_jia_branches"] == list("子寅辰午申戌")
    assert result["six_spirits"] == ["青龙", "朱雀", "勾陈", "螣蛇", "白虎", "玄武"]
    assert result["void_branches"] == ["戌", "亥"]


def test_changing_first_yao_reaches_qian_palace_one_shi_gua():
    result = liuyao([6, 7, 7, 7, 7, 7], "甲子")
    assert result["original_name"] == "天风姤"
    assert result["changed_name"] == "乾为天"
    assert result["palace"] == "乾"
    assert result["palace_sequence"] == "一世"
    assert result["shi"] == 1 and result["ying"] == 4
    assert result["yao"][0]["changing"] is True


def test_day_stem_changes_six_spirit_start():
    assert liuyao([7] * 6, "丙子")["six_spirits"] == [
        "朱雀", "勾陈", "螣蛇", "白虎", "玄武", "青龙"
    ]
