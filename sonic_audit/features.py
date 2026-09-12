"""Deterministic, CPU-local sonic features for the isolated SONIC AUDIT mode.

The module consumes a decoded mono audio file and returns only bounded derived
data.  It never writes audio, embeds a source path, downloads model weights, or
uses a network service.  The embeddings are deliberately classical signal
descriptors rather than learned semantic representations.  They are useful for
pilot clustering and retrieval, but must not be presented as an acoustic track
identity on their own.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import fft as scipy_fft
from scipy import ndimage, signal


FEATURE_SCHEMA_VERSION = "sonic-feature-v1"
SIMILARITY_REPORT_SCHEMA_VERSION = "sonic-similarity-report-v1"
STABILITY_REPORT_SCHEMA_VERSION = "sonic-cluster-stability-v1"
_ALGORITHM_VERSION = "classical-sonic-cpu-v2"
_HEX_64 = frozenset("0123456789abcdef")


class SonicFeatureError(ValueError):
    """A safe, enumerated failure raised before a feature record is emitted."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SonicFeatureConfig:
    """Bounds and DSP parameters frozen into every feature hash."""

    analysis_sample_rate: int = 16_000
    min_duration_seconds: float = 1.0
    max_duration_seconds: float = 180.0
    max_file_bytes: int = 256 * 1024 * 1024
    window_seconds: float = 8.0
    window_hop_seconds: float = 4.0
    max_windows: int = 16
    n_fft: int = 1024
    hop_length: int = 256
    mel_bands: int = 32
    fingerprint_max_landmarks: int = 8_000
    chromaprint_mode: str = "auto"
    chromaprint_timeout_seconds: float = 20.0

    def validate(self) -> None:
        if not 8_000 <= self.analysis_sample_rate <= 48_000:
            raise SonicFeatureError("invalid_config", "analysis sample rate is outside bounds")
        if not 0.25 <= self.min_duration_seconds <= self.max_duration_seconds:
            raise SonicFeatureError("invalid_config", "duration bounds are inconsistent")
        if self.max_duration_seconds > 600:
            raise SonicFeatureError("invalid_config", "maximum duration exceeds the safety bound")
        if not 1_024 <= self.max_file_bytes <= 1024 * 1024 * 1024:
            raise SonicFeatureError("invalid_config", "file-size bound is outside safety limits")
        if not 0.5 <= self.window_seconds <= self.max_duration_seconds:
            raise SonicFeatureError("invalid_config", "window duration is outside bounds")
        if not 0.1 <= self.window_hop_seconds <= self.window_seconds:
            raise SonicFeatureError("invalid_config", "window hop is outside bounds")
        if not 1 <= self.max_windows <= 64:
            raise SonicFeatureError("invalid_config", "window count is outside bounds")
        if self.n_fft < 256 or self.n_fft > 8192 or self.n_fft & (self.n_fft - 1):
            raise SonicFeatureError("invalid_config", "n_fft must be a bounded power of two")
        if not 32 <= self.hop_length <= self.n_fft:
            raise SonicFeatureError("invalid_config", "hop length is outside bounds")
        if not 12 <= self.mel_bands <= 128:
            raise SonicFeatureError("invalid_config", "mel band count is outside bounds")
        if not 100 <= self.fingerprint_max_landmarks <= 50_000:
            raise SonicFeatureError("invalid_config", "fingerprint bound is outside limits")
        if self.chromaprint_mode not in {"auto", "disabled"}:
            raise SonicFeatureError("invalid_config", "chromaprint_mode must be auto or disabled")
        if not 1 <= self.chromaprint_timeout_seconds <= 120:
            raise SonicFeatureError("invalid_config", "Chromaprint timeout is outside bounds")


