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


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_size: int,
    step: int = 4,
):
    """Return the largest font (down to ~max_size/3) at which ``text`` fits
    within ``max_width``. Used for song titles so they shrink instead of
    getting an ellipsis. If even the floor size overflows, return that anyway
    — it'll clip, but that's better than the title silently disappearing."""
    min_size = max(40, max_size // 3)
    if not text:
        return _load_font(max_size)
    size = max_size
    while size > min_size:
        font = _load_font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= step
    return _load_font(min_size)


def _parse_background(value) -> tuple[str, tuple[int, int, int] | None]:
    """Parse a display.background config value.

    Returns (mode, color) where mode is ``"auto"`` (use average album color)
    or ``"color"`` (use the returned RGB tuple). Accepted forms:
      - ``"auto"``                — average color of the album image
      - ``"black"`` / ``"white"`` — convenience aliases
      - ``"#rrggbb"``             — explicit hex color
    """
    if not isinstance(value, str):
        raise ValueError(
            f"display.background must be a string, got {type(value).__name__}"
        )
    v = value.strip().lower()
    if v == "auto":
        return ("auto", None)
    if v == "black":
        return ("color", (0, 0, 0))
    if v == "white":
        return ("color", (255, 255, 255))
    if v.startswith("#") and len(v) == 7:
        try:
            return (
                "color",
                (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)),
            )
        except ValueError:
            pass
    raise ValueError(
        f"display.background must be 'auto', 'black', 'white', or '#rrggbb' "
        f"(got {value!r})"
    )


class Composer:
    """Compose an album-art + track-metadata image sized for the Frame TV."""

    def __init__(
        self,
        orientation: str,
        output_dir: pathlib.Path,
        background: str = "black",
    ):
        orientation = orientation.lower()
        if orientation not in ("landscape", "portrait"):
            raise ValueError(
                f"orientation must be 'landscape' or 'portrait', got {orientation!r}"
            )
        self.orientation = orientation
        self.output_dir = pathlib.Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.background_mode, self.background_color = _parse_background(background)
        # Stable string mixed into the composed-image cache key so that
        # changing display.background invalidates old files automatically.
        self.background_key = (
            "auto"
            if self.background_mode == "auto"
            else "%02x%02x%02x" % self.background_color
        )

    def output_path(self, track: TrackInfo) -> pathlib.Path:
        # Composed images are per-track (artist + title) because the image
        # bakes in the song title — two songs off the same album need two
        # different composed files. Background mode is mixed in so toggling
        # it produces fresh files instead of serving stale ones.
        key_input = f"{track.display_key}|bg={self.background_key}"
        key = hashlib.sha256(key_input.encode("utf-8")).hexdigest()[:16]
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

        cover, bg = self._prepare_cover(cover_path, 1920)

        if self.orientation == "landscape":
            img = self._compose_landscape(track, cover, bg)
        else:
            img = self._compose_portrait(track, cover, bg)

        img.save(out, format="JPEG", quality=90)
        log.info("Composed image saved: %s", out)
        return out

    # ----- layouts -----

    def _compose_landscape(
        self, track: TrackInfo, cover: Image.Image, bg: tuple[int, int, int]
    ) -> Image.Image:
        w, h = LANDSCAPE
        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)

        cover_size = cover.width
        cover_x = 120
        cover_y = (h - cover_size) // 2
        img.paste(cover, (cover_x, cover_y))

        # Text block: right of the cover, vertically centered.
        text_x = cover_x + cover_size + 160
        text_right = w - 160
        text_width = text_right - text_x

        title = track.title or ""
        title_font = _fit_font(draw, title, text_width, max_size=160)
        artist_font = _load_font(100)
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

    def _compose_portrait(
        self, track: TrackInfo, cover: Image.Image, bg: tuple[int, int, int]
    ) -> Image.Image:
        w, h = PORTRAIT
        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)

        cover_size = cover.width
        cover_x = (w - cover_size) // 2
        cover_y = 480
        img.paste(cover, (cover_x, cover_y))

        center_x = w // 2
        text_width = w - 240
        text_top = cover_y + cover_size + 240

        title = track.title or ""
        title_font = _fit_font(draw, title, text_width, max_size=180)
        artist_font = _load_font(120)
        artist = _truncate(draw, track.artist or "", artist_font, text_width)

        t_bbox = draw.textbbox((0, 0), title, font=title_font)
        title_h = t_bbox[3] - t_bbox[1]
        gap = 80

        draw.text((center_x, text_top), title, font=title_font,
                  fill=(255, 255, 255), anchor="mt")
        draw.text((center_x, text_top + title_h + gap), artist, font=artist_font,
                  fill=(200, 200, 200), anchor="mt")
        return img

    def _prepare_cover(
        self, cover_path: pathlib.Path, size: int
    ) -> tuple[Image.Image, tuple[int, int, int]]:
        """Load the album image, resize to ``size``, and return it together
        with the background color to use behind it. Non-square covers are
        padded to a square with the same background so the seams disappear."""
        with Image.open(cover_path) as src:
            cover = src.convert("RGB")
        cover.thumbnail((size, size), Image.Resampling.LANCZOS)

        bg = self._background_for(cover)

        if cover.size != (size, size):
            padded = Image.new("RGB", (size, size), bg)
            off = ((size - cover.width) // 2, (size - cover.height) // 2)
            padded.paste(cover, off)
            cover = padded
        return cover, bg

    def _background_for(self, cover: Image.Image) -> tuple[int, int, int]:
        if self.background_mode == "auto":
            # Resizing to 1x1 with LANCZOS averages all pixels into a single
            # color — cheaper and more accurate than walking the histogram.
            small = cover.resize((1, 1), Image.Resampling.LANCZOS)
            r, g, b = small.getpixel((0, 0))[:3]
            return (r, g, b)
        return self.background_color  # type: ignore[return-value]
