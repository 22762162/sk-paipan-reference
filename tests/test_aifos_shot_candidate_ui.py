from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).parents[1]
JS = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
    encoding="utf-8"
)
CSS = (ROOT / "aifos" / "web" / "static" / "style.css").read_text(
    encoding="utf-8"
)


def _run_node(source):
    completed = subprocess.run(
        ["node", "-e", source], check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_shot_candidate_statuses_and_ai_auto_selection_rendering_are_present():
    for status in (
        "awaiting_selection",
        "regenerating_candidates",
        "technical_incomplete",
    ):
        assert status in JS
    assert "function shotCandidateGridHtml(item, editable)" in JS
    assert 'class="shot-candidate-grid${selected ? " selected-only" : ""}"' in JS
    assert "const batchLabel = state.roundLabel" in JS
    assert "const expected = Math.min(4, Math.max(1" in JS
    assert "group.generation_round" in JS
    assert "group.max_candidate_rounds" in JS
    assert "group.current_round_progress" in JS
    assert "第${generationRound}/${maxCandidateRounds}轮 · ${currentRoundProgress}/${expected}张" in JS
    assert "✓ 正式关键帧" in JS
    assert "AI已选优" in JS
    assert "已完成的历史候选立即显示（质检未通过也保留）" in JS
    assert "人工筛选（显示隐藏候选）" in JS
    assert "未选候选已隐藏" in JS
    assert "历史失败 · 系统自动接管" in JS
    assert "缺 ${missing" in JS
    assert "系统补齐后可选" in JS
    assert "Array.from({ length: expected }" in JS
    assert 'aria-label="候选 ${index} 尚未生成"' in JS
    assert "等待技术补齐" in JS
    assert "selection.selected_uri || selection.selected_url" in JS
    assert "Array.isArray(finalGroup.candidates)" in JS
    assert "item.candidate_progress" in JS
    assert "live_progress: true" in JS
    assert 'candidate.passed === false ? "质检未通过 · 系统将编辑返修"' in JS
    assert "Number(group.expected_count) === 3 ? 3 : 4" not in JS
    assert 'item.status === "technical_incomplete");' in JS


def test_failed_stage_guidance_is_visible_and_has_checkpoint_recovery_action():
    assert "真实失败阶段 · ${esc(guidance.failure.stage_label" in JS
    assert "已结束，不是仍在生成" in JS
    assert "data-guidance-recover" in JS
    assert "resumeProductionFromGuidance" in JS
    assert "从断点自动修复并继续" in JS
    assert "保留已完成资产，只重跑失败阶段及其下游" in JS


def test_candidate_round_progress_preserves_legacy_three_image_history():
    helper = JS[
        JS.index("function shotCandidateGroup"):
        JS.index("function planItemThumbs")
    ]
    result = _run_node(helper + r'''
      const item = {category: "shot_image", status: "regenerating_candidates",
        candidate_group: {
          expected_count: 3,
          generation_round: 4,
          max_candidate_rounds: 10,
          current_round_progress: {completed: 2, total: 4},
          candidates: [
            {candidate_index: 1, url: "/artifacts/a.png"},
            {candidate_index: 2, url: "/artifacts/b.png"},
          ],
        }};
      const state = shotCandidateState(item);
      console.log(JSON.stringify({
        expected: state.expected,
        missing: state.missing,
        generationRound: state.generationRound,
        maxCandidateRounds: state.maxCandidateRounds,
        progress: state.currentRoundProgress,
        label: state.roundLabel,
      }));
    ''')
    assert result == {
        "expected": 3,
        "missing": 1,
        "generationRound": 4,
        "maxCandidateRounds": 10,
        "progress": 2,
        "label": "第4/10轮 · 2/3张",
    }


def test_live_progress_is_used_before_a_complete_candidate_group_exists():
    helper = JS[
        JS.index("function shotCandidateGroup"):
        JS.index("function planItemThumbs")
    ]
    result = _run_node(helper + r'''
      const item = {category: "shot_image", status: "generating",
        candidate_group: {candidates: [], candidate_count: 0},
        candidate_progress: {
          candidate_set_id: "live", candidate_set_token: "token",
          candidate_revision: 2, generation_round: 3,
          max_candidate_rounds: 10, completed_count: 1,
          candidates: [{candidate_index: 1, url: "/artifacts/live.png",
            passed: false}],
        }};
      const state = shotCandidateState(item);
      console.log(JSON.stringify({
        live: state.liveProgress,
        count: state.candidates.length,
        failedVisible: state.candidates[0].passed === false,
        progress: state.currentRoundProgress,
      }));
    ''')
    assert result == {
        "live": True,
        "count": 1,
        "failedVisible": True,
        "progress": 1,
    }


def test_selected_shot_grid_hides_every_unselected_candidate():
    helper = JS[
        JS.index("function shotCandidateGroup"):
        JS.index("function planItemHtml")
    ]
    result = _run_node(r'''
      function esc(value) { return String(value); }
      function thumbUrl(value) { return value; }
      function qcIssueText(value) { return String(value || ""); }
      function shotBestEffortLabel() { return ""; }
    ''' + helper + r'''
      const token = "token";
      const candidates = [1, 2, 3, 4].map((index) => ({
        candidate_index: index, candidate_id: `${token}#${index}`,
        candidate_set_token: token, url: `/artifacts/${index}.png`,
        passed: index === 2,
      }));
      const item = {id: "shot:1", label: "镜头1", category: "shot_image",
        status: "done", candidate_group: {
          candidate_set_id: "set", candidate_set_token: token,
          candidate_revision: 1, candidates,
          selection: {candidate_set_token: token, candidate_index: 2,
            candidate_id: `${token}#2`, selected_url: "/artifacts/2.png",
            source: "ai"},
        }};
      const html = shotCandidateGridHtml(item, true);
      console.log(JSON.stringify({
        cards: (html.match(/<article class="shot-candidate/g) || []).length,
        selectedVisible: html.includes("/artifacts/2.png"),
        unselectedHidden: !html.includes("/artifacts/1.png")
          && !html.includes("/artifacts/3.png")
          && !html.includes("/artifacts/4.png"),
        selectedOnly: html.includes("selected-only"),
        review: html.includes("人工筛选（显示隐藏候选）"),
      }));
    ''')
    assert result == {
        "cards": 1,
        "selectedVisible": True,
        "unselectedHidden": True,
        "selectedOnly": True,
        "review": True,
    }


def test_best_effort_repair_is_rendered_as_nonblocking_ai_selection():
    helper = JS[
        JS.index("const STORYBOARD_KEYFRAME_TRANSIENT_STATUSES"):
        JS.index("const PLAN_REWORK_STATUSES")
    ]
    badge = JS[
        JS.index("function planQcBadge"):
        JS.index("function planQcIssuesHtml")
    ]
    result = _run_node(helper + badge + r'''
      const fromSelection = {
        status: "done", candidate_group: {
          selection: {best_effort_risk: true}
        }
      };
      const fromQc = {
        status: "reused", qc: {best_effort_promoted: true}
      };
      const fromGroup = {
        status: "done", candidate_group: {best_effort_promoted: true}
      };
      const unfinished = {
        status: "awaiting_selection", qc: {best_effort_promoted: true}
      };
      console.log(JSON.stringify({
        labels: [
          shotBestEffortLabel(fromSelection),
          shotBestEffortLabel(fromQc),
          shotBestEffortLabel(fromGroup),
          shotBestEffortLabel(unfinished),
        ],
        selectionBadgeWithoutQc: planQcBadge(fromSelection),
      }));
    ''')
    assert result["labels"] == [
        "AI已选相对最优（风险留档，非阻断）",
        "AI已选相对最优（风险留档，非阻断）",
        "AI已选相对最优（风险留档，非阻断）",
        "",
    ]
    assert "AI已选相对最优（风险留档，非阻断）" in \
        result["selectionBadgeWithoutQc"]
    assert "if (bestEffort)" in JS
    assert "if (row.bestEffort) return shotBestEffortLabel(row.item)" in JS


def test_tenth_round_best_effort_is_labeled_risk_but_continues():
    helper = JS[
        JS.index("const STORYBOARD_KEYFRAME_TRANSIENT_STATUSES"):
        JS.index("const PLAN_REWORK_STATUSES")
    ]
    result = _run_node(helper + r'''
      const item = {status: "done", candidate_group: {
        generation_round: 10, max_candidate_rounds: 10,
        selection: {best_effort_risk: true},
      }};
      console.log(JSON.stringify({label: shotBestEffortLabel(item)}));
    ''')
    assert result["label"] == (
        "已到10轮上限 · AI选相对最优（风险留档，继续生产）")


def test_episode29_visual_pass_issues_are_grouped_without_red_false_failure():
    helper = JS[
        JS.index("function qcIssueText"):
        JS.index("function shotBestEffortLabel")
    ]
    result = _run_node(r'''
      function esc(value) { return String(value); }
    ''' + helper + r'''
      const qc = {
        passed: false,
        visual_pass: true,
        image_passed: true,
        critical_failures: [],
        issues: [
          "画面本身符合两名女性、离场终点、现代酒店、电梯、门框框中框等要求",
          "参考图6仍是无法完整显示房门、走廊和远处电梯布局的旧场景基准图，与提示词明确要求替换该图不一致",
          "提示词概括称参考图4、5只锁人物身份，但对照表又允许它们继承服装、配饰和道具位置，用途边界表述不一致",
          "柳争流的红唇在侧面暖光下不够醒目，但不影响身份或剧情理解",
          "提示词较长，包含多段旧阶段排除项，可进一步压缩",
          "酒瓶与酒杯姿势成立，但可进一步强调双手分工",
        ],
        advisory_issues: [
          "柳争流的红唇在侧面暖光下不够醒目，但不影响身份或剧情理解",
          "提示词较长，包含多段旧阶段排除项，可进一步压缩",
          "酒瓶与酒杯姿势成立，但可进一步强调双手分工",
        ],
      };
      const groups = qcIssuePresentation(qc);
      console.log(JSON.stringify({groups, html: qcIssueSectionsHtml(qc)}));
    ''')
    groups = result["groups"]
    assert len(groups["facts"]) == 1
    assert len(groups["contractRisks"]) == 2
    assert len(groups["suggestions"]) == 3
    assert groups["visualFailures"] == []
    assert "画面通过事实" in result["html"]
    assert "合同或参考风险" in result["html"]
    assert "非阻断优化建议" in result["html"]
    assert "画面质检问题" not in result["html"]
    assert 'class="pc-issues"' not in result["html"]


def test_best_effort_positive_and_advisory_lines_are_never_red_failures():
    helper = JS[
        JS.index("function qcIssueText"):
        JS.index("function shotBestEffortLabel")
    ]
    result = _run_node(r'''
      function esc(value) { return String(value); }
    ''' + helper + r'''
      const qc = {
        passed: false,
        issues: [
          "画面人物身份和空间关系符合要求",
          "参考图职责存在不一致",
          "轮廓光可进一步加强",
        ],
        advisory_issues: ["轮廓光可进一步加强"],
      };
      const groups = qcIssuePresentation(qc, true);
      console.log(JSON.stringify({groups, html: qcIssueSectionsHtml(qc, true)}));
    ''')
    assert result["groups"]["facts"] == ["画面人物身份和空间关系符合要求"]
    assert result["groups"]["contractRisks"] == ["参考图职责存在不一致"]
    assert result["groups"]["suggestions"] == ["轮廓光可进一步加强"]
    assert result["groups"]["visualFailures"] == []
    assert "画面质检问题" not in result["html"]


def test_real_visual_failure_remains_a_red_qc_issue():
    helper = JS[
        JS.index("function qcIssueText"):
        JS.index("function shotBestEffortLabel")
    ]
    result = _run_node(r'''
      function esc(value) { return String(value); }
    ''' + helper + r'''
      const qc = {
        passed: false,
        visual_pass: false,
        issues: ["人物多出一人"],
        critical_failures: ["人物多出一人"],
      };
      console.log(JSON.stringify({
        groups: qcIssuePresentation(qc), html: qcIssueSectionsHtml(qc),
      }));
    ''')
    assert result["groups"]["visualFailures"] == ["人物多出一人"]
    assert "画面质检问题 1 条" in result["html"]
    assert 'class="pc-issues qc-visual-failures"' in result["html"]


def test_storyboard_masks_stale_failure_while_repairing_or_best_effort_done():
    helper = JS[
        JS.index("const STORYBOARD_KEYFRAME_TRANSIENT_STATUSES"):
        JS.index("const PLAN_REWORK_STATUSES")
    ]
    storyboard = JS[
        JS.index("function storyboardKeyframePlanItem"):
        JS.index("function storyboardKeyframeUrl")
    ]
    result = _run_node(helper + storyboard + r'''
      function probe(status, bestEffort) {
        return storyboardKeyframeFailure({
          render_plan: {items: [{
            category: "shot_image", shot_no: 2, status,
            candidate_group: {selection: {best_effort_risk: bestEffort}},
          }]},
          image_failures: [{shot_no: 2, issues: ["旧失败"]}],
        }, 2);
      }
      console.log(JSON.stringify({
        generating: probe("generating", false),
        regenerating: probe("regenerating_candidates", false),
        selecting: probe("awaiting_selection", false),
        pending: probe("pending", false),
        bestEffortDone: probe("done", true),
        realFailure: probe("failed", false),
      }));
    ''')
    assert result["generating"] is None
    assert result["regenerating"] is None
    assert result["selecting"] is None
    assert result["pending"] is None
    assert result["bestEffortDone"] is None
    assert result["realFailure"]["issues"] == ["旧失败"]
    assert "const failures = storyboardImageFailures(data);" in JS
    assert "const currentImageFailures = storyboardImageFailures(data);" in JS
    assert "const reportedImageFailures = storyboardImageFailures(data).length;" in JS


def test_cast_and_prop_candidates_do_not_require_mobile_selection():
    cast_view = JS[
        JS.index("function renderCastSelection"):
        JS.index("const VIDEO_REF_KIND_CN")
    ]
    for expected in (
        "AI自动选优，无需手机逐张定版",
        "人工改选只是可选覆盖",
        "✓ AI已选优",
        "AI选优中",
        "改选这张（可选）",
        "核心道具AI四选一",
        "改选这套（可选）",
        "AI选优后自动继续",
    ):
        assert expected in cast_view
    for obsolete in (
        "逐个点开大图对比，各选 1 张定版",
        "待选 1 张",
        "请选择1张",
        "全部选完才能继续",
        "手机端主选片入口",
    ):
        assert obsolete not in cast_view


def test_awaiting_cast_banner_says_ai_autoselects_without_phone_work():
    banner = JS[
        JS.index("function renderProgressBanner"):
        JS.index("const CARD_W")
    ]
    assert "人物/核心道具正在AI自动选优" in banner
    assert "无需手机逐张确认" in banner
    assert "查看选优结果（可选）" in banner
    assert "去选人物/道具" not in banner


def test_current_candidate_group_precedes_single_shot_artifact():
    candidates = JS.index(
        "const candidates = shotCandidates(item);"
    )
    artifact = JS.index(
        "else if ((art.images || {})[item.shot_no]) urls = "
        "[art.images[item.shot_no]];"
    )
    assert candidates < artifact
    assert "${canEdit && !candidateMode ?" in JS
    assert "✎ 修改提示词，编辑当前图并再生成1张" in JS


def test_selection_posts_complete_optimistic_lock_identity_and_refreshes_409():
    assert 'api("/api/shot-candidates/select"' in JS
    for field in (
        "candidate_set_id",
        "candidate_set_token",
        "contract_revision",
        "candidate_revision",
        "candidate_id",
        "candidate_index",
    ):
        assert field in JS
    assert 'source: "manual"' in JS
    assert "error.status === 409" in JS
    assert "refreshShotCandidatePlan(episodeId, onDone, true)" in JS


def test_regeneration_is_confirmed_single_edit_and_never_pauses_episode():
    assert 'armConfirm(button, "编辑返修1张"' in JS
    assert 'api("/api/shot-candidates/regenerate"' in JS
    assert "confirm_regenerate: true" in JS
    assert "其他镜头继续生产，不会暂停整集" in JS
    candidate_binding = JS[
        JS.index("function bindShotCandidateControls"):
        JS.index("function bindPlanRegen")
    ]
    assert "ensureBatchRevisionCheckpoint" not in candidate_binding


def test_candidate_editor_and_mutation_protect_live_overlay_from_dom_replacement():
    assert 'panel.dataset.shotCandidateMutation === "1"' in JS
    assert '.shot-candidate-regenerate-form:not([hidden])' in JS
    assert '.shot-candidate-regenerate[data-armed="1"]' in JS
    assert "shot-candidate-compare" in JS
    assert "scroll-snap-type: x mandatory" in CSS
    assert "flex: 0 0 min(68vw, 260px)" in CSS
