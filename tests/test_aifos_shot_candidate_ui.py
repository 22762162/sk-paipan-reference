from pathlib import Path


ROOT = Path(__file__).parents[1]
JS = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
    encoding="utf-8"
)
CSS = (ROOT / "aifos" / "web" / "static" / "style.css").read_text(
    encoding="utf-8"
)


def test_shot_candidate_statuses_and_four_up_rendering_are_present():
    for status in (
        "awaiting_selection",
        "regenerating_candidates",
        "technical_incomplete",
    ):
        assert status in JS
    assert "function shotCandidateGridHtml(item, editable)" in JS
    assert 'class="shot-candidate-grid"' in JS
    assert "本镜4张候选" in JS
    assert "✓ 正式关键帧" in JS
    assert "4张均保留回看" in JS
    assert "缺 ${missing" in JS
    assert "补齐4张后可选" in JS
    assert "const slots = [1, 2, 3, 4]" in JS
    assert 'aria-label="候选 ${index} 尚未生成"' in JS
    assert "等待技术补齐" in JS
    assert "selection.selected_uri || selection.selected_url" in JS


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