def _round_float(value: float, digits: int = 7) -> float:
    if not math.isfinite(float(value)):
        raise SonicFeatureError("non_finite_feature", "feature calculation produced a non-finite value")
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _round_vector(values: np.ndarray, digits: int = 7) -> list[float]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    if not np.all(np.isfinite(flat)):
        raise SonicFeatureError("non_finite_feature", "embedding contains a non-finite value")
    return [_round_float(value, digits) for value in flat]


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SonicFeatureError("non_canonical_feature", "feature data is not canonical JSON") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_sha256(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if len(normalized) != 64 or any(char not in _HEX_64 for char in normalized):
        raise SonicFeatureError("invalid_hash", f"{field} must be a lowercase-compatible SHA-256")
    return normalized


def _unit(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(values))
    if norm <= 1e-12:
        return np.zeros_like(values)
    return values / norm


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    a = np.asarray(left, dtype=np.float64).reshape(-1)
    b = np.asarray(right, dtype=np.float64).reshape(-1)
    if a.size == 0 or a.shape != b.shape or not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 0.0
    return float(np.clip(np.dot(a, b) / denominator, -1.0, 1.0))


def _load_mono_audio(path: os.PathLike[str] | str, config: SonicFeatureConfig) -> tuple[np.ndarray, int, float]:
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError as exc:
        raise SonicFeatureError("audio_unreadable", "audio input is not readable") from exc
    if not candidate.is_file():
        raise SonicFeatureError("audio_not_file", "audio input must be a regular file")
    if stat.st_size <= 0:
        raise SonicFeatureError("audio_empty", "audio input is empty")
    if stat.st_size > config.max_file_bytes:
        raise SonicFeatureError("audio_too_large", "audio input exceeds the configured byte bound")

    try:
        import soundfile as sf

        info = sf.info(str(candidate))
        if int(info.channels) != 1:
            raise SonicFeatureError("audio_not_mono", "decoded audio must contain exactly one channel")
        original_sample_rate = int(info.samplerate)
        if original_sample_rate < 4_000 or original_sample_rate > 384_000:
            raise SonicFeatureError("audio_sample_rate_invalid", "audio sample rate is outside bounds")
        declared_duration = float(info.frames) / original_sample_rate
        if declared_duration < config.min_duration_seconds:
            raise SonicFeatureError("audio_too_short", "audio is shorter than the configured minimum")
        if declared_duration > config.max_duration_seconds + (1.0 / original_sample_rate):
            raise SonicFeatureError("audio_too_long", "audio exceeds the configured analysis bound")
        waveform, read_rate = sf.read(str(candidate), dtype="float32", always_2d=True)
    except SonicFeatureError:
        raise
    except Exception as exc:
        raise SonicFeatureError("audio_decode_failed", "decoded audio could not be read") from exc

    if int(read_rate) != original_sample_rate or waveform.ndim != 2 or waveform.shape[1] != 1:
        raise SonicFeatureError("audio_decode_inconsistent", "decoded audio metadata changed while reading")
    samples = waveform[:, 0].astype(np.float32, copy=False)
    if samples.size == 0 or not np.all(np.isfinite(samples)):
        raise SonicFeatureError("audio_samples_invalid", "decoded samples are empty or non-finite")
    duration = float(samples.size) / original_sample_rate
    if duration < config.min_duration_seconds:
        raise SonicFeatureError("audio_too_short", "decoded audio is shorter than the configured minimum")

    if original_sample_rate != config.analysis_sample_rate:
        divisor = math.gcd(original_sample_rate, config.analysis_sample_rate)
        samples = signal.resample_poly(
            samples,
            config.analysis_sample_rate // divisor,
            original_sample_rate // divisor,
        ).astype(np.float32, copy=False)
    expected_max = int(math.ceil(config.max_duration_seconds * config.analysis_sample_rate)) + 2
    if samples.size > expected_max:
        raise SonicFeatureError("audio_too_long", "resampled audio exceeds the configured analysis bound")
    return samples, original_sample_rate, duration


def _decoded_pcm_hash(samples: np.ndarray) -> str:
    clipped = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2", copy=False)
    return hashlib.sha256(pcm.tobytes(order="C")).hexdigest()


def _frame_view(samples: np.ndarray, frame_length: int, hop: int) -> np.ndarray:
    if samples.size < frame_length:
        samples = np.pad(samples, (0, frame_length - samples.size))
    frame_count = 1 + (samples.size - frame_length) // hop
    shape = (frame_count, frame_length)
    strides = (samples.strides[0] * hop, samples.strides[0])
    return np.lib.stride_tricks.as_strided(samples, shape=shape, strides=strides, writeable=False)


def _spectral_frames(samples: np.ndarray, config: SonicFeatureConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    frames = _frame_view(samples, config.n_fft, config.hop_length)
    window = signal.windows.hann(config.n_fft, sym=False).astype(np.float32)
    spectrum = scipy_fft.rfft(frames * window[None, :], axis=1)
    power = (spectrum.real * spectrum.real + spectrum.imag * spectrum.imag).astype(np.float32)
    frequencies = scipy_fft.rfftfreq(config.n_fft, 1.0 / config.analysis_sample_rate).astype(np.float32)
    return frames, power, frequencies


def _hz_to_mel(frequency: np.ndarray | float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(frequency, dtype=np.float64) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray:
    return 700.0 * (np.power(10.0, np.asarray(mel, dtype=np.float64) / 2595.0) - 1.0)


def _mel_filterbank(sample_rate: int, n_fft: int, bands: int) -> np.ndarray:
    mel_edges = np.linspace(_hz_to_mel(40.0), _hz_to_mel(sample_rate / 2.0), bands + 2)
    hz_edges = _mel_to_hz(mel_edges)
    bins = np.floor((n_fft + 1) * hz_edges / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_fft // 2)
    bank = np.zeros((bands, n_fft // 2 + 1), dtype=np.float32)
    for band in range(bands):
        left, center, right = int(bins[band]), int(bins[band + 1]), int(bins[band + 2])
        center = max(center, left + 1)
        right = max(right, center + 1)
        right = min(right, n_fft // 2)
        if center > left:
            bank[band, left:center] = np.linspace(0.0, 1.0, center - left, endpoint=False)
        if right > center:
            bank[band, center:right] = np.linspace(1.0, 0.0, right - center, endpoint=False)
    return bank


def _chroma(power: np.ndarray, frequencies: np.ndarray) -> np.ndarray:
    result = np.zeros((power.shape[0], 12), dtype=np.float64)
    valid = frequencies >= 50.0
    valid_indices = np.flatnonzero(valid)
    if valid_indices.size == 0:
        return result
    midi = np.rint(69.0 + 12.0 * np.log2(frequencies[valid] / 440.0)).astype(int)
    pitch_classes = np.mod(midi, 12)
    for pitch_class in range(12):
        bins = valid_indices[pitch_classes == pitch_class]
        if bins.size:
            result[:, pitch_class] = np.sum(power[:, bins], axis=1)
    totals = np.sum(result, axis=1, keepdims=True)
    return result / np.maximum(totals, 1e-12)


def _spectral_contrast(power: np.ndarray, frequencies: np.ndarray, bands: int = 6) -> np.ndarray:
    lower, upper = 80.0, min(float(frequencies[-1]), 8_000.0)
    edges = np.geomspace(lower, max(lower + 1.0, upper), bands + 1)
    values = np.zeros((power.shape[0], bands), dtype=np.float64)
    db = 10.0 * np.log10(np.maximum(power, 1e-12))
    for index in range(bands):
        mask = (frequencies >= edges[index]) & (frequencies < edges[index + 1])
        if not np.any(mask):
            continue
        segment = db[:, mask]
        values[:, index] = np.percentile(segment, 90, axis=1) - np.percentile(segment, 10, axis=1)
    return values


def _onset_and_rhythm(log_mel: np.ndarray, sample_rate: int, hop_length: int) -> tuple[np.ndarray, float, float]:
    frame_energy = np.mean(log_mel, axis=1)
    onset = np.maximum(0.0, np.diff(frame_energy, prepend=frame_energy[:1]))
    onset -= float(np.mean(onset))
    autocorrelation = signal.correlate(onset, onset, mode="full", method="fft")[onset.size - 1 :]
    if autocorrelation.size == 0 or autocorrelation[0] <= 1e-12:
        return np.zeros(12, dtype=np.float64), 0.0, 0.0
    autocorrelation /= autocorrelation[0]
    fps = sample_rate / hop_length
    min_lag = max(1, int(math.floor(fps * 60.0 / 240.0)))
    max_lag = min(autocorrelation.size - 1, int(math.ceil(fps * 60.0 / 40.0)))
    if max_lag < min_lag:
        return np.zeros(12, dtype=np.float64), 0.0, 0.0
    region = autocorrelation[min_lag : max_lag + 1]
    best_offset = int(np.argmax(region))
    lag = min_lag + best_offset
    tempo = 60.0 * fps / lag
    regularity = float(np.clip(region[best_offset], 0.0, 1.0))
    positions = np.linspace(min_lag, max_lag, 12)
    rhythm = np.interp(positions, np.arange(autocorrelation.size), autocorrelation)
    return np.clip(rhythm, 0.0, 1.0), float(tempo), regularity


def _window_features(samples: np.ndarray, config: SonicFeatureConfig) -> dict[str, Any]:
    frames, power, frequencies = _spectral_frames(samples, config)
    frame_rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1) + 1e-15)
    total_power = np.sum(power, axis=1) + 1e-12
    normalized = power / total_power[:, None]
    centroid = np.sum(normalized * frequencies[None, :], axis=1)
    bandwidth = np.sqrt(np.sum(normalized * np.square(frequencies[None, :] - centroid[:, None]), axis=1))
    cumulative = np.cumsum(power, axis=1)
    rolloff_indices = np.argmax(cumulative >= 0.85 * cumulative[:, -1:], axis=1)
    rolloff = frequencies[rolloff_indices]
    flatness = np.exp(np.mean(np.log(np.maximum(power, 1e-12)), axis=1)) / np.maximum(
        np.mean(power, axis=1), 1e-12
    )
    signs = np.signbit(frames)
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)

    mel = np.maximum(power @ _mel_filterbank(config.analysis_sample_rate, config.n_fft, config.mel_bands).T, 1e-12)
    log_mel = np.log10(mel)
    mfcc = scipy_fft.dct(log_mel, type=2, axis=1, norm="ortho")[:, :13]
    chroma = _chroma(power, frequencies)
    contrast = _spectral_contrast(power, frequencies)
    rhythm, tempo, regularity = _onset_and_rhythm(log_mel, config.analysis_sample_rate, config.hop_length)

    chroma_distribution = np.mean(chroma, axis=0)
    chroma_distribution /= max(float(np.sum(chroma_distribution)), 1e-12)
    tonal_concentration = 1.0 - float(
        -np.sum(chroma_distribution * np.log(np.maximum(chroma_distribution, 1e-12))) / math.log(12.0)
    )
    median_flatness = float(np.median(flatness))
    non_silence = float(np.mean(frame_rms > 10 ** (-50 / 20)))
    music_likelihood = np.clip(
        0.45 * tonal_concentration
        + 0.25 * (1.0 - np.clip(median_flatness, 0.0, 1.0))
        + 0.20 * regularity
        + 0.10 * non_silence,
        0.0,
        1.0,
    )

    mfcc_scaled = mfcc.copy()
    mfcc_scaled[:, 0] /= 100.0
    mfcc_scaled[:, 1:] /= 25.0
    contrast_scaled = contrast / 40.0
    track = np.concatenate(
        [
            np.mean(chroma, axis=0),
            np.std(chroma, axis=0),
            np.mean(mfcc_scaled[:, :12], axis=0),
            np.std(mfcc_scaled[:, :12], axis=0),
            np.mean(log_mel, axis=0) / 10.0,
            rhythm,
        ]
    )
    nyquist = config.analysis_sample_rate / 2.0
    scalar_series = np.column_stack(
        [
            centroid / nyquist,
            bandwidth / nyquist,
            rolloff / nyquist,
            np.clip(flatness, 0.0, 1.0),
            np.clip(zcr, 0.0, 1.0),
            np.clip((20.0 * np.log10(frame_rms + 1e-12) + 100.0) / 100.0, 0.0, 1.0),
        ]
    )
    style = np.concatenate(
        [
            np.mean(mfcc_scaled, axis=0),
            np.std(mfcc_scaled, axis=0),
            np.mean(contrast_scaled, axis=0),
            np.std(contrast_scaled, axis=0),
            np.mean(scalar_series, axis=0),
            np.std(scalar_series, axis=0),
            np.array([tempo / 240.0, regularity, tonal_concentration, music_likelihood]),
        ]
    )
    track = _unit(track)
    style = _unit(style)
    quality = {
        "rms_dbfs": _round_float(20.0 * math.log10(float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) + 1e-12), 4),
        "silence_fraction": _round_float(1.0 - non_silence, 5),
        "spectral_flatness_median": _round_float(median_flatness, 5),
        "tonal_concentration": _round_float(tonal_concentration, 5),
        "rhythm_regularity": _round_float(regularity, 5),
        "tempo_bpm_proxy": _round_float(tempo, 3),
        "music_likelihood_proxy": _round_float(float(music_likelihood), 5),
    }
    return {
        "quality": quality,
        "track_embedding": _embedding_block(track, "track-classical-v1"),
        "style_embedding": _embedding_block(style, "style-classical-v1"),
    }


def _embedding_block(vector: np.ndarray, algorithm: str) -> dict[str, Any]:
    rounded = _round_vector(vector)
    body = {"algorithm": algorithm, "dimensions": len(rounded), "vector": rounded}
    body["hash"] = _sha256_json(body)
    return body


def _window_starts(sample_count: int, config: SonicFeatureConfig) -> list[int]:
    window = min(sample_count, max(config.n_fft, int(round(config.window_seconds * config.analysis_sample_rate))))
    if sample_count <= window:
        return [0]
    hop = max(1, int(round(config.window_hop_seconds * config.analysis_sample_rate)))
    starts = list(range(0, sample_count - window + 1, hop))
    last = sample_count - window
    if starts[-1] != last:
        starts.append(last)
    if len(starts) > config.max_windows:
        selected = np.linspace(0, len(starts) - 1, config.max_windows)
        indices = sorted({int(round(value)) for value in selected})
        starts = [starts[index] for index in indices]
    return starts


def _global_quality(samples: np.ndarray, sample_rate: int, config: SonicFeatureConfig) -> dict[str, Any]:
    frame_length = min(samples.size, max(256, int(round(0.05 * sample_rate))))
    hop = max(1, frame_length // 2)
    frames = _frame_view(samples, frame_length, hop)
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1) + 1e-15)
    db = 20.0 * np.log10(rms + 1e-12)
    overall_rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))) + 1e-15))
    peak = float(np.max(np.abs(samples)))
    clipping = float(np.mean(np.abs(samples) >= 0.999))
    silence = float(np.mean(db < -50.0))
    if overall_rms < 1e-7:
        status = "silent"
    elif clipping > 0.01:
        status = "clipped"
    elif silence > 0.8:
        status = "mostly_silent"
    else:
        status = "usable"
    return {
        "status": status,
        "rms_dbfs": _round_float(20.0 * math.log10(overall_rms + 1e-12), 4),
        "peak_dbfs": _round_float(20.0 * math.log10(peak + 1e-12), 4),
        "crest_factor_db": _round_float(20.0 * math.log10((peak + 1e-12) / (overall_rms + 1e-12)), 4),
        "dynamic_range_db_proxy": _round_float(max(0.0, float(np.percentile(db, 95) - np.percentile(db, 10))), 4),
        "silence_fraction": _round_float(silence, 6),
        "clipping_fraction": _round_float(clipping, 7),
        "dc_offset": _round_float(float(np.mean(samples)), 7),
    }


