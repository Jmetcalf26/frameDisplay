import hashlib
import logging
import pathlib

from PIL import Image, ImageDraw, ImageFont

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

# Both orientations stay at 16:9 — the Frame is physically rotated for portrait,
# but the panel pixels stay 16:9 either way.
LANDSCAPE = (3840, 2160)
PORTRAIT = (2160, 3840)

# Common truetype font locations. First hit wins; if none exist we fall back
# to Pillow's tiny bitmap default (ugly but keeps the composer working).
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def _load_font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    log.warning("No truetype font found; falling back to default bitmap font")
    return ImageFont.load_default()


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "\u2026"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


class Composer:
    """Compose an album-art + track-metadata image sized for the Frame TV."""

    def __init__(self, orientation: str, output_dir: pathlib.Path):
        orientation = orientation.lower()
        if orientation not in ("landscape", "portrait"):
            raise ValueError(
                f"orientation must be 'landscape' or 'portrait', got {orientation!r}"
            )
        self.orientation = orientation
        self.output_dir = pathlib.Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def output_path(self, track: TrackInfo) -> pathlib.Path:
        # Composed images are per-track (artist + title) because the image
        # bakes in the song title — two songs off the same album need two
        # different composed files.
        key = hashlib.sha256(track.display_key.encode("utf-8")).hexdigest()[:16]
        return self.output_dir / f"{key}-{self.orientation}.jpg"

    def compose(self, track: TrackInfo, cover_path: pathlib.Path) -> pathlib.Path:
        """Compose the track image and return its on-disk path.

        If the output file already exists we reuse it — delete it (or the
        whole composed_dir) to force a rebuild.
        """
        out = self.output_path(track)
        if out.exists():
            log.info("Composed image cache hit: %s", out.name)
            return out

        if self.orientation == "landscape":
            img = self._compose_landscape(track, cover_path)
        else:
            img = self._compose_portrait(track, cover_path)

        img.save(out, format="JPEG", quality=90)
        log.info("Composed image saved: %s", out)
        return out

    # ----- layouts -----

    def _compose_landscape(self, track: TrackInfo, cover_path: pathlib.Path) -> Image.Image:
        w, h = LANDSCAPE
        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        cover_size = 1920
        cover = self._load_cover(cover_path, cover_size)
        cover_x = 120
        cover_y = (h - cover_size) // 2
        img.paste(cover, (cover_x, cover_y))

        # Text block: right of the cover, vertically centered.
        text_x = cover_x + cover_size + 160
        text_right = w - 160
        text_width = text_right - text_x

        title_font = _load_font(160)
        artist_font = _load_font(100)

        title = _truncate(draw, track.title or "", title_font, text_width)
        artist = _truncate(draw, track.artist or "", artist_font, text_width)

        t_bbox = draw.textbbox((0, 0), title, font=title_font)
        a_bbox = draw.textbbox((0, 0), artist, font=artist_font)
        title_h = t_bbox[3] - t_bbox[1]
        artist_h = a_bbox[3] - a_bbox[1]
        gap = 60
        total_h = title_h + gap + artist_h
        top = (h - total_h) // 2

        draw.text((text_x, top), title, font=title_font, fill=(255, 255, 255))
        draw.text((text_x, top + title_h + gap), artist, font=artist_font, fill=(200, 200, 200))
        return img

    def _compose_portrait(self, track: TrackInfo, cover_path: pathlib.Path) -> Image.Image:
        w, h = PORTRAIT
        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        cover_size = 1920
        cover = self._load_cover(cover_path, cover_size)
        cover_x = (w - cover_size) // 2
        cover_y = 480
        img.paste(cover, (cover_x, cover_y))

        center_x = w // 2
        text_width = w - 240
        text_top = cover_y + cover_size + 240

        title_font = _load_font(180)
        artist_font = _load_font(120)

        title = _truncate(draw, track.title or "", title_font, text_width)
        artist = _truncate(draw, track.artist or "", artist_font, text_width)

        t_bbox = draw.textbbox((0, 0), title, font=title_font)
        title_h = t_bbox[3] - t_bbox[1]
        gap = 80

        draw.text((center_x, text_top), title, font=title_font,
                  fill=(255, 255, 255), anchor="mt")
        draw.text((center_x, text_top + title_h + gap), artist, font=artist_font,
                  fill=(200, 200, 200), anchor="mt")
        return img

    def _load_cover(self, cover_path: pathlib.Path, size: int) -> Image.Image:
        with Image.open(cover_path) as src:
            cover = src.convert("RGB")
        cover.thumbnail((size, size), Image.Resampling.LANCZOS)
        # If the source isn't perfectly square, pad to a square on black.
        if cover.size != (size, size):
            padded = Image.new("RGB", (size, size), (0, 0, 0))
            off = ((size - cover.width) // 2, (size - cover.height) // 2)
            padded.paste(cover, off)
            cover = padded
        return cover
