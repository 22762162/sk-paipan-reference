from aifos.video_temporal_qc import analyze_temporal_samples


def test_temporal_evidence_flags_freeze_and_global_jump_without_blocking():
    samples = [
        {"label": "0%", "timestamp": 0, "uri": "a"},
        {"label": "25%", "timestamp": 1, "uri": "b"},
        {"label": "50%", "timestamp": 2, "uri": "c"},
    ]
    pixels = {
        "a": bytes([20] * 1024),
        "b": bytes([20] * 1024),
        "c": bytes([240] * 1024),
    }

    report = analyze_temporal_samples(
        samples, frame_reader=lambda uri: pixels[uri])

    assert report["analysis_available"] is True
    assert report["blocking"] is False
    assert report["advisory_only"] is True
    assert [row["classification"] for row in report["transitions"]] == [
        "near_duplicate", "abrupt_global_change"]
    assert {row["code"] for row in report["warnings"]} == {
        "near_duplicate_frames", "abrupt_global_change"}


def test_temporal_evidence_records_unavailable_samples():
    report = analyze_temporal_samples(
        [{"label": "0%", "uri": "missing"}],
        frame_reader=lambda _uri: (_ for _ in ()).throw(
            FileNotFoundError("missing")))

    assert report["analysis_available"] is False
    assert report["usable_sample_count"] == 0
    assert report["unavailable"][0]["label"] == "0%"
    assert report["blocking"] is False


def test_temporal_evidence_marks_near_blank_frame_as_warning_only():
    report = analyze_temporal_samples(
        [
            {"label": "0%", "uri": "black"},
            {"label": "100%", "uri": "gray"},
        ],
        frame_reader=lambda uri: (
            bytes([0] * 1024) if uri == "black" else bytes([128] * 1024)))

    assert any(row["code"] == "near_blank_frame"
               for row in report["warnings"])
    assert report["reference_chain_eligible"] is False