def _aggregate_windows(windows: list[dict[str, Any]], key: str) -> tuple[dict[str, Any], list[int]]:
    ranked = sorted(
        range(len(windows)),
        key=lambda index: (-windows[index]["quality"]["music_likelihood_proxy"], index),
    )
    keep_count = max(1, int(math.ceil(len(windows) * 0.6)))
    selected = sorted(ranked[:keep_count])
    vectors = np.asarray([windows[index][key]["vector"] for index in selected], dtype=np.float64)
    aggregate = _unit(np.median(vectors, axis=0))
    algorithm = "track-window-median-v1" if key == "track_embedding" else "style-window-median-v1"
    return _embedding_block(aggregate, algorithm), selected


def _spectral_landmarks(samples: np.ndarray, config: SonicFeatureConfig) -> dict[str, Any]:
    _, power, frequencies = _spectral_frames(samples, config)
    overall_rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))) + 1e-15))
    base_body: dict[str, Any] = {
        "algorithm": "spectral-landmark-v2",
        "time_unit_seconds": _round_float(config.hop_length / config.analysis_sample_rate, 8),
    }
    if overall_rms < 10 ** (-60.0 / 20.0):
        body = {
            **base_body,
            "status": "unavailable",
            "reason": "insufficient_signal",
            "landmark_count": 0,
            "landmarks": [],
        }
        body["hash"] = _sha256_json(body)
        return body
    db = 10.0 * np.log10(np.maximum(power, 1e-12))
    db -= float(np.max(db))
    valid_frequency = (frequencies >= 80.0) & (frequencies <= min(7_500.0, frequencies[-1]))
    local_max = ndimage.maximum_filter(db, size=(5, 11), mode="nearest")
    peak_mask = (db >= local_max - 1e-7) & (db >= -52.0) & valid_frequency[None, :]
    spectral_flatness = np.exp(np.mean(np.log(np.maximum(power, 1e-12)), axis=1)) / np.maximum(
        np.mean(power, axis=1), 1e-12
    )
    frame_rms = np.sqrt(np.sum(power, axis=1) + 1e-15) / config.n_fft
    # White-noise-like spectra have expected flatness near 0.56 under this
    # periodogram, so 0.50 conservatively withholds those frames while keeping
    # tonal and mixed music frames available for landmarking.
    usable_frames = (frame_rms >= 10 ** (-55.0 / 20.0)) & (spectral_flatness <= 0.50)
    peak_mask &= usable_frames[:, None]
    peaks_by_frame: dict[int, list[tuple[int, float]]] = {}
    for frame_index in range(db.shape[0]):
        bins = np.flatnonzero(peak_mask[frame_index])
        if bins.size == 0:
            continue
        order = sorted((int(bin_index) for bin_index in bins), key=lambda item: (-float(db[frame_index, item]), item))[:2]
        frame_peaks: list[tuple[int, float]] = []
        for bin_index in order:
            frequency = max(float(frequencies[bin_index]), 1.0)
            quantized = int(np.clip(round(24.0 * math.log2(frequency / 55.0)) + 64, 0, 255))
            frame_peaks.append((quantized, float(db[frame_index, bin_index])))
        peaks_by_frame[frame_index] = frame_peaks

    landmarks: list[tuple[int, int, float]] = []
    maximum_delta = min(63, max(8, int(round(1.5 * config.analysis_sample_rate / config.hop_length))))
    for anchor_frame in sorted(peaks_by_frame):
        for anchor_frequency, anchor_strength in peaks_by_frame[anchor_frame][:1]:
            candidates: list[tuple[float, int, int]] = []
            for delta in range(2, maximum_delta + 1):
                for target_frequency, strength in peaks_by_frame.get(anchor_frame + delta, ()):  # type: ignore[arg-type]
                    candidates.append((strength, target_frequency, delta))
            candidates.sort(key=lambda item: (-item[0], item[2], item[1]))
            for strength, target_frequency, delta in candidates[:2]:
                token = (anchor_frequency << 14) | (target_frequency << 6) | delta
                landmarks.append((token, anchor_frame, anchor_strength + strength))

    frames_per_second = config.analysis_sample_rate / config.hop_length
    if landmarks:
        bounded: list[tuple[int, int, float]] = []
        buckets: dict[int, list[tuple[int, int, float]]] = {}
        for item in landmarks:
            bucket = int(item[1] / frames_per_second)
            buckets.setdefault(bucket, []).append(item)
        per_second = max(4, int(math.ceil(config.fingerprint_max_landmarks / max(1, len(buckets)))))
        for bucket in sorted(buckets):
            chosen = sorted(buckets[bucket], key=lambda item: (-item[2], item[0], item[1]))[:per_second]
            bounded.extend(chosen)
        landmarks = sorted(bounded, key=lambda item: (item[1], item[0]))[: config.fingerprint_max_landmarks]
    compact = [[int(token), int(anchor)] for token, anchor, _ in landmarks]
    status = "available" if len(compact) >= 12 else "unavailable"
    body: dict[str, Any] = {
        **base_body,
        "status": status,
        "reason": None if status == "available" else "insufficient_signal",
        "landmark_count": len(compact),
        "landmarks": compact if status == "available" else [],
    }
    if status != "available":
        body["landmark_count"] = 0
    body["hash"] = _sha256_json(body)
    return body


