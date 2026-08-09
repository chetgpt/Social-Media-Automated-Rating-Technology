import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import tiktok_showcase_media as media


def _jpeg(width=1080, height=1920):
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + int(height).to_bytes(2, "big")
        + int(width).to_bytes(2, "big")
        + b"\x03"
        + b"\x01\x11\x00"
        + b"\x02\x11\x00"
        + b"\x03\x11\x00"
        + b"\xff\xd9"
    )


def test_stage_comment_screenshot_is_content_addressed(tmp_path):
    source_root = tmp_path / "captures"
    source_root.mkdir()
    source = source_root / "comment.png"
    source.write_bytes(b"exact-comment-png")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    public = tmp_path / "public"
    expected_jpeg = _jpeg()

    def fake_runner(command, **kwargs):
        assert kwargs["timeout"] == 60
        Path(command[-1]).write_bytes(expected_jpeg)
        return SimpleNamespace(returncode=0, stderr="")

    result = media.stage_comment_screenshot(
        source,
        expected_source_hash=source_hash,
        allowed_source_root=source_root,
        public_directory=public,
        public_base_url="https://media.example.com/tiktok/showcases/",
        ffmpeg_path=__file__,
        runner=fake_runner,
    )

    expected_hash = hashlib.sha256(expected_jpeg).hexdigest()
    assert result["media_sha256"] == expected_hash
    assert result["media_path"] == str(public / f"{expected_hash}.jpg")
    assert result["media_url"].endswith(f"/{expected_hash}.jpg")
    assert result["width"] == 1080
    assert result["height"] == 1920


def test_stage_rejects_source_tampering_and_path_escape(tmp_path):
    root = tmp_path / "captures"
    root.mkdir()
    source = root / "comment.png"
    source.write_bytes(b"changed")

    with pytest.raises(media.ShowcaseMediaError, match="hash"):
        media.stage_comment_screenshot(
            source,
            expected_source_hash="0" * 64,
            allowed_source_root=root,
            public_directory=tmp_path / "public",
            public_base_url="https://media.example.com/showcases/",
            ffmpeg_path=__file__,
        )

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    with pytest.raises(media.ShowcaseMediaError, match="outside"):
        media.stage_comment_screenshot(
            outside,
            expected_source_hash=hashlib.sha256(outside.read_bytes()).hexdigest(),
            allowed_source_root=root,
            public_directory=tmp_path / "public",
            public_base_url="https://media.example.com/showcases/",
            ffmpeg_path=__file__,
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://media.example.com/showcases/",
        "https://127.0.0.1/showcases/",
        "https://localhost/showcases/",
        "https://media.example.com:8443/showcases/",
        "https://user:secret@media.example.com/showcases/",
        "https://media.example.com/showcases/?token=secret",
    ),
)
def test_public_media_prefix_must_be_stable_https_domain(url):
    with pytest.raises(media.ShowcaseMediaError):
        media.validate_public_base_url(url)


def test_verify_hosted_media_rejects_redirect_and_hash_drift():
    payload = _jpeg(800, 1200)

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg; charset=binary"}

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield payload

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    verified = media.verify_hosted_media(
        "https://media.example.com/showcases/comment.jpg",
        expected_hash=hashlib.sha256(payload).hexdigest(),
        session=Session(),
    )
    assert verified["media_bytes"] == len(payload)

    with pytest.raises(media.ShowcaseMediaError, match="hash"):
        media.verify_hosted_media(
            "https://media.example.com/showcases/comment.jpg",
            expected_hash="f" * 64,
            session=Session(),
        )

    class RedirectSession:
        def get(self, url, **kwargs):
            return SimpleNamespace(status_code=302, headers={})

    with pytest.raises(media.ShowcaseMediaError, match="HTTP 302"):
        media.verify_hosted_media(
            "https://media.example.com/showcases/comment.jpg",
            expected_hash="f" * 64,
            session=RedirectSession(),
        )
