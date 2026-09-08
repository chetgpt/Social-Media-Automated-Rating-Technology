"""Offline contract tests for the AUDIO ARCHIVE transport adapter.

These tests replace the browser/network side of SONIC transport. They prove
that one bounded acquisition produces one inseparable M4A/MP3 pair and that no
durable checkpoint is exposed until temporary source cleanup and promotion of
both formats have completed.
"""

import asyncio
import hashlib
import inspect
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_archive_transport as archive
import sonic_audio_transport as sonic


RUN_ID = "audio_archive_test_creator_bankbca_2p_20260829_010203_123456"
POST_A = "7673070116011216148"
POST_B = "7673070116011216149"
CREATOR = "bankbca"


def candidate(post_id):
    return {
        "post_id": post_id,
        "canonical_url": f"https://www.tiktok.com/@{CREATOR}/video/{post_id}",
        "creator_handle": CREATOR,
        "content_type": "video",
    }


def destination_pair(output, ordinal, post_id):
    return {
        "m4a": output / f"{ordinal:04d}_{post_id}.m4a",
        "mp3": output / f"{ordinal:04d}_{post_id}.mp3",
    }


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class FixedInspector:
    def __init__(self, duration=2.0):
        self.duration = duration

    def __call__(self, path):
        return sonic.MediaProbe(self.duration, Path(path).stat().st_size)