def _chromaprint(path: Path, config: SonicFeatureConfig, fpcalc_path: os.PathLike[str] | str | None) -> dict[str, Any]:
    if config.chromaprint_mode == "disabled":
        return {"status": "disabled", "algorithm": "chromaprint"}
    executable = str(fpcalc_path) if fpcalc_path is not None else shutil.which("fpcalc")
    if not executable:
        return {"status": "not_available", "algorithm": "chromaprint"}
    try:
        completed = subprocess.run(
            [executable, "-raw", "-json", "-length", str(int(math.ceil(config.max_duration_seconds))), str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=config.chromaprint_timeout_seconds,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "failed", "algorithm": "chromaprint", "reason": "execution_failed"}
    if completed.returncode != 0 or len(completed.stdout) > 2_000_000:
        return {"status": "failed", "algorithm": "chromaprint", "reason": "nonzero_or_oversized_output"}
    try:
        payload = json.loads(completed.stdout)
        raw = payload.get("fingerprint")
        if isinstance(raw, str):
            values = [int(item) for item in raw.split(",") if item.strip()]
        elif isinstance(raw, list):
            values = [int(item) for item in raw]
        else:
            raise ValueError("missing fingerprint")
        if not values or len(values) > 100_000:
            raise ValueError("invalid fingerprint length")
        values = [value & 0xFFFFFFFF for value in values]
        body: dict[str, Any] = {
            "status": "available",
            "algorithm": "chromaprint-raw",
            "duration_seconds": _round_float(float(payload.get("duration") or 0.0), 3),
            "values": values,
        }
        body["hash"] = _sha256_json(body)
        return body
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"status": "failed", "algorithm": "chromaprint", "reason": "invalid_output"}


