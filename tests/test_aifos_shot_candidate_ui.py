from pathlib import Path


ROOT = Path(__file__).parents[1]
JS = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
    encoding="utf-8"
)
CSS = (ROOT / "aifos" / "web" / "static" / "style.css").read_text(
    encoding="utf-8"
)


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
