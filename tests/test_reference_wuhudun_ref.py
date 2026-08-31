from reference.wuhudun_ref import first_month_stem, flowing_months, wuhudun


def test_first_month_stem_five_year_groups():
    assert [first_month_stem(stem) for stem in "甲乙丙丁戊"] == ["丙", "戊", "庚", "壬", "甲"]
    assert first_month_stem("己") == "丙"


def test_twelve_months_start_at_yin_and_keep_ganzhi_in_step():
    months = flowing_months("甲")
    assert len(months) == 12
    assert months[0] == {"month": 1, "stem": "丙", "branch": "寅", "ganzhi": "丙寅"}
    assert months[-1]["ganzhi"] == "丁丑"
    assert wuhudun("甲") == [item["ganzhi"] for item in months]
