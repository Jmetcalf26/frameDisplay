import pathlib

import pytest
from PIL import Image

from backend.composer import LANDSCAPE, PORTRAIT, Composer
from backend.models import TrackInfo


def _fake_cover(path: pathlib.Path, size: int = 640) -> pathlib.Path:
    """Write a tiny square PNG to path."""
    img = Image.new("RGB", (size, size), (128, 64, 200))
    img.save(path, format="PNG")
    return path


@pytest.fixture
def track():
    return TrackInfo(title="Blue in Green", artist="Miles Davis", album="Kind of Blue")


def test_landscape_dimensions(tmp_path, track):
    cover = _fake_cover(tmp_path / "cover.png")
    composer = Composer(orientation="landscape", output_dir=tmp_path / "out")

    out = composer.compose(track, cover)

    assert out.exists()
    assert out.suffix == ".jpg"
    with Image.open(out) as img:
        assert img.size == LANDSCAPE


def test_portrait_dimensions(tmp_path, track):
    cover = _fake_cover(tmp_path / "cover.png")
    composer = Composer(orientation="portrait", output_dir=tmp_path / "out")

    out = composer.compose(track, cover)

    with Image.open(out) as img:
        assert img.size == PORTRAIT


def test_cache_hit_skips_rebuild(tmp_path, track):
    cover = _fake_cover(tmp_path / "cover.png")
    composer = Composer(orientation="landscape", output_dir=tmp_path / "out")

    first = composer.compose(track, cover)
    first_mtime = first.stat().st_mtime_ns

    # Second call should return the existing file without rewriting.
    second = composer.compose(track, cover)
    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_output_path_uses_track_key_and_orientation(tmp_path, track):
    """Composed images are per-track (artist+title) since the image bakes in
    the song title. Two songs off the same album must get different files."""
    composer = Composer(orientation="landscape", output_dir=tmp_path / "out")
    p = composer.output_path(track)
    assert p.parent == tmp_path / "out"
    assert p.name.endswith("-landscape.jpg")

    p2 = Composer(orientation="portrait", output_dir=tmp_path / "out").output_path(track)
    assert p2.name.endswith("-portrait.jpg")
    # Same track key, different orientation suffix
    assert p.stem.split("-")[0] == p2.stem.split("-")[0]

    # Different song on the SAME album must produce a different filename
    other = TrackInfo(title="So What", artist=track.artist, album=track.album)
    p_other = composer.output_path(other)
    assert p_other.stem != p.stem


def test_invalid_orientation_raises(tmp_path):
    with pytest.raises(ValueError):
        Composer(orientation="diagonal", output_dir=tmp_path / "out")


def test_non_square_cover_is_padded(tmp_path, track):
    # Tall cover (4:5-ish)
    wide_cover = tmp_path / "wide.png"
    Image.new("RGB", (800, 1000), (10, 20, 30)).save(wide_cover, format="PNG")

    composer = Composer(orientation="landscape", output_dir=tmp_path / "out")
    out = composer.compose(track, wide_cover)

    with Image.open(out) as img:
        assert img.size == LANDSCAPE


def test_long_text_does_not_overflow(tmp_path):
    cover = _fake_cover(tmp_path / "cover.png")
    track = TrackInfo(
        title="T",
        artist="A Very Long Artist Name That Should Definitely Exceed The Text Box Width And Force Truncation",
        album="And An Equally Absurdly Long Album Name That Would Otherwise Spill Off The Canvas Into Nowhere",
    )
    composer = Composer(orientation="landscape", output_dir=tmp_path / "out")

    # Should not raise — truncation handles overflow.
    out = composer.compose(track, cover)
    assert out.exists()
