"""Regression tests for topic-aware AI hero cache reuse."""

from pathlib import Path

from agent.content.images import ai_hero_image



def _prepare_generation(monkeypatch):
    provider_calls = []

    class _FakeProvider:
        def generate(self, prompt: str, *, aspect_ratio: str) -> dict:
            provider_calls.append((prompt, aspect_ratio))
            return {"success": True, "image": "fake://hero-image"}

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "_resolve_provider", lambda: _FakeProvider())
    monkeypatch.setattr(ai_hero_image, "build_hero_prompt", lambda **kwargs: "fake prompt")

    def fake_materialize(
        _image_ref: str, *, dest_dir: Path, enforce_landscape: bool = True
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved = dest_dir / "hero_ai.jpg"
        saved.write_bytes(b"")
        return saved

    monkeypatch.setattr(ai_hero_image, "_materialize_provider_image", fake_materialize)
    return provider_calls



def _generate(tmp_path, monkeypatch, *, style_seed: str = "topic-a"):
    _prepare_generation(monkeypatch)
    return ai_hero_image._try_generate(
        out_dir=tmp_path,
        category_id="health",
        category_name="건강",
        topic_title="주제",
        blog_content="# 주제\n\n본문",
        style_seed=style_seed,
    )



def test_same_seed_reuses_existing_image(tmp_path):
    hero = tmp_path / "hero_ai.jpg"
    hero.write_bytes(b"existing image")
    (tmp_path / "hero_ai.seed").write_text("topic-a", encoding="utf-8")

    result = ai_hero_image._find_cached_ai_hero(tmp_path, style_seed="topic-a")

    assert result == hero



def test_different_seed_does_not_reuse_existing_image(tmp_path):
    hero = tmp_path / "hero_ai.jpg"
    hero.write_bytes(b"existing image")
    (tmp_path / "hero_ai.seed").write_text("topic-a", encoding="utf-8")

    result = ai_hero_image._find_cached_ai_hero(tmp_path, style_seed="topic-b")

    assert result is None



def test_missing_seed_does_not_reuse_when_seed_is_requested(tmp_path):
    hero = tmp_path / "hero_ai.jpg"
    hero.write_bytes(b"existing image")

    result = ai_hero_image._find_cached_ai_hero(tmp_path, style_seed="topic-a")

    assert result is None



def test_omitted_seed_keeps_existing_cache_behavior(tmp_path):
    hero = tmp_path / "hero_ai.jpg"
    hero.write_bytes(b"existing image")
    (tmp_path / "hero_ai.seed").write_text("different-topic", encoding="utf-8")

    result = ai_hero_image._find_cached_ai_hero(tmp_path)

    assert result == hero



def test_unreadable_seed_file_does_not_raise(monkeypatch, tmp_path):
    def fail_read(*args, **kwargs):
        raise OSError("cannot read seed")

    monkeypatch.setattr(ai_hero_image.Path, "read_text", fail_read)

    assert ai_hero_image._read_hero_seed(tmp_path) == ""



def test_generated_image_writes_its_seed(tmp_path, monkeypatch):
    _generate(tmp_path, monkeypatch, style_seed="topic-a")

    assert (tmp_path / "hero_ai.jpg").is_file()
    assert (tmp_path / "hero_ai.seed").read_text(encoding="utf-8") == "topic-a"



def test_seed_write_failure_does_not_hide_generated_image(tmp_path, monkeypatch):
    _generate(tmp_path, monkeypatch, style_seed="topic-a")

    def fail_write(*args, **kwargs):
        raise OSError("cannot write seed")

    monkeypatch.setattr(ai_hero_image.Path, "write_text", fail_write)
    result = _generate(tmp_path, monkeypatch, style_seed="topic-b")

    assert result is not None
    assert (tmp_path / "hero_ai.jpg").is_file()



def test_seed_file_is_not_an_image_cache_candidate(tmp_path):
    (tmp_path / "hero_ai.seed").write_text("topic-a", encoding="utf-8")

    assert ai_hero_image._find_cached_ai_hero(tmp_path) is None
