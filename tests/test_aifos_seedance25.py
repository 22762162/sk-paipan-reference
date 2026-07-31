"""Seedance 2.5 is opt-in and must be proven by the installed CLI."""

import copy
import stat
from unittest import mock

import pytest

from aifos.director import Director
from aifos.config import Config, DEFAULTS
from aifos.errors import ProviderError
from aifos.production import dreamina as dreamina_module
from aifos.production.dreamina import DreaminaProvider
from aifos.standard_center import DEFAULT_STANDARD
from aifos.workflow import production_profile
from aifos.seedance_policy import (
    SeedancePolicyError,
    probe_cli_help,
    parse_cli_help,
    validate_runtime_request,
)


class _ReachedCli(RuntimeError):
    """The policy gates passed and the provider reached its paid CLI call."""


UPGRADE_POLICY = {
    "candidate_capability_key": "seedance2_5",
    "reported_limits": {
        "max_duration_seconds": 30,
        # Keep the exact standard/UI schema name here.  Runtime reporting may
        # continue to call these material inputs.
        "max_material_assets": 40,
        "max_total_references": 50,
    },
    "allowed_reasons": [
        "indivisible_continuous_take_16_to_30_seconds",
        "required_references_exceed_seedance2_0_limit",
        "reference_video_required",
        "complex_continuous_action",
    ],
}


def _upgrade_payload(**overrides):
    payload = {
        "first": "/tmp/first.png",
        "last": "/tmp/last.png",
        "duration": 30,
        "video_model_tier": "seedance2_5",
        "video_model_reason": "complex_continuous_action",
        "model_upgrade_policy": copy.deepcopy(UPGRADE_POLICY),
    }
    payload.update(overrides)
    return payload


def _upgrade_conf(alias="real-seedance-2.5-vip"):
    return {"model_aliases": {"seedance2_5": alias}}


def _proved_capability():
    return {
        "supported": True,
        "alias_supported": True,
        "max_duration_seconds": 30,
        "max_material_inputs": 40,
        "max_total_references": 50,
    }


def test_production_profile_archives_optional_upgrade_policy():
    profile = production_profile(Config(copy.deepcopy(DEFAULTS)), {
        "content": copy.deepcopy(DEFAULT_STANDARD),
        "version": 1,
        "version_id": 1,
        "fingerprint": "test-standard",
    })
    assert profile["video_model"] == "seedance2.0fast_vip"
    assert profile["resolution"] == "720p"
    assert profile["model_upgrade_policy"][
        "candidate_capability_key"] == "seedance2_5"
    assert profile["model_upgrade_policy"]["reported_limits"] == {
        "max_material_assets": 40,
        "max_total_references": 50,
        "max_duration_seconds": 30,
    }


def test_seedance20_contract_is_fast_vip_and_never_clips_or_drops():
    payload = {
        "first": "first.png",
        "last": "last.png",
        "duration": 15,
        "reference_images": [f"asset-{index}.png" for index in range(7)],
    }
    runtime = validate_runtime_request(payload, {})
    assert runtime["tier"] == "seedance2_0"
    assert runtime["model_alias"] == "seedance2.0fast_vip"
    assert runtime["submitted_duration"] == 15
    assert runtime["material_reference_count"] == 7
    assert runtime["total_reference_count"] == 9

    with pytest.raises(SeedancePolicyError, match="禁止静默丢弃"):
        validate_runtime_request({
            **payload,
            "reference_images": [
                f"asset-{index}.png" for index in range(8)],
        }, {})
    with pytest.raises(SeedancePolicyError, match="禁止静默截短"):
        validate_runtime_request({**payload, "duration": 15.1}, {})


def test_seedance20_payload_model_wins_over_drifted_provider_config(tmp_path):
    captured = {}

    def _capture(_tag, cmd, _cwd, _timeout, cancel=None):
        captured["cmd"] = cmd
        raise _ReachedCli()

    provider = DreaminaProvider("jimeng", {
        "enabled": True,
        "capabilities": ["video"],
        "model_version": "seedance2.0mini",
    })
    payload = {
        "shot_no": 1,
        "first": "/tmp/first.png",
        "last": "/tmp/last.png",
        "duration": 8,
        "prompt": "single action",
        "video_model_tier": "seedance2_0",
        "model_version": "seedance2.0fast_vip",
        "video_resolution": "720p",
    }
    with mock.patch.object(
            dreamina_module, "run_interruptible", side_effect=_capture):
        with pytest.raises(_ReachedCli):
            provider.generate("video", payload, tmp_path)
    assert "--model_version=seedance2.0fast_vip" in captured["cmd"]
    assert "--model_version=seedance2.0mini" not in captured["cmd"]


def test_seedance20_rejects_an_unknown_or_disguised_model():
    with pytest.raises(SeedancePolicyError, match="不是已知"):
        validate_runtime_request({
            "first": "first.png",
            "last": "last.png",
            "duration": 8,
            "video_model_tier": "seedance2_0",
            "model_version": "seedance2.5-unproved",
        }, {})


def test_seedance20_allows_half_second_rounding_to_cli_minimum():
    runtime = validate_runtime_request({
        "first": "first.png",
        "last": "last.png",
        "duration": 3.5,
    }, {})
    assert runtime["requested_duration"] == 3.5
    assert runtime["submitted_duration"] == 4
    with pytest.raises(SeedancePolicyError, match="不静默拉长"):
        validate_runtime_request({
            "first": "first.png",
            "last": "last.png",
            "duration": 3.0,
        }, {})


