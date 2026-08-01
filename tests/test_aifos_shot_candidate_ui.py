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
    assert 'class="shot-candidate-grid"' in JS
    assert 'const batchLabel = expected === 3 ? "问题镜头补抽3张"' in JS
    assert "✓ 正式关键帧" in JS
    assert "AI已自动选优" in JS
    assert "无需手机操作" in JS
    assert "改选这张（可选）" in JS
    assert "历史失败 · 系统自动接管" in JS
    assert "缺 ${missing" in JS
    assert "系统补齐后可改选" in JS
    assert "Array.from({ length: expected }" in JS
    assert 'aria-label="候选 ${index} 尚未生成"' in JS
    assert "等待技术补齐" in JS
    assert "selection.selected_uri || selection.selected_url" in JS
    assert "Array.isArray(group.candidates)" in JS
    assert "Number(group.expected_count) === 3 ? 3 : 4" in JS
    assert 'item.status === "technical_incomplete";' in JS


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
        "已补抽3张并AI选优（非阻断风险）",
        "已补抽3张并AI选优（非阻断风险）",
        "已补抽3张并AI选优（非阻断风险）",
        "",
    ]
    assert "已补抽3张并AI选优（非阻断风险）" in \
        result["selectionBadgeWithoutQc"]
    assert "if (bestEffort)" in JS
    assert 'if (row.bestEffort) return "已补抽3张并AI选优（非阻断风险）"' in JS


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
        "const candidates = shotCandidates(item).map(shotCandidateUrl);"
    )
    artifact = JS.index(
        "else if ((art.images || {})[item.shot_no]) urls = "
        "[art.images[item.shot_no]];"
    )
    assert candidates < artifact
    assert "${canEdit && !candidateMode ?" in JS
    assert "✎ 修改提示词，整组换4张" in JS


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


def test_regeneration_is_confirmed_whole_group_and_never_pauses_episode():
    assert 'armConfirm(button, "整组换4张"' in JS
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