class SyntheticMP3Transcoder:
    """Deterministic non-media stand-in for the local ffmpeg MP3 pass."""

    def __init__(self, *, fail=False, omit_output=False):
        self.fail = fail
        self.omit_output = omit_output
        self.calls = []

    def __call__(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        self.calls.append((source, destination))
        if self.fail:
            raise archive.AudioArchiveTransportError("synthetic_mp3_failure")
        if not self.omit_output:
            destination.write_bytes(b"mp3-derived-from:" + source.read_bytes())


class SyntheticSonicTransport:
    """No-browser transport double that preserves SONIC callback ordering."""

    def __init__(
        self,
        temp_root,
        *,
        unavailable=(),
        after_cleanup=None,
        corrupt_receipt=(),
    ):
        self.temp_root = Path(temp_root)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.limits = sonic.AcquisitionLimits(
            max_audio_bytes=archive.ARCHIVE_AUDIO_CAP_BYTES,
        )
        self.unavailable = set(unavailable)
        self.after_cleanup = after_cleanup
        self.corrupt_receipt = set(corrupt_receipt)
        self.calls = []
        self.live_paths = set()

    @staticmethod
    def provenance():
        return {
            "browser_profile": "Profile 7",
            "browser_mode": "existing_profile_attach",
            "account_handle": "operator",
            "identity_binding": "exact_post_id_and_creator_from_tiktok_html",
            "media_transport": "bounded_injected_test_stream",
            "audio_conversion": archive.ARCHIVE_AUDIO_CONVERSION,
        }

    async def process_candidates(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["continue_on_item_error"] is True
        receipts = []
        for row in kwargs["candidates"]:
            post_id = row["post_id"]
            if post_id in self.unavailable:
                receipt = sonic.SonicTransportReceipt(
                    post_id=post_id,
                    creator_handle=row["creator_handle"],
                    duration_seconds=0.0,
                    source_byte_count=0,
                    audio_byte_count=0,
                    source_sha256="",
                    audio_sha256="",
                    provenance=self.provenance(),
                    status="unavailable",
                    error_code="synthetic_unavailable",
                )
                await maybe_await(kwargs["receipt_processor"](receipt))
                receipts.append(receipt)
                continue

            item_dir = self.temp_root / f"synthetic-{post_id}"
            item_dir.mkdir()
            source_path = item_dir / "source-video.bin"
            audio_path = item_dir / "normalized-audio.m4a"
            source_payload = ("source-" + post_id).encode("ascii")
            audio_payload = ("m4a-" + post_id).encode("ascii") * 4
            source_path.write_bytes(source_payload)
            audio_path.write_bytes(audio_payload)
            self.live_paths.update((source_path, audio_path))
            item = sonic.TransientAudioItem(
                post_id=post_id,
                creator_handle=row["creator_handle"],
                source_media_path=source_path,
                audio_path=audio_path,
                duration_seconds=2.0,
                source_byte_count=len(source_payload),
                audio_byte_count=len(audio_payload),
                source_sha256=hashlib.sha256(source_payload).hexdigest(),
                audio_sha256=hashlib.sha256(audio_payload).hexdigest(),
                provenance=self.provenance(),
                timing={},
            )
            try:
                derived = await maybe_await(kwargs["processor"](item))
            finally:
                source_path.unlink()
                audio_path.unlink()
                item_dir.rmdir()
                self.live_paths.clear()
            if self.after_cleanup is not None:
                self.after_cleanup(post_id)
            audio_hash = item.audio_sha256
            if post_id in self.corrupt_receipt:
                audio_hash = "0" * 64
            receipt = sonic.SonicTransportReceipt(
                post_id=post_id,
                creator_handle=row["creator_handle"],
                duration_seconds=item.duration_seconds,
                source_byte_count=item.source_byte_count,
                audio_byte_count=item.audio_byte_count,
                source_sha256=item.source_sha256,
                audio_sha256=audio_hash,
                provenance=self.provenance(),
                derived_result=derived,
            )
            await maybe_await(kwargs["receipt_processor"](receipt))
            receipts.append(receipt)
        return receipts


def transport_instance(tmp_path, fake, *, transcoder=None, staging_root=None):
    return archive.AudioArchiveTransport(
        sonic_transport=fake,
        staging_root=staging_root,
        mp3_transcoder=transcoder or SyntheticMP3Transcoder(),
        mp3_inspector=FixedInspector(),
    )


def run_archive(instance, candidates, destinations, **kwargs):
    return asyncio.run(
        instance.archive_candidates(
            RUN_ID,
            candidates,
            destinations,
            **kwargs,
        )
    )


def test_archive_run_maps_to_deterministic_sonic_owner():
    first = archive.sonic_owner_run_id(RUN_ID)
    assert first == archive.sonic_owner_run_id(RUN_ID)
    assert first.startswith("sonic_")
    assert len(first) == len("sonic_") + 16
    assert first != archive.sonic_owner_run_id(RUN_ID + "_other")


def test_default_transport_uses_dual_transcoders_ffprobe_and_16_mib_cap():
    instance = archive.AudioArchiveTransport()
    wrapped = instance._transport()
    assert isinstance(wrapped.audio_transcoder, archive.ArchiveM4ATranscoder)
    assert isinstance(wrapped.audio_inspector, archive.ArchiveFFprobeInspector)
    assert wrapped.audio_inspector.expected_format == "m4a"
    # MP3 helpers are constructed lazily inside archive_candidates so merely
    # asking for the wrapped SONIC transport never initializes ffmpeg work.
    assert instance.mp3_transcoder is None
    assert instance.mp3_inspector is None
    assert archive.ArchiveMP3Transcoder is not None
    assert wrapped.audio_conversion == archive.ARCHIVE_AUDIO_CONVERSION
    assert wrapped.limits.max_audio_bytes == 16 * 1024 * 1024


def test_m4a_transcoder_forces_normalized_metadata_free_aac(monkeypatch, tmp_path):
    observed = {}
    monkeypatch.setattr(archive.shutil, "which", lambda value: "ffmpeg.exe")

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs

    monkeypatch.setattr(archive.subprocess, "run", fake_run)
    source = tmp_path / "source.bin"
    destination = tmp_path / "audio.m4a"
    archive.ArchiveM4ATranscoder()(source, destination)
    argv = observed["argv"]
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-profile:a") + 1] == "aac_low"
    assert argv[argv.index("-b:a") + 1] == "192k"
    assert argv[argv.index("-ar") + 1] == "44100"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-map_metadata") + 1] == "-1"
    assert argv[argv.index("-map_chapters") + 1] == "-1"
    assert argv[argv.index("-f") + 1] == "ipod"
    assert "-vn" in argv
    assert observed["kwargs"]["check"] is True
    assert observed["kwargs"]["capture_output"] is True
    assert observed["kwargs"]["capture_output"] is True


def test_mp3_transcoder_derives_metadata_free_mp3_from_m4a(monkeypatch, tmp_path):
    observed = {}
    monkeypatch.setattr(archive.shutil, "which", lambda value: "ffmpeg.exe")

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs

    monkeypatch.setattr(archive.subprocess, "run", fake_run)
    source = tmp_path / "normalized.m4a"
    destination = tmp_path / "audio.mp3"
    archive.ArchiveMP3Transcoder()(source, destination)
    argv = observed["argv"]
    assert argv[argv.index("-i") + 1] == str(source)
    assert argv[argv.index("-c:a") + 1] == "libmp3lame"
    assert argv[argv.index("-b:a") + 1] == "192k"
    assert argv[argv.index("-ar") + 1] == "44100"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-map_metadata") + 1] == "-1"
    assert argv[argv.index("-map_chapters") + 1] == "-1"
    assert argv[argv.index("-f") + 1] == "mp3"
    assert "-vn" in argv
    assert observed["kwargs"]["check"] is True


@pytest.mark.parametrize(
    "audio_format,format_name,codec,profile",
    [
        ("m4a", "mov,mp4,m4a,3gp,3g2,mj2", "aac", "LC"),
        ("mp3", "mp3", "mp3", "unknown"),
    ],
)
def test_archive_ffprobe_verifies_actual_dual_format_profile(
    monkeypatch,
    tmp_path,
    audio_format,
    format_name,
    codec,
    profile,
):
    path = tmp_path / f"audio.{audio_format}"
    path.write_bytes(b"x" * 100)
    observed = {}
    monkeypatch.setattr(archive.shutil, "which", lambda value: "ffprobe.exe")

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": codec,
                            "profile": profile,
                            "sample_rate": "44100",
                            "channels": 2,
                            "bit_rate": "192000",
                        }
                    ],
                    "format": {
                        "format_name": format_name,
                        "duration": "2.0",
                        "size": "100",
                        "bit_rate": "192000",
                    },
                }
            )
        )

    monkeypatch.setattr(archive.subprocess, "run", fake_run)
    probe = archive.ArchiveFFprobeInspector(audio_format)(path)
    assert probe.format_name == audio_format
    assert probe.codec_name == codec
    assert probe.sample_rate_hz == 44100
    assert probe.channels == 2
    assert probe.bitrate_bps == 192000
    assert "a:0" in observed["argv"]


