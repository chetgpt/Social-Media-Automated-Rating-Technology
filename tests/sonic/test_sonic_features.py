from __future__ import annotations

import copy
import math
import wave
from pathlib import Path

import numpy as np
import pytest

from sonic_audit.features import (
    FEATURE_SCHEMA_VERSION,
    SIMILARITY_REPORT_SCHEMA_VERSION,
    STABILITY_REPORT_SCHEMA_VERSION,
    SonicFeatureConfig,
    SonicFeatureError,
    analyze_audio,
    build_similarity_report,
    evaluate_clustering_stability,
    recording_similarity,
    validate_feature_record,
)


TEST_CONFIG = SonicFeatureConfig(
    analysis_sample_rate=8_000,
    min_duration_seconds=0.5,
    max_duration_seconds=8.0,
    max_file_bytes=8 * 1024 * 1024,
    window_seconds=1.5,
    window_hop_seconds=0.75,
    max_windows=5,
    n_fft=512,
    hop_length=128,
    mel_bands=24,
    fingerprint_max_landmarks=1_500,
    chromaprint_mode="disabled",
)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 8_000) -> None:
    bounded = np.clip(np.asarray(samples), -1.0, 1.0)
    pcm = np.rint(bounded * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def _song(sample_rate: int, seconds: float, root: float, phase: float = 0.0) -> np.ndarray:
    time = np.arange(int(sample_rate * seconds), dtype=np.float64) / sample_rate
    pulse = 0.60 + 0.40 * (np.sin(2 * np.pi * 2.0 * time) > 0)
    audio = pulse * (
        0.42 * np.sin(2 * np.pi * root * time + phase)
        + 0.28 * np.sin(2 * np.pi * root * 1.5 * time + phase / 2)
        + 0.18 * np.sin(2 * np.pi * root * 2.0 * time)
    )
    return audio.astype(np.float32)


def test_analyze_audio_is_deterministic_path_free_and_hash_verified(tmp_path: Path) -> None:
    audio_path = tmp_path / "decoded.wav"
    _write_wav(audio_path, _song(8_000, 3.2, 220.0))

    first = analyze_audio(audio_path, post_id="post-1", config=TEST_CONFIG)
    second = analyze_audio(audio_path, post_id="post-1", config=TEST_CONFIG)

    assert first == second
    assert first["schema_version"] == FEATURE_SCHEMA_VERSION
    assert first["window_count"] == len(first["windows"])
    assert first["quality"]["status"] == "usable"
    assert first["fingerprints"]["chromaprint"] == {
        "status": "disabled",
        "algorithm": "chromaprint",
    }
    assert first["fingerprints"]["spectral_landmark"]["landmark_count"] > 20
    assert first["fingerprints"]["spectral_landmark"]["status"] == "available"
    assert first["aggregate"]["track_embedding"]["dimensions"] > 40
    assert math.isclose(
        np.linalg.norm(first["aggregate"]["track_embedding"]["vector"]),
        1.0,
        rel_tol=1e-5,
    )
    serialized = str(first).lower()
    assert str(audio_path).lower() not in serialized
    assert "waveform" not in serialized
    validate_feature_record(first)

    tampered = copy.deepcopy(first)
    tampered["quality"]["rms_dbfs"] = -1.0
    with pytest.raises(SonicFeatureError, match="record_hash_mismatch"):
        validate_feature_record(tampered)


def test_audio_validation_rejects_stereo_and_invalid_sha(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    samples = _song(8_000, 1.0, 220.0)
    pcm = np.column_stack([samples, samples])
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(8_000)
        handle.writeframes(np.rint(pcm * 32767).astype("<i2").tobytes())

    with pytest.raises(SonicFeatureError, match="audio_not_mono"):
        analyze_audio(path, post_id="post-stereo", config=TEST_CONFIG)

    mono = tmp_path / "mono.wav"
    _write_wav(mono, samples)
    with pytest.raises(SonicFeatureError, match="invalid_hash"):
        analyze_audio(mono, post_id="post-mono", audio_sha256="nope", config=TEST_CONFIG)


def test_recording_retrieval_prefers_same_recording_with_overlay(tmp_path: Path) -> None:
    generator = np.random.default_rng(5)
    song_a = _song(8_000, 3.5, 220.0)
    song_b = _song(8_000, 3.5, 311.0, phase=0.7)
    voice_like = signal = 0.09 * np.sin(
        2 * np.pi * (95.0 + 35.0 * np.sin(2 * np.pi * 1.7 * np.arange(song_a.size) / 8_000))
        * np.arange(song_a.size)
        / 8_000
    )
    a_overlay = np.clip(0.88 * song_a + voice_like + generator.normal(0, 0.006, song_a.size), -1, 1)
    b_overlay = np.clip(0.90 * song_b + generator.normal(0, 0.006, song_b.size), -1, 1)

    records = []
    for post_id, samples, label in [
        ("a-1", song_a, "track-a"),
        ("a-2", a_overlay, "track-a"),
        ("b-1", song_b, "track-b"),
        ("b-2", b_overlay, "track-b"),
    ]:
        path = tmp_path / f"{post_id}.wav"
        _write_wav(path, samples)
        record = analyze_audio(path, post_id=post_id, config=TEST_CONFIG)
        record["reference_label"] = label
        records.append(record)

    same = recording_similarity(records[0], records[1])
    different = recording_similarity(records[0], records[2])
    assert same["recording_score"] > different["recording_score"]
    assert same["spectral_landmark_score"] > different["spectral_landmark_score"]

    report = build_similarity_report(
        records,
        {record["post_id"]: record["reference_label"] for record in records},
        threshold=(same["recording_score"] + different["recording_score"]) / 2,
    )
    assert report["schema_version"] == SIMILARITY_REPORT_SCHEMA_VERSION
    assert report["evaluable_query_count"] == 4
    assert report["recall_at_1"] == 1.0
    assert report["recall_at_5"] == 1.0
    assert report["pair_metrics"]["false_match_rate_at_threshold"] == 0.0


def _style_record(post_id: str, center: np.ndarray, offset: float) -> dict:
    windows = []
    for index in range(5):
        perturbation = np.array([offset * (index - 2), -offset * (index - 2), 0.0, 0.0])
        vector = center + perturbation
        vector = vector / np.linalg.norm(vector)
        windows.append({"style_embedding": {"vector": vector.tolist()}})
    return {"post_id": post_id, "windows": windows}


def test_style_cluster_stability_is_deterministic_for_separated_groups() -> None:
    records = [
        _style_record("a-1", np.array([1.0, 0.0, 0.0, 0.0]), 0.01),
        _style_record("a-2", np.array([0.98, 0.04, 0.0, 0.0]), 0.01),
        _style_record("a-3", np.array([0.96, -0.05, 0.0, 0.0]), 0.01),
        _style_record("b-1", np.array([0.0, 1.0, 0.0, 0.0]), 0.01),
        _style_record("b-2", np.array([0.04, 0.98, 0.0, 0.0]), 0.01),
        _style_record("b-3", np.array([-0.05, 0.96, 0.0, 0.0]), 0.01),
    ]

    first = evaluate_clustering_stability(records, n_clusters=2, repeats=6, seed=9)
    second = evaluate_clustering_stability(records, n_clusters=2, repeats=6, seed=9)

    assert first == second
    assert first["schema_version"] == STABILITY_REPORT_SCHEMA_VERSION
    assert first["status"] == "evaluated"
    assert first["mean_adjusted_rand_index"] == 1.0
    assert first["minimum_adjusted_rand_index"] == 1.0
    assert first["pairwise_comparison_count"] == 15

    raw_vectors = [
        [1.0, 0.01, 0.0],
        [0.98, -0.02, 0.0],
        [0.96, 0.03, 0.0],
        [0.01, 1.0, 0.0],
        [-0.02, 0.98, 0.0],
        [0.03, 0.96, 0.0],
    ]
    vector_report = evaluate_clustering_stability(raw_vectors, n_clusters=2, repeats=4, seed=2)
    assert vector_report["resampling_basis"] == "aggregate_vectors"
    assert vector_report["status"] == "evaluated"
    assert vector_report["mean_adjusted_rand_index"] == 1.0


def test_similarity_report_abstains_when_reference_support_is_insufficient() -> None:
    report = build_similarity_report([], {})

    assert report["schema_version"] == SIMILARITY_REPORT_SCHEMA_VERSION
    assert report["status"] == "not_evaluable"
    assert report["reason"] == "insufficient_records"
    assert report["recall_at_1"] is None
    assert report["pair_metrics"]["precision_at_threshold"] is None
    assert len(report["report_hash"]) == 64


def test_silence_and_flat_noise_cannot_be_recording_matches(tmp_path: Path) -> None:
    silence_short_path = tmp_path / "silence-short.wav"
    silence_long_path = tmp_path / "silence-long.wav"
    noise_a_path = tmp_path / "noise-a.wav"
    noise_b_path = tmp_path / "noise-b.wav"
    _write_wav(silence_short_path, np.zeros(8_000 * 3, dtype=np.float32))
    _write_wav(silence_long_path, np.zeros(8_000 * 3 + 4_000, dtype=np.float32))
    generator = np.random.default_rng(81)
    _write_wav(noise_a_path, generator.normal(0.0, 0.08, 8_000 * 3).astype(np.float32))
    _write_wav(noise_b_path, generator.normal(0.0, 0.08, 8_000 * 3).astype(np.float32))

    silence_short = analyze_audio(silence_short_path, post_id="silent-a", config=TEST_CONFIG)
    silence_long = analyze_audio(silence_long_path, post_id="silent-b", config=TEST_CONFIG)
    noise_a = analyze_audio(noise_a_path, post_id="noise-a", config=TEST_CONFIG)
    noise_b = analyze_audio(noise_b_path, post_id="noise-b", config=TEST_CONFIG)

    for record in (silence_short, silence_long):
        assert record["quality"]["status"] == "silent"
        assert record["fingerprints"]["spectral_landmark"]["status"] == "unavailable"
        assert record["fingerprints"]["spectral_landmark"]["reason"] == "insufficient_signal"
        assert record["fingerprints"]["spectral_landmark"]["landmarks"] == []

    silent_pair = recording_similarity(silence_short, silence_long)
    silent_noise_pair = recording_similarity(silence_short, noise_a)
    noise_pair = recording_similarity(noise_a, noise_b)
    assert silent_pair["comparable"] is False
    assert silent_pair["recording_score"] == 0.0
    assert "quality_silent" in silent_pair["reason"]
    assert silent_noise_pair["comparable"] is False
    assert silent_noise_pair["recording_score"] == 0.0
    assert noise_a["fingerprints"]["spectral_landmark"]["status"] == "unavailable"
    assert noise_b["fingerprints"]["spectral_landmark"]["status"] == "unavailable"
    assert noise_pair["comparable"] is False
    assert noise_pair["reason"] == "insufficient_fingerprint_evidence"
    assert noise_pair["recording_score"] == 0.0

    report = build_similarity_report(
        [silence_short, silence_long, noise_a],
        {"silent-a": "silence", "silent-b": "silence", "noise-a": "noise"},
    )
    assert report["status"] == "not_evaluable"
    assert report["reason"] == "insufficient_usable_reference_labels"
    assert report["quality_excluded_record_count"] == 2
    assert report["quality_exclusion_counts"] == {"quality_silent": 2}


def test_identical_style_vectors_are_degenerate_not_stable() -> None:
    report = evaluate_clustering_stability(
        [[1.0, 0.0, 0.0]] * 6,
        n_clusters=3,
        repeats=4,
        seed=3,
    )

    assert report["status"] == "not_evaluable"
    assert report["reason"] == "degenerate_style_vectors"
    assert report["minimum_effective_distinct_vectors"] == 1
    assert report["maximum_effective_distinct_vectors"] == 1
    assert report["occupied_cluster_counts"] == [1, 1, 1, 1]
    assert report["mean_adjusted_rand_index"] is None
    assert report["minimum_adjusted_rand_index"] is None
    assert report["maximum_adjusted_rand_index"] is None
