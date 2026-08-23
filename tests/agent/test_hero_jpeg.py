"""Regression tests for AI hero JPEG materialization."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from agent.content.images.ai_hero_image import _to_jpeg


def _save_varied_png(path: Path) -> None:
    image = Image.effect_noise((400, 300), 64).convert("RGB")
    image.save(path, "PNG")


def test_png_becomes_jpeg_and_original_is_removed(tmp_path):
    source = tmp_path / "hero_ai.png"
    _save_varied_png(source)

    result = _to_jpeg(source)

    assert result == tmp_path / "hero_ai.jpg"
    assert result.is_file()
    assert not source.exists()


def test_jpeg_is_smaller_than_source_png(tmp_path):
    source = tmp_path / "hero_ai.png"
    _save_varied_png(source)
    source_size = source.stat().st_size

    result = _to_jpeg(source)

    assert result.stat().st_size < source_size


def test_existing_jpeg_is_returned_unchanged(tmp_path):
    source = tmp_path / "hero_ai.jpg"
    Image.new("RGB", (400, 300), (200, 180, 220)).save(source, "JPEG", quality=85)
    original_bytes = source.read_bytes()

    result = _to_jpeg(source)

    assert result == source
    assert source.read_bytes() == original_bytes


def test_transparent_png_becomes_rgb_jpeg_with_white_background(tmp_path):
    source = tmp_path / "hero_ai.png"
    image = Image.new("RGBA", (400, 300), (0, 120, 220, 0))
    ImageDraw.Draw(image).rectangle((0, 0, 199, 299), fill=(0, 120, 220, 255))
    image.save(source, "PNG")

    result = _to_jpeg(source)

    assert result.suffix == ".jpg"
    assert not source.exists()
    with Image.open(result) as converted:
        assert converted.mode == "RGB"


def test_broken_image_returns_original_path(tmp_path):
    source = tmp_path / "hero_ai.png"
    source.write_bytes(b"not a valid image")

    result = _to_jpeg(source)

    assert result == source
    assert source.read_bytes() == b"not a valid image"
    assert not (tmp_path / "hero_ai.jpg").exists()