@pytest.mark.parametrize(
    "audio_format,format_name,codec,sample_rate,channels,bitrate,error",
    [
        ("m4a", "mp3", "aac", "44100", 2, "192000", "container"),
        ("m4a", "mov,mp4,m4a", "mp3", "44100", 2, "192000", "codec"),
        ("mp3", "mp3", "mp3", "48000", 2, "192000", "profile"),
        ("mp3", "mp3", "mp3", "44100", 1, "192000", "profile"),
        ("mp3", "mp3", "mp3", "44100", 2, "128000", "bitrate"),
    ],
)
def test_archive_ffprobe_rejects_wrong_actual_profile(
    monkeypatch,
    tmp_path,
    audio_format,
    format_name,
    codec,
    sample_rate,
    channels,
    bitrate,
    error,
):
    path = tmp_path / f"audio.{audio_format}"
    path.write_bytes(b"x" * 100)
    monkeypatch.setattr(archive.shutil, "which", lambda value: "ffprobe.exe")
    monkeypatch.setattr(
        archive.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": codec,
                            "profile": "LC",
                            "sample_rate": sample_rate,
                            "channels": channels,
                            "bit_rate": bitrate,
                        }
                    ],
                    "format": {
                        "format_name": format_name,
                        "duration": "2.0",
                        "size": "100",
                        "bit_rate": bitrate,
                    },
                }
            )
        ),
    )
    with pytest.raises(archive.AudioArchiveTransportError, match=error):
        archive.ArchiveFFprobeInspector(audio_format)(path)


