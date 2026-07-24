from aifos.story_context import build_shot_story_contexts


def test_shot_story_context_keeps_pre_crossing_reaction_shots_modern():
    script = {
        "scenes": [{
            "scene_no": 1,
            "location": "主场景",
            "action": "现代青年在书房查电脑资料，睡着后再睁眼已在明代东宫寝殿。",
            "production_design": {
                "story_function": "开篇穿越觉醒",
                "environment": "明末东宫太子寝殿",
                "time_weather": "崇祯十六年腊月初三戌时三刻",
            },
        }],
    }
    storyboard = {"shots": [
        {"shot_no": 1, "scene_no": 1,
         "description": "现代书房深夜，青年伏案查史料。",
         "script_reference": "电脑上的《明季北略》停在崇祯页面。"},
        {"shot_no": 4, "scene_no": 1,
         "description": "李继周消化青年刚说的话，眼神变化。",
         "script_reference": "李继周消化穿越者刚说的话。"},
        {"shot_no": 5, "scene_no": 1,
         "description": "青年喃喃自语，续评袁崇焕之死。",
         "script_reference": "不是不能杀，是不能这么杀。"},
        {"shot_no": 9, "scene_no": 1,
         "description": "画面切换至明末东宫寝殿深夜，朱慈烺猛地睁眼。",
         "script_reference": "穿越后醒来。"},
    ]}

    contexts = build_shot_story_contexts(script, storyboard)

    assert contexts[5]["era"] == "现代"
    assert contexts[5]["location"] == "现代书房"
    assert contexts[5]["script_excerpt"] == "不是不能杀，是不能这么杀。"
    assert contexts[4]["era"] == "现代"  # reaction shot inherits the timeline
    assert contexts[9]["era"] == "明代"
    assert contexts[9]["location"] == "明末东宫太子寝殿"
