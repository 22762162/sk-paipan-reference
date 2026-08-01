"""创作选片策略不得被内容质检重新变成生产闸门。"""

from dataclasses import FrozenInstanceError

import pytest

from aifos.selection_mode import (
    CANDIDATES_PER_SHOT,
    CandidateResultVersion,
    FailureClass,
    build_candidate_result_versions,
    build_candidate_set_version,
    build_selection_policy,
    downstream_ready,
    evaluate_candidate_promotion,
    is_stale_candidate_result,
    selection_policy_from_config,
    should_retry_failure,
)


def _version(**updates):
    facts = {
        "episode_id": "p015/e001",
        "shot_no": 5,
        "contract_revision": 2,
        "candidate_revision": 3,
        "prompt": {"subject": "主角查看书页", "style": "本剧锁定风格"},
        "reference_manifest": {
            "identity": ["character.png"],
            "geometry": ["scene-top.png"],
        },
    }
    facts.update(updates)
    return build_candidate_set_version(**facts)


def test_selection_mode_closes_content_qc_but_never_preflight_or_integrity():
    policy = build_selection_policy(True)

    assert policy.selection_mode_enabled is True
    assert policy.image_content_qc_enabled is False
    assert policy.video_content_qc_enabled is False
    assert policy.content_qc_blocking is False
    assert policy.content_qc_auto_retry is False
    assert policy.prompt_review_enabled is True
    assert policy.director_contract_review_enabled is True
    assert policy.technical_integrity_checks_enabled is True
    assert policy.candidates_per_shot == CANDIDATES_PER_SHOT == 4
    assert policy.downstream_requires_selection is True


def test_content_qc_remains_observational_when_selection_mode_is_off():
    policy = build_selection_policy(
        False,
        image_content_qc_requested=True,
        video_content_qc_requested=False,
    )

    assert policy.image_content_qc_enabled is True
    assert policy.video_content_qc_enabled is False
    assert policy.content_qc_blocking is False
    assert policy.content_qc_auto_retry is False


def test_final_settings_keys_feed_the_policy_and_selection_mode_wins():
    policy = selection_policy_from_config({
        "defaults": {
            "selection_mode": True,
            "image_content_qc": True,
            "video_content_qc": True,
            "shot_candidate_count": 4,
        },
    })

    assert policy.selection_mode_enabled is True
    assert policy.image_content_qc_enabled is False
    assert policy.video_content_qc_enabled is False
    assert policy.candidates_per_shot == 4


def test_legacy_image_qc_is_only_used_when_new_key_is_absent():
    legacy = selection_policy_from_config({
        "defaults": {"image_qc": False},
    })
    new_key_wins = selection_policy_from_config({
        "defaults": {"image_qc": False, "image_content_qc": True},
    })

    assert legacy.image_content_qc_enabled is False
    assert new_key_wins.image_content_qc_enabled is True


def test_candidate_count_setting_rejects_any_value_other_than_four():
    with pytest.raises(ValueError, match="只允许固定为4"):
        selection_policy_from_config({
            "defaults": {"shot_candidate_count": 3},
        })


@pytest.mark.parametrize("failure", [
    FailureClass.NETWORK,
    FailureClass.API,
    FailureClass.TIMEOUT,
    FailureClass.RATE_LIMIT,
    FailureClass.TECHNICAL_INTEGRITY,
])
def test_only_technical_failures_retry_while_attempts_remain(failure):
    assert should_retry_failure(failure, attempts_remaining=1) is True
    assert should_retry_failure(failure, attempts_remaining=0) is False


def test_content_or_unknown_failure_never_triggers_automatic_retry():
    assert should_retry_failure(
        FailureClass.CONTENT, attempts_remaining=2) is False
    assert should_retry_failure("bad_composition", attempts_remaining=2) is False


def test_candidate_version_is_deterministic_and_ignores_mapping_order():
    first = _version(reference_manifest={
        "identity": ["character.png"],
        "geometry": ["scene-top.png"],
    })
    second = _version(reference_manifest={
        "geometry": ["scene-top.png"],
        "identity": ["character.png"],
    })

    assert first == second
    assert first.token.startswith("cset-v1:")


@pytest.mark.parametrize("change", [
    {"contract_revision": 3},
    {"candidate_revision": 4},
    {"prompt": {"subject": "主角合上书页"}},
    {"reference_manifest": {"identity": ["new-character.png"]}},
])
def test_any_generation_fact_change_creates_a_new_candidate_version(change):
    assert _version(**change).token != _version().token


def test_exactly_four_parallel_candidate_credentials_share_one_version():
    version = _version()
    results = build_candidate_result_versions(version)

    assert len(results) == 4
    assert tuple(result.candidate_index for result in results) == (1, 2, 3, 4)
    assert {result.candidate_set_token for result in results} == {version.token}


def test_manual_or_ai_selection_of_current_version_unlocks_downstream():
    version = _version()
    candidate = build_candidate_result_versions(version)[2]

    assert downstream_ready(candidate, version, selection_source="manual") is True
    assert downstream_ready(candidate, version, selection_source="ai") is True
    assert downstream_ready(None, version, selection_source="manual") is False
    assert downstream_ready(candidate, version) is False


def test_stale_result_can_never_overwrite_current_formal_asset():
    old_version = _version(candidate_revision=2)
    current_version = _version(candidate_revision=3)
    late_result = build_candidate_result_versions(old_version)[0]

    assert is_stale_candidate_result(late_result, current_version) is True
    decision = evaluate_candidate_promotion(
        late_result, current_version, selection_source="ai")
    assert decision.allowed is False
    assert decision.stale is True
    assert "禁止覆盖" in decision.reason
    assert downstream_ready(
        late_result, current_version, selection_source="ai") is False


def test_invalid_candidate_index_or_missing_selection_cannot_promote():
    version = _version()
    invalid = CandidateResultVersion(version.token, candidate_index=5)

    assert evaluate_candidate_promotion(
        invalid, version, selection_source="manual").allowed is False
    valid = build_candidate_result_versions(version)[0]
    assert evaluate_candidate_promotion(
        valid, version, selection_source="").allowed is False


def test_policy_and_version_objects_are_immutable():
    policy = build_selection_policy(True)
    version = _version()

    with pytest.raises(FrozenInstanceError):
        policy.candidates_per_shot = 3
    with pytest.raises(FrozenInstanceError):
        version.token = "cset-v1:changed"


@pytest.mark.parametrize(("field", "value"), [
    ("episode_id", ""),
    ("shot_no", 0),
    ("contract_revision", "1.5"),
    ("candidate_revision", True),
])
def test_version_rejects_ambiguous_or_invalid_identity_fields(field, value):
    with pytest.raises(ValueError):
        _version(**{field: value})