def test_archive_ffprobe_accepts_low_average_aac_bitrate_for_silence(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "silence.m4a"
    path.write_bytes(b"x" * 100)
    monkeypatch.setattr(archive.shutil, "which", lambda value: "ffprobe.exe")
    monkeypatch.setattr(
        archive.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": "aac",
                            "profile": "LC",
                            "sample_rate": "44100",
                            "channels": 2,
                            "bit_rate": "6500",
                        }
                    ],
                    "format": {
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        "duration": "2.0",
                        "size": "100",
                        "bit_rate": "6500",
                    },
                }
            )
        ),
    )

    probe = archive.ArchiveFFprobeInspector("m4a")(path)
    assert probe.bitrate_bps == 6500


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg/FFprobe are required for the offline codec probe",
)
def test_real_offline_dual_codec_outputs_are_valid_and_drop_source_title(tmp_path):
    source = tmp_path / "source.wav"
    m4a = tmp_path / "normalized.m4a"
    mp3 = tmp_path / "derived.mp3"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-metadata",
            "title=secret-source-title",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    archive.ArchiveM4ATranscoder()(source, m4a)
    archive.ArchiveMP3Transcoder()(m4a, mp3)
    m4a_probe = archive.ArchiveFFprobeInspector("m4a")(m4a)
    mp3_probe = archive.ArchiveFFprobeInspector("mp3")(mp3)
    assert (m4a_probe.codec_name, m4a_probe.codec_profile) == ("aac", "LC")
    assert mp3_probe.codec_name == "mp3"
    assert m4a_probe.sample_rate_hz == mp3_probe.sample_rate_hz == 44100
    assert m4a_probe.channels == mp3_probe.channels == 2

    for output in (m4a, mp3):
        details = json.loads(
            subprocess.run(
                [
                    shutil.which("ffprobe"),
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert len(details.get("streams", [])) == 1
        assert details.get("chapters", []) == []
        tags = details.get("format", {}).get("tags", {})
        assert "secret-source-title" not in json.dumps(tags).casefold()


def test_receipt_persists_independent_m4a_and_mp3_durations(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    instance = archive.AudioArchiveTransport(
        sonic_transport=fake,
        mp3_transcoder=SyntheticMP3Transcoder(),
        mp3_inspector=FixedInspector(2.05),
    )

    receipt = run_archive(
        instance,
        [candidate(POST_A)],
        {POST_A: destinations},
    )[0]
    assert receipt.files["m4a"]["duration_seconds"] == 2.0
    assert receipt.files["mp3"]["duration_seconds"] == 2.05


def test_success_promotes_matched_pair_only_after_sonic_temp_cleanup(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    transcoder = SyntheticMP3Transcoder()
    instance = transport_instance(tmp_path, fake, transcoder=transcoder)
    checkpoints = []

    async def checkpoint(receipt):
        assert not fake.live_paths
        assert all(path.is_file() for path in destinations.values())
        assert not list(output.glob(".audio-archive-*.partial"))
        assert not list(output.glob(".audio-archive-*.staged"))
        checkpoints.append(receipt)

    receipts = run_archive(
        instance,
        [candidate(POST_A)],
        {POST_A: destinations},
        expected_account="operator",
        receipt_processor=checkpoint,
    )
    assert receipts == checkpoints
    receipt = receipts[0]
    assert receipt.status == "completed"
    assert receipt.error_code == ""
    assert set(receipt.files) == {"m4a", "mp3"}
    for format_name, destination in destinations.items():
        file_receipt = receipt.files[format_name]
        assert Path(file_receipt["destination_path"]) == destination
        assert file_receipt["byte_count"] == destination.stat().st_size
        assert file_receipt["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
        assert file_receipt["format"] == format_name
    assert receipt.files["m4a"]["mime_type"] == "audio/mp4"
    assert receipt.files["m4a"]["codec"] == "aac-lc"
    assert receipt.files["mp3"]["mime_type"] == "audio/mpeg"
    assert receipt.files["mp3"]["codec"] == "mp3"
    assert receipt.transport_owner_run_id == archive.sonic_owner_run_id(RUN_ID)
    assert receipt.provenance["persistent_audio_retained"] is True
    assert receipt.provenance["source_video_retained"] is False
    assert len(transcoder.calls) == 1
    call = fake.calls[0]
    assert call["run_id"] == archive.sonic_owner_run_id(RUN_ID)
    assert call["expected_account"] == "operator"


def test_dedicated_run_staging_root_is_empty_after_pair_promotion(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    staging = tmp_path / ".staging"
    staging.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    instance = transport_instance(tmp_path, fake, staging_root=staging)
    receipts = run_archive(instance, [candidate(POST_A)], {POST_A: destinations})
    assert receipts[0].status == "completed"
    assert all(path.is_file() for path in destinations.values())
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize("existing_format", ["m4a", "mp3"])
def test_existing_either_destination_rejected_before_transport(tmp_path, existing_format):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    destinations[existing_format].write_bytes(b"existing")
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    instance = transport_instance(tmp_path, fake)
    with pytest.raises(archive.AudioArchiveTransportError, match="already_exists"):
        run_archive(instance, [candidate(POST_A)], {POST_A: destinations})
    assert destinations[existing_format].read_bytes() == b"existing"
    assert not destinations["mp3" if existing_format == "m4a" else "m4a"].exists()
    assert fake.calls == []


def test_destination_mapping_must_exactly_match_frozen_posts_and_formats(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    instance = transport_instance(tmp_path, fake)
    with pytest.raises(archive.AudioArchiveTransportError, match="mapping_mismatch"):
        run_archive(
            instance,
            [candidate(POST_A)],
            {POST_B: destination_pair(output, 1, POST_B)},
        )
    with pytest.raises(archive.AudioArchiveTransportError, match="pair_invalid"):
        run_archive(
            instance,
            [candidate(POST_A)],
            {POST_A: {"m4a": output / f"0001_{POST_A}.m4a"}},
        )
    assert fake.calls == []


def test_racing_half_destination_is_not_overwritten_and_pair_rolls_back(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)

    def create_racing_final(post_id):
        assert post_id == POST_A
        destinations["mp3"].write_bytes(b"external-winner")

    fake = SyntheticSonicTransport(
        tmp_path / "sonic-temp",
        after_cleanup=create_racing_final,
    )
    instance = transport_instance(tmp_path, fake)
    with pytest.raises(archive.AudioArchiveTransportError, match="already_exists"):
        run_archive(instance, [candidate(POST_A)], {POST_A: destinations})
    assert destinations["mp3"].read_bytes() == b"external-winner"
    assert not destinations["m4a"].exists()
    assert not list(output.glob(".audio-archive-*.partial"))
    assert not list(output.glob(".audio-archive-*.staged"))


def test_unavailable_item_yields_neither_format_and_continues(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    first = destination_pair(output, 1, POST_A)
    second = destination_pair(output, 2, POST_B)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp", unavailable={POST_A})
    instance = transport_instance(tmp_path, fake)
    receipts = run_archive(
        instance,
        [candidate(POST_A), candidate(POST_B)],
        {POST_A: first, POST_B: second},
    )
    assert [receipt.post_id for receipt in receipts] == [POST_A, POST_B]
    assert [receipt.status for receipt in receipts] == ["unavailable", "completed"]
    assert receipts[0].error_code == "synthetic_unavailable"
    assert receipts[0].files == {}
    assert not any(path.exists() for path in first.values())
    assert all(path.is_file() for path in second.values())


@pytest.mark.parametrize("failure_mode", ["raise", "omit"])
def test_mp3_derivation_failure_leaves_no_half_pair_or_checkpoint(tmp_path, failure_mode):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    transcoder = SyntheticMP3Transcoder(
        fail=failure_mode == "raise",
        omit_output=failure_mode == "omit",
    )
    instance = transport_instance(tmp_path, fake, transcoder=transcoder)
    checkpoints = []
    with pytest.raises(archive.AudioArchiveTransportError):
        run_archive(
            instance,
            [candidate(POST_A)],
            {POST_A: destinations},
            receipt_processor=checkpoints.append,
        )
    assert checkpoints == []
    assert not any(path.exists() for path in destinations.values())
    assert not list(output.glob(".audio-archive-*.partial"))
    assert not list(output.glob(".audio-archive-*.staged"))


def test_source_receipt_hash_mismatch_fails_without_pair_or_stage_residue(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    destinations = destination_pair(output, 1, POST_A)
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp", corrupt_receipt={POST_A})
    instance = transport_instance(tmp_path, fake)
    with pytest.raises(archive.AudioArchiveTransportError, match="hash_mismatch"):
        run_archive(instance, [candidate(POST_A)], {POST_A: destinations})
    assert not any(path.exists() for path in destinations.values())
    assert not list(output.glob(".audio-archive-*.partial"))
    assert not list(output.glob(".audio-archive-*.staged"))


def test_symlink_destination_parent_is_rejected_before_transport(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available to this test user")
    fake = SyntheticSonicTransport(tmp_path / "sonic-temp")
    instance = transport_instance(tmp_path, fake)
    with pytest.raises(archive.AudioArchiveTransportError, match="link_or_reparse"):
        run_archive(
            instance,
            [candidate(POST_A)],
            {POST_A: destination_pair(linked_parent, 1, POST_A)},
        )
    assert fake.calls == []