def test_seedance25_requires_a_real_configured_model_alias():
    with pytest.raises(SeedancePolicyError, match="未配置真实 CLI model alias"):
        validate_runtime_request(
            _upgrade_payload(), {}, help_probe=_proved_capability())


@pytest.mark.parametrize("reason", [None, "", "save_money", "auto"])
def test_seedance25_requires_an_allowed_per_shot_reason(reason):
    payload = _upgrade_payload(video_model_reason=reason)
    with pytest.raises(SeedancePolicyError, match="升级|按需"):
        validate_runtime_request(
            payload, _upgrade_conf(), help_probe=_proved_capability())


def test_seedance25_accepts_model_upgrade_reason_alias():
    payload = _upgrade_payload(
        video_model_reason=None,
        model_upgrade_reason="reference_video_required")
    runtime = validate_runtime_request(
        payload, _upgrade_conf(), help_probe=_proved_capability())
    assert runtime["upgrade_reason"] == "reference_video_required"


def test_seedance25_blocks_when_current_cli_help_is_unavailable():
    with pytest.raises(SeedancePolicyError, match="当前 CLI 不支持") as exc:
        validate_runtime_request(
            _upgrade_payload(), _upgrade_conf(),
            command=["/definitely/missing/dreamina"])
    assert "提交前阻断" in str(exc.value)


def test_seedance25_blocks_when_help_does_not_prove_all_limits():
    probe = {
        "supported": False,
        "reason": "max_total_references<50",
    }
    with pytest.raises(SeedancePolicyError, match="max_total_references<50"):
        validate_runtime_request(
            _upgrade_payload(), _upgrade_conf(), help_probe=probe)


def test_cli_help_requires_the_exact_configured_alias_token():
    parsed = parse_cli_help(
        "models: prefix-real-seedance-2.5-vip-suffix\n"
        "max-duration=30\nmax-material-inputs=40\n"
        "max-total-references=50",
        "real-seedance-2.5-vip")
    assert parsed["alias_supported"] is False


def test_cli_help_parser_reads_ranges_and_explicit_input_caps():
    parsed = parse_cli_help(
        "models: seedance2.5fast_vip\n"
        "duration: supported range: 4-30\n"
        "input limits: materials<=40, total-references<=50\n",
        "seedance2.5fast_vip")
    assert parsed == {
        "model_alias": "seedance2.5fast_vip",
        "alias_supported": True,
        "max_duration_seconds": 30,
        "max_material_inputs": 40,
        "max_total_references": 50,
    }


def test_seedance25_passes_only_after_actual_cli_help_probe(tmp_path):
    binary = tmp_path / "dreamina"
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"multimodal2video\" ] && "
        "[ \"$2\" = \"--help\" ]; then\n"
        "  echo 'models: real-seedance-2.5-vip'\n"
        "  echo 'max-duration=30'\n"
        "  echo 'max-material-inputs=40'\n"
        "  echo 'max-total-references=50'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    direct_probe = probe_cli_help(
        [str(binary)], "real-seedance-2.5-vip")
    assert direct_probe["supported"] is True

    runtime = validate_runtime_request(
        _upgrade_payload(reference_images=[
            f"asset-{index}.png" for index in range(40)]),
        _upgrade_conf(), command=[str(binary)])
    assert runtime["model_alias"] == "real-seedance-2.5-vip"
    assert runtime["requested_duration"] == 30
    assert runtime["material_reference_count"] == 40
    assert runtime["total_reference_count"] == 42
    assert runtime["cli_probe"]["supported"] is True


def test_provider_stops_before_generation_when_seedance25_is_unproved(
        tmp_path):
    provider = DreaminaProvider("jimeng", {
        "enabled": True,
        "capabilities": ["video"],
        **_upgrade_conf(),
    })
    with mock.patch.object(
            provider, "_upgrade_help_probe", return_value={
                "supported": False,
                "reason": "installed help has no seedance 2.5 alias",
            }), mock.patch.object(
                dreamina_module, "run_interruptible") as run:
        with pytest.raises(ProviderError, match="提交前阻断"):
            provider.generate(
                "video", _upgrade_payload(shot_no=1, prompt="single action"),
                tmp_path)
    run.assert_not_called()


def test_snapshot_signature_covers_tier_reason_model_duration_and_refs():
    base = {
        "shot_no": 1,
        "prompt_compact": "single action",
        "first": "first.png",
        "last": "last.png",
        "reference_images": ["character.png"],
        "duration": 8,
        "video_model_tier": "seedance2_0",
        "video_model_reason": "",
        "model_version": "seedance2.0fast_vip",
        "video_resolution": "720p",
    }
    snapshot = Director._video_input_snapshot(base)
    assert snapshot["material_reference_count_requested"] == 1
    assert snapshot["total_reference_count_requested"] == 3

    variants = [
        {**base, "duration": 9},
        {**base, "reference_images": ["character.png", "room.png"]},
        {**base, "model_version": "different-real-alias"},
        {
            **base,
            "video_model_tier": "seedance2_5",
            "video_model_reason": "complex_continuous_action",
            "model_version": "",
        },
        {**base, "video_model_reason": "reference_video_required"},
    ]
    for variant in variants:
        assert Director._video_input_snapshot(variant)[
            "input_signature"] != snapshot["input_signature"]