def extract_sonic_features(
    audio_path: os.PathLike[str] | str,
    *,
    config: SonicFeatureConfig | None = None,
    fpcalc_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Extract a path-free, deterministic feature payload from bounded mono audio."""

    resolved_config = config or SonicFeatureConfig()
    resolved_config.validate()
    samples, original_sample_rate, original_duration = _load_mono_audio(audio_path, resolved_config)
    duration = samples.size / resolved_config.analysis_sample_rate
    starts = _window_starts(samples.size, resolved_config)
    window_size = min(
        samples.size,
        max(resolved_config.n_fft, int(round(resolved_config.window_seconds * resolved_config.analysis_sample_rate))),
    )
    windows: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        stop = min(samples.size, start + window_size)
        derived = _window_features(samples[start:stop], resolved_config)
        windows.append(
            {
                "index": index,
                "start_seconds": _round_float(start / resolved_config.analysis_sample_rate, 4),
                "end_seconds": _round_float(stop / resolved_config.analysis_sample_rate, 4),
                **derived,
            }
        )
    track_embedding, selected_track = _aggregate_windows(windows, "track_embedding")
    style_embedding, selected_style = _aggregate_windows(windows, "style_embedding")
    fingerprint = _spectral_landmarks(samples, resolved_config)
    chromaprint = _chromaprint(Path(audio_path), resolved_config, fpcalc_path)
    decoded_hash = _decoded_pcm_hash(samples)
    payload: dict[str, Any] = {
        "algorithm_version": _ALGORITHM_VERSION,
        "config": asdict(resolved_config),
        "audio_summary": {
            "original_sample_rate": original_sample_rate,
            "analysis_sample_rate": resolved_config.analysis_sample_rate,
            "duration_seconds": _round_float(original_duration, 5),
            "analyzed_duration_seconds": _round_float(duration, 5),
            "decoded_pcm_sha256": decoded_hash,
        },
        "quality": _global_quality(samples, resolved_config.analysis_sample_rate, resolved_config),
        "window_count": len(windows),
        "windows": windows,
        "aggregate": {
            "selected_track_window_indices": selected_track,
            "selected_style_window_indices": selected_style,
            "track_embedding": track_embedding,
            "style_embedding": style_embedding,
        },
        "fingerprints": {
            "spectral_landmark": fingerprint,
            "chromaprint": chromaprint,
        },
    }
    payload["feature_hash"] = _sha256_json(payload)
    return payload


def analyze_audio(
    audio_path: os.PathLike[str] | str,
    *,
    post_id: str,
    audio_sha256: str | None = None,
    config: SonicFeatureConfig | None = None,
    fpcalc_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """CLI-friendly feature entrypoint; returns no source path or media content."""

    normalized_post_id = str(post_id).strip()
    if not normalized_post_id or len(normalized_post_id) > 128 or any(ord(char) < 32 for char in normalized_post_id):
        raise SonicFeatureError("invalid_post_id", "post_id must be a bounded printable identifier")
    source_hash = _validate_sha256(audio_sha256, "audio_sha256")
    features = extract_sonic_features(audio_path, config=config, fpcalc_path=fpcalc_path)
    record: dict[str, Any] = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "post_id": normalized_post_id,
        "source_audio_sha256": source_hash,
        **features,
    }
    record["record_hash"] = _sha256_json(record)
    return record


def validate_feature_record(record: Mapping[str, Any]) -> None:
    """Validate schema, binding, feature hash, record hash, and vector shapes."""

    if record.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise SonicFeatureError("invalid_feature_schema", "feature record schema is unsupported")
    post_id = record.get("post_id")
    if not isinstance(post_id, str) or not post_id:
        raise SonicFeatureError("invalid_feature_record", "feature record has no post binding")
    expected_record_hash = record.get("record_hash")
    if not isinstance(expected_record_hash, str):
        raise SonicFeatureError("invalid_feature_record", "feature record has no record hash")
    unhashed_record = dict(record)
    unhashed_record.pop("record_hash", None)
    if _sha256_json(unhashed_record) != expected_record_hash:
        raise SonicFeatureError("record_hash_mismatch", "feature record hash does not verify")

    feature_keys = [
        "algorithm_version",
        "config",
        "audio_summary",
        "quality",
        "window_count",
        "windows",
        "aggregate",
        "fingerprints",
    ]
    feature_payload = {key: record.get(key) for key in feature_keys}
    claimed_feature_hash = record.get("feature_hash")
    feature_payload["feature_hash"] = claimed_feature_hash
    computed_payload = dict(feature_payload)
    computed_payload.pop("feature_hash", None)
    if not isinstance(claimed_feature_hash, str) or _sha256_json(computed_payload) != claimed_feature_hash:
        raise SonicFeatureError("feature_hash_mismatch", "audio-derived feature hash does not verify")
    windows = record.get("windows")
    if not isinstance(windows, list) or len(windows) != record.get("window_count") or not windows:
        raise SonicFeatureError("invalid_feature_record", "window count does not verify")
    for container in [record.get("aggregate"), *windows]:
        if not isinstance(container, Mapping):
            raise SonicFeatureError("invalid_feature_record", "embedding container is missing")
        for key in ("track_embedding", "style_embedding"):
            block = container.get(key)
            if not isinstance(block, Mapping) or not isinstance(block.get("vector"), list):
                raise SonicFeatureError("invalid_feature_record", "embedding block is missing")
            body = {item: block.get(item) for item in ("algorithm", "dimensions", "vector")}
            if block.get("dimensions") != len(block["vector"]) or _sha256_json(body) != block.get("hash"):
                raise SonicFeatureError("embedding_hash_mismatch", "embedding hash does not verify")


def _landmark_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    if left.get("status") != "available" or right.get("status") != "available":
        return 0.0
    left_items = left.get("landmarks")
    right_items = right.get("landmarks")
    if not isinstance(left_items, list) or not isinstance(right_items, list) or not left_items or not right_items:
        return 0.0
    left_map: dict[int, list[int]] = {}
    right_map: dict[int, list[int]] = {}
    for item in left_items:
        if isinstance(item, list) and len(item) == 2:
            left_map.setdefault(int(item[0]), []).append(int(item[1]))
    for item in right_items:
        if isinstance(item, list) and len(item) == 2:
            right_map.setdefault(int(item[0]), []).append(int(item[1]))
    votes: dict[int, int] = {}
    common_occurrences = 0
    for token in left_map.keys() & right_map.keys():
        left_times = left_map[token][:8]
        right_times = right_map[token][:8]
        common_occurrences += min(len(left_times), len(right_times))
        for left_time in left_times:
            for right_time in right_times:
                delta = right_time - left_time
                votes[delta] = votes.get(delta, 0) + 1
    if not votes:
        return 0.0
    aligned = max(votes.get(delta - 1, 0) + votes.get(delta, 0) + votes.get(delta + 1, 0) for delta in votes)
    denominator = max(1, min(len(left_items), len(right_items)))
    return float(np.clip(aligned / denominator, 0.0, 1.0))


def _chromaprint_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    if left.get("status") != "available" or right.get("status") != "available":
        return 0.0
    left_values = [int(value) & 0xFFFFFFFF for value in left.get("values", [])]
    right_values = [int(value) & 0xFFFFFFFF for value in right.get("values", [])]
    if not left_values or not right_values:
        return 0.0
    shorter, longer = (left_values, right_values) if len(left_values) <= len(right_values) else (right_values, left_values)
    minimum_overlap = min(len(shorter), max(16, int(math.ceil(len(shorter) * 0.35))))
    if minimum_overlap > len(shorter):
        minimum_overlap = len(shorter)
    best = 0.0
    for offset in range(-len(shorter) + minimum_overlap, len(longer) - minimum_overlap + 1):
        short_start = max(0, -offset)
        long_start = max(0, offset)
        overlap = min(len(shorter) - short_start, len(longer) - long_start)
        if overlap < minimum_overlap:
            continue
        equal_bits = 0
        for index in range(overlap):
            difference = shorter[short_start + index] ^ longer[long_start + index]
            equal_bits += 32 - int(difference).bit_count()
        raw = equal_bits / (32.0 * overlap)
        normalized = np.clip((raw - 0.5) / 0.5, 0.0, 1.0)
        best = max(best, float(normalized))
    return best


def _max_window_cosine(left: Mapping[str, Any], right: Mapping[str, Any], embedding_key: str) -> float:
    left_windows = left.get("windows") if isinstance(left.get("windows"), list) else []
    right_windows = right.get("windows") if isinstance(right.get("windows"), list) else []
    best = -1.0
    for left_window in left_windows:
        for right_window in right_windows:
            try:
                score = _cosine(left_window[embedding_key]["vector"], right_window[embedding_key]["vector"])
            except (KeyError, TypeError):
                continue
            best = max(best, score)
    if best < -0.5:
        try:
            return _cosine(
                left["aggregate"][embedding_key]["vector"],
                right["aggregate"][embedding_key]["vector"],
            )
        except (KeyError, TypeError):
            return 0.0
    return float(np.clip((best + 1.0) / 2.0, 0.0, 1.0))


def recording_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two post-level feature records without treating similarity as identity."""

    try:
        exact_pcm = left["audio_summary"]["decoded_pcm_sha256"] == right["audio_summary"]["decoded_pcm_sha256"]
        left_fingerprints = left["fingerprints"]
        right_fingerprints = right["fingerprints"]
    except (KeyError, TypeError) as exc:
        raise SonicFeatureError("invalid_feature_record", "similarity input is incomplete") from exc
    left_quality = str((left.get("quality") or {}).get("status") or "missing")
    right_quality = str((right.get("quality") or {}).get("status") or "missing")
    if left_quality != "usable" or right_quality != "usable":
        reasons = []
        if left_quality != "usable":
            reasons.append(f"left_quality_{left_quality}")
        if right_quality != "usable":
            reasons.append(f"right_quality_{right_quality}")
        result: dict[str, Any] = {
            "comparable": False,
            "reason": "+".join(reasons),
            "exact_decoded_pcm": bool(exact_pcm),
            "recording_score": 0.0,
            "spectral_landmark_score": 0.0,
            "spectral_landmark_evidence_score": 0.0,
            "chromaprint_score": 0.0,
            "track_embedding_score": 0.0,
            "style_embedding_score": 0.0,
        }
        result["comparison_hash"] = _sha256_json(result)
        return result

    left_landmarks = left_fingerprints["spectral_landmark"]
    right_landmarks = right_fingerprints["spectral_landmark"]
    left_chromaprint = left_fingerprints["chromaprint"]
    right_chromaprint = right_fingerprints["chromaprint"]
    landmark = _landmark_similarity(left_landmarks, right_landmarks)
    chromaprint = _chromaprint_similarity(left_chromaprint, right_chromaprint)
    # Landmark alignment is a sparse-match fraction.  Six to eight percent of
    # a bounded landmark packet agreeing at one time offset is already strong
    # evidence; unrelated synthetic clips in the focused calibration produce
    # no aligned landmarks.  This is an internal retrieval scale, not a claim
    # of catalog identity, and the pilot must recalibrate it on real labels.
    landmark_evidence = min(1.0, landmark / 0.08)
    fingerprint = max(landmark_evidence, chromaprint)
    track = _max_window_cosine(left, right, "track_embedding")
    style = _max_window_cosine(left, right, "style_embedding")
    has_common_fingerprint_method = (
        left_landmarks.get("status") == "available"
        and right_landmarks.get("status") == "available"
    ) or (
        left_chromaprint.get("status") == "available"
        and right_chromaprint.get("status") == "available"
    )
    if not exact_pcm and not has_common_fingerprint_method:
        result = {
            "comparable": False,
            "reason": "insufficient_fingerprint_evidence",
            "exact_decoded_pcm": False,
            "recording_score": 0.0,
            "spectral_landmark_score": _round_float(landmark, 6),
            "spectral_landmark_evidence_score": _round_float(landmark_evidence, 6),
            "chromaprint_score": _round_float(chromaprint, 6),
            "track_embedding_score": _round_float(track, 6),
            "style_embedding_score": _round_float(style, 6),
        }
        result["comparison_hash"] = _sha256_json(result)
        return result
    if exact_pcm:
        score = 1.0
    elif fingerprint >= 0.20:
        score = 0.80 * fingerprint + 0.20 * track
    else:
        # Embeddings alone are intentionally capped below the default match threshold.
        score = min(0.69, 0.60 * track + 0.09 * style)
    result = {
        "comparable": True,
        "reason": None,
        "exact_decoded_pcm": bool(exact_pcm),
        "recording_score": _round_float(float(np.clip(score, 0.0, 1.0)), 6),
        "spectral_landmark_score": _round_float(landmark, 6),
        "spectral_landmark_evidence_score": _round_float(landmark_evidence, 6),
        "chromaprint_score": _round_float(chromaprint, 6),
        "track_embedding_score": _round_float(track, 6),
        "style_embedding_score": _round_float(style, 6),
    }
    result["comparison_hash"] = _sha256_json(result)
    return result


def _labels_from_references(
    records: Sequence[Mapping[str, Any]],
    references: Mapping[str, str] | Sequence[Mapping[str, Any]] | None,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    if references is None:
        for record in records:
            label = record.get("reference_label")
            if label is not None and str(label).strip():
                labels[str(record.get("post_id"))] = str(label).strip()
        return labels
    if isinstance(references, Mapping):
        iterator = references.items()
    else:
        iterator = []
        for item in references:
            post_id = item.get("post_id")
            label = item.get("reference_label", item.get("label"))
            iterator.append((post_id, label))
    for post_id, label in iterator:
        if post_id is None or label is None or not str(label).strip():
            continue
        labels[str(post_id)] = str(label).strip()
    return labels


def build_similarity_report(
    records: Sequence[Mapping[str, Any]],
    references: Mapping[str, str] | Sequence[Mapping[str, Any]] | None = None,
    *,
    threshold: float = 0.72,
) -> dict[str, Any]:
    """Build leave-one-post-out retrieval and threshold metrics from known labels."""

    if not 0.0 <= threshold <= 1.0:
        raise SonicFeatureError("invalid_threshold", "similarity threshold must be between zero and one")
    materialized = list(records)
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in materialized:
        post_id = str(record.get("post_id") or "")
        if not post_id or post_id in by_id:
            raise SonicFeatureError("invalid_post_set", "post identifiers must be present and unique")
        by_id[post_id] = record
    labels = _labels_from_references(materialized, references)
    unknown_reference_ids = sorted(set(labels) - set(by_id))
    if unknown_reference_ids:
        raise SonicFeatureError("unknown_reference", "a reference label targets a record outside the set")

    quality_exclusions: dict[str, str] = {}
    for post_id, record in by_id.items():
        quality = record.get("quality")
        status = str(quality.get("status") or "missing") if isinstance(quality, Mapping) else "missing"
        if status != "usable":
            quality_exclusions[post_id] = f"quality_{status}"
    quality_exclusion_counts: dict[str, int] = {}
    for reason_value in quality_exclusions.values():
        quality_exclusion_counts[reason_value] = quality_exclusion_counts.get(reason_value, 0) + 1

    input_labeled_ids = sorted(post_id for post_id in by_id if post_id in labels)
    labeled_ids = [post_id for post_id in input_labeled_ids if post_id not in quality_exclusions]
    if len(materialized) < 2 or len(labeled_ids) < 2:
        if len(materialized) < 2:
            reason = "insufficient_records"
        elif len(input_labeled_ids) < 2:
            reason = "insufficient_reference_labels"
        else:
            reason = "insufficient_usable_reference_labels"
        report: dict[str, Any] = {
            "schema_version": SIMILARITY_REPORT_SCHEMA_VERSION,
            "status": "not_evaluable",
            "reason": reason,
            "threshold": _round_float(threshold, 6),
            "record_count": len(materialized),
            "input_labeled_record_count": len(input_labeled_ids),
            "labeled_record_count": len(labeled_ids),
            "quality_excluded_record_count": len(quality_exclusions),
            "quality_exclusion_counts": quality_exclusion_counts,
            "distinct_reference_count": len({labels[post_id] for post_id in labeled_ids}),
            "repeated_reference_count": 0,
            "evaluable_query_count": 0,
            "resolved_query_count": 0,
            "unresolved_query_count": 0,
            "abstention_rate": None,
            "recall_at_1": None,
            "recall_at_5": None,
            "pair_metrics": {
                "positive_pairs": 0,
                "negative_pairs": 0,
                "comparable_pairs": 0,
                "incomparable_pairs": 0,
                "incomparable_positive_pairs": 0,
                "true_positive": 0,
                "false_positive": 0,
                "true_negative": 0,
                "false_negative": 0,
                "precision_at_threshold": None,
                "true_positive_rate_at_threshold": None,
                "false_match_rate_at_threshold": None,
            },
            "nearest_by_query": [],
        }
        report["report_hash"] = _sha256_json(report)
        return report
    comparisons: dict[tuple[str, str], dict[str, Any]] = {}
    tp = fp = tn = fn = 0
    positive_pairs = negative_pairs = incomparable_pairs = 0
    incomparable_positive_pairs = 0
    for left_index, left_id in enumerate(labeled_ids):
        for right_id in labeled_ids[left_index + 1 :]:
            comparison = recording_similarity(by_id[left_id], by_id[right_id])
            comparisons[(left_id, right_id)] = comparison
            truth = labels[left_id] == labels[right_id]
            if truth:
                positive_pairs += 1
            else:
                negative_pairs += 1
            if not comparison.get("comparable"):
                incomparable_pairs += 1
                if truth:
                    # End-to-end identity accuracy must include abstention on
                    # a true positive pair; otherwise TPR is overstated by
                    # conditioning silently on comparability.
                    incomparable_positive_pairs += 1
                    fn += 1
                continue
            predicted = comparison["recording_score"] >= threshold
            if truth:
                if predicted:
                    tp += 1
                else:
                    fn += 1
            else:
                if predicted:
                    fp += 1
                else:
                    tn += 1

    label_counts: dict[str, int] = {}
    for post_id in labeled_ids:
        label = labels[post_id]
        label_counts[label] = label_counts.get(label, 0) + 1
    evaluable = [post_id for post_id in labeled_ids if label_counts[labels[post_id]] >= 2]
    hits_at_1 = hits_at_5 = resolved_queries = 0
    nearest: list[dict[str, Any]] = []
    for query_id in evaluable:
        candidates: list[tuple[float, str]] = []
        for candidate_id in labeled_ids:
            if candidate_id == query_id:
                continue
            pair = tuple(sorted((query_id, candidate_id)))
            if not comparisons[pair].get("comparable"):
                continue
            score = float(comparisons[pair]["recording_score"])
            candidates.append((score, candidate_id))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        first_five = candidates[:5]
        hit_one = bool(candidates and labels[candidates[0][1]] == labels[query_id])
        hit_five = any(labels[candidate_id] == labels[query_id] for _, candidate_id in first_five)
        resolved = bool(candidates and candidates[0][0] >= threshold)
        hits_at_1 += int(hit_one)
        hits_at_5 += int(hit_five)
        resolved_queries += int(resolved)
        nearest.append(
            {
                "query_post_id": query_id,
                "top_post_id": candidates[0][1] if candidates else None,
                "top_score": _round_float(candidates[0][0], 6) if candidates else None,
                "resolved_at_threshold": resolved,
                "predicted_reference_label": labels[candidates[0][1]] if resolved and candidates else None,
                "hit_at_1": hit_one,
                "hit_at_5": hit_five,
            }
        )

    predictions = tp + fp
    report: dict[str, Any] = {
        "schema_version": SIMILARITY_REPORT_SCHEMA_VERSION,
        "status": "evaluated" if evaluable else "not_evaluable",
        "reason": None if evaluable else "no_repeated_reference_labels",
        "threshold": _round_float(threshold, 6),
        "record_count": len(materialized),
        "input_labeled_record_count": len(input_labeled_ids),
        "labeled_record_count": len(labeled_ids),
        "quality_excluded_record_count": len(quality_exclusions),
        "quality_exclusion_counts": quality_exclusion_counts,
        "distinct_reference_count": len({labels[post_id] for post_id in labeled_ids}),
        "repeated_reference_count": sum(1 for count in label_counts.values() if count >= 2),
        "evaluable_query_count": len(evaluable),
        "resolved_query_count": resolved_queries,
        "unresolved_query_count": len(evaluable) - resolved_queries,
        "abstention_rate": _round_float((len(evaluable) - resolved_queries) / len(evaluable), 6)
        if evaluable
        else None,
        "recall_at_1": _round_float(hits_at_1 / len(evaluable), 6) if evaluable else None,
        "recall_at_5": _round_float(hits_at_5 / len(evaluable), 6) if evaluable else None,
        "pair_metrics": {
            "positive_pairs": positive_pairs,
            "negative_pairs": negative_pairs,
            "comparable_pairs": positive_pairs + negative_pairs - incomparable_pairs,
            "incomparable_pairs": incomparable_pairs,
            "incomparable_positive_pairs": incomparable_positive_pairs,
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
            "precision_at_threshold": _round_float(tp / predictions, 6) if predictions else None,
            "true_positive_rate_at_threshold": _round_float(tp / (tp + fn), 6) if tp + fn else None,
            "false_match_rate_at_threshold": _round_float(fp / (fp + tn), 6) if fp + tn else None,
        },
        "nearest_by_query": nearest,
    }
    report["report_hash"] = _sha256_json(report)
    return report


def _kmeans(vectors: np.ndarray, clusters: int, seed: int, max_iterations: int = 100) -> np.ndarray:
    generator = np.random.default_rng(seed)
    first = int(generator.integers(0, vectors.shape[0]))
    centers = [vectors[first]]
    while len(centers) < clusters:
        distance = np.min(
            np.stack([np.sum(np.square(vectors - center), axis=1) for center in centers], axis=1),
            axis=1,
        )
        total = float(np.sum(distance))
        if total <= 1e-12:
            next_index = next(index for index in range(vectors.shape[0]) if index >= len(centers))
        else:
            next_index = int(generator.choice(vectors.shape[0], p=distance / total))
        centers.append(vectors[next_index])
    center_array = np.asarray(centers, dtype=np.float64)
    labels = np.full(vectors.shape[0], -1, dtype=int)
    for _ in range(max_iterations):
        distances = np.stack([np.sum(np.square(vectors - center), axis=1) for center in center_array], axis=1)
        new_labels = np.argmin(distances, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for cluster in range(clusters):
            members = vectors[labels == cluster]
            if members.size:
                center_array[cluster] = _unit(np.mean(members, axis=0))
    return labels


def _comb2(value: int) -> float:
    return value * (value - 1) / 2.0


def _adjusted_rand_index(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape or left.size < 2:
        return 1.0
    left_values = {value: index for index, value in enumerate(sorted(set(left.tolist())))}
    right_values = {value: index for index, value in enumerate(sorted(set(right.tolist())))}
    table = np.zeros((len(left_values), len(right_values)), dtype=int)
    for left_label, right_label in zip(left, right):
        table[left_values[int(left_label)], right_values[int(right_label)]] += 1
    sum_cells = sum(_comb2(int(value)) for value in table.flat)
    sum_rows = sum(_comb2(int(value)) for value in np.sum(table, axis=1))
    sum_columns = sum(_comb2(int(value)) for value in np.sum(table, axis=0))
    total_pairs = _comb2(left.size)
    if total_pairs <= 0:
        return 1.0
    expected = sum_rows * sum_columns / total_pairs
    maximum = 0.5 * (sum_rows + sum_columns)
    denominator = maximum - expected
    if abs(denominator) <= 1e-12:
        return 1.0 if np.array_equal(left, right) else 0.0
    return float(np.clip((sum_cells - expected) / denominator, -1.0, 1.0))


def evaluate_clustering_stability(
    records: Sequence[Mapping[str, Any] | Sequence[float]],
    *,
    n_clusters: int,
    repeats: int = 8,
    seed: int = 17,
) -> dict[str, Any]:
    """Measure style-cluster stability under deterministic window resampling."""

    materialized = list(records)
    if len(materialized) < 3 or not 2 <= n_clusters < len(materialized):
        raise SonicFeatureError("invalid_cluster_shape", "clusters must be between two and record_count - 1")
    if not 2 <= repeats <= 64:
        raise SonicFeatureError("invalid_cluster_repeats", "stability repeats must be between two and 64")
    mapping_mode = all(isinstance(record, Mapping) for record in materialized)
    vector_mode = all(not isinstance(record, Mapping) for record in materialized)
    if not (mapping_mode or vector_mode):
        raise SonicFeatureError("invalid_style_vectors", "style inputs must use one consistent representation")
    if mapping_mode:
        mapped = [record for record in materialized if isinstance(record, Mapping)]
        post_ids = [str(record.get("post_id") or "") for record in mapped]
        if any(not post_id for post_id in post_ids) or len(set(post_ids)) != len(post_ids):
            raise SonicFeatureError("invalid_post_set", "post identifiers must be present and unique")
    else:
        post_ids = [str(index) for index in range(len(materialized))]
    generator = np.random.default_rng(seed)
    assignments: list[np.ndarray] = []
    effective_distinct_counts: list[int] = []
    occupied_cluster_counts: list[int] = []
    resampling_basis = "aggregate_vectors"
    for repeat in range(repeats):
        vectors: list[np.ndarray] = []
        for record in materialized:
            if isinstance(record, Mapping):
                windows = record.get("windows")
                if isinstance(windows, list) and windows:
                    resampling_basis = "window_bootstrap"
                    window_vectors = np.asarray(
                        [window["style_embedding"]["vector"] for window in windows], dtype=np.float64
                    )
                    if not np.all(np.isfinite(window_vectors)):
                        raise SonicFeatureError("invalid_feature_record", "style embedding contains non-finite values")
                    draw_count = max(1, int(math.ceil(window_vectors.shape[0] * 0.7)))
                    indices = generator.integers(0, window_vectors.shape[0], size=draw_count)
                    vectors.append(_unit(np.median(window_vectors[indices], axis=0)))
                    continue
                try:
                    vector = (
                        record["style_vector"]
                        if "style_vector" in record
                        else record["aggregate"]["style_embedding"]["vector"]
                    )
                except (KeyError, TypeError) as exc:
                    raise SonicFeatureError("invalid_feature_record", "style vector is missing") from exc
            else:
                vector = record
            array = np.asarray(vector, dtype=np.float64).reshape(-1)
            if array.size == 0 or not np.all(np.isfinite(array)):
                raise SonicFeatureError("invalid_style_vectors", "style vector is empty or non-finite")
            vectors.append(_unit(array))
        dimensions = {vector.size for vector in vectors}
        if len(dimensions) != 1:
            raise SonicFeatureError("invalid_style_vectors", "style vector dimensions are inconsistent")
        vector_array = np.asarray(vectors)
        distinct_count = int(np.unique(np.round(vector_array, 7), axis=0).shape[0])
        effective_distinct_counts.append(distinct_count)
        if distinct_count < n_clusters:
            labels = np.zeros(len(vectors), dtype=int)
        else:
            labels = _kmeans(vector_array, n_clusters, seed + repeat * 104729)
        assignments.append(labels)
        occupied_cluster_counts.append(len(set(labels.tolist())))
    scores = [
        _adjusted_rand_index(assignments[left], assignments[right])
        for left in range(len(assignments))
        for right in range(left + 1, len(assignments))
    ]
    consensus = []
    for post_index, post_id in enumerate(post_ids):
        consensus.append({"post_id": post_id, "assignments": [int(item[post_index]) for item in assignments]})
    degenerate = any(
        distinct < n_clusters or occupied < n_clusters
        for distinct, occupied in zip(effective_distinct_counts, occupied_cluster_counts)
    )
    report: dict[str, Any] = {
        "schema_version": STABILITY_REPORT_SCHEMA_VERSION,
        "status": "not_evaluable" if degenerate else "evaluated",
        "reason": "degenerate_style_vectors" if degenerate else None,
        "record_count": len(materialized),
        "n_clusters": n_clusters,
        "repeats": repeats,
        "seed": seed,
        "resampling_basis": resampling_basis,
        "minimum_effective_distinct_vectors": min(effective_distinct_counts),
        "maximum_effective_distinct_vectors": max(effective_distinct_counts),
        "occupied_cluster_counts": occupied_cluster_counts,
        "mean_adjusted_rand_index": None if degenerate else _round_float(float(np.mean(scores)), 6),
        "minimum_adjusted_rand_index": None if degenerate else _round_float(float(np.min(scores)), 6),
        "maximum_adjusted_rand_index": None if degenerate else _round_float(float(np.max(scores)), 6),
        "pairwise_comparison_count": len(scores),
        "assignments": consensus,
    }
    report["report_hash"] = _sha256_json(report)
    return report
