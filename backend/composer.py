import hashlib
import logging
import pathlib

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

# Both orientations stay at 16:9 — the Frame is physically rotated for portrait,
# but the panel pixels stay 16:9 either way.
LANDSCAPE = (3840, 2160)
PORTRAIT = (2160, 3840)

# Truetype font candidates per family. First hit wins on each system; if
# none exist we fall back to Pillow's tiny bitmap default (ugly but keeps
# the composer working). Bold variants are preferred so the title reads at
# TV-viewing distance.
_FONT_FAMILIES: dict[str, list[str]] = {
    "sans": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "serif": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/Library/Fonts/Georgia.ttf",
        "/Library/Fonts/Times New Roman.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ],
    "mono": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/Library/Fonts/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ],
}

# Genre-mood font candidates. Same first-hit-wins convention. These aim for
# a distinct typographic feel per mood — the differences are subtle on Linux
# (serif vs sans vs mono) but dramatic on macOS where display/script fonts
# like Marker Felt, Didot, Futura, and American Typewriter are available.
_GENRE_MOOD_FONTS: dict[str, list[str]] = {
    "elegant": [
        # Jazz, Classical, Soul, Blues — refined serif
        "/System/Library/Fonts/Supplemental/Didot.ttc",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/usr/share/fonts/truetype/fonts-yrsa-rasa/Yrsa-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    ],
    "gritty": [
        # Punk, Metal, Grunge, Hardcore — raw / rough
        "/System/Library/Fonts/Supplemental/Marker Felt.ttc",
        "/System/Library/Fonts/Supplemental/Chalkduster.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    ],
    "modern": [
        # Electronic, Techno, Ambient — clean / minimal
        "/System/Library/Fonts/Supplemental/Futura.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-L.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ],
    "heavy": [
        # Hip Hop, Rap, Trap — bold / impactful
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ],
    "traditional": [
        # Country, Folk, Bluegrass — warm slab / typewriter
        "/System/Library/Fonts/Supplemental/American Typewriter.ttc",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ],
}

# Map Discogs genre/style strings (lowercased) to mood keys.
# Styles (more specific) are checked before genres so e.g. a "Rock" album
# with style "Punk" resolves to "gritty" rather than falling through.
_GENRE_MOOD_MAP: dict[str, str] = {
    # --- Discogs top-level genres ---
    "jazz": "elegant",
    "classical": "elegant",
    "blues": "elegant",
    "funk / soul": "elegant",
    "latin": "elegant",
    "electronic": "modern",
    "hip hop": "heavy",
    "folk, world, & country": "traditional",
    "reggae": "traditional",
    # "Rock" and "Pop" intentionally omitted — too broad to pick a mood.

    # --- Discogs styles (granular) ---
    # elegant
    "soul": "elegant",
    "neo soul": "elegant",
    "r&b": "elegant",
    "rhythm & blues": "elegant",
    "swing": "elegant",
    "bossa nova": "elegant",
    "big band": "elegant",
    "contemporary jazz": "elegant",
    "smooth jazz": "elegant",
    "free jazz": "elegant",
    "hard bop": "elegant",
    "cool jazz": "elegant",
    "fusion": "elegant",
    "opera": "elegant",
    "baroque": "elegant",
    "romantic": "elegant",
    "modern classical": "elegant",
    # gritty
    "punk": "gritty",
    "punk rock": "gritty",
    "post-punk": "gritty",
    "hardcore": "gritty",
    "grunge": "gritty",
    "metal": "gritty",
    "heavy metal": "gritty",
    "death metal": "gritty",
    "black metal": "gritty",
    "thrash metal": "gritty",
    "doom metal": "gritty",
    "sludge metal": "gritty",
    "stoner rock": "gritty",
    "industrial": "gritty",
    "noise": "gritty",
    "noise rock": "gritty",
    "grindcore": "gritty",
    "metalcore": "gritty",
    "mathcore": "gritty",
    "crust": "gritty",
    "oi": "gritty",
    # modern
    "house": "modern",
    "deep house": "modern",
    "tech house": "modern",
    "techno": "modern",
    "ambient": "modern",
    "trance": "modern",
    "drum n bass": "modern",
    "dubstep": "modern",
    "idm": "modern",
    "synth-pop": "modern",
    "electro": "modern",
    "minimal": "modern",
    "downtempo": "modern",
    "trip hop": "modern",
    "future bass": "modern",
    "vaporwave": "modern",
    "synthwave": "modern",
    "new wave": "modern",
    # heavy
    "trap": "heavy",
    "gangsta": "heavy",
    "grime": "heavy",
    "boom bap": "heavy",
    "conscious": "heavy",
    "g-funk": "heavy",
    "rap": "heavy",
    "crunk": "heavy",
    "dirty south": "heavy",
    # traditional
    "country": "traditional",
    "folk": "traditional",
    "folk rock": "traditional",
    "bluegrass": "traditional",
    "americana": "traditional",
    "country rock": "traditional",
    "singer-songwriter": "traditional",
}


def _genre_to_mood(genre: str | None, style: str | None) -> str | None:
    """Map Discogs genre/style to a font mood.

    Styles are checked first (more specific), then genres. First match
    from the comma-separated list wins. Returns ``None`` when nothing
    matches so the caller falls back to the configured ``display.font``.
    """
    for source in (style, genre):
        if not source:
            continue
        for part in source.split(","):
            key = part.strip().lower()
            if key in _GENRE_MOOD_MAP:
                return _GENRE_MOOD_MAP[key]
    return None


def _validate_font_family(family: str) -> str:
    f = family.strip().lower()
    if f not in _FONT_FAMILIES:
        raise ValueError(
            f"display.font must be one of {sorted(_FONT_FAMILIES)} (got {family!r})"
        )
    return f


def _load_font(size: int, family: str = "sans"):
    paths = (
        _GENRE_MOOD_FONTS.get(family)
        or _FONT_FAMILIES.get(family, _FONT_FAMILIES["sans"])
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    log.warning(
        "No truetype font found for family %r; falling back to default bitmap font",
        family,
    )
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
    family: str = "sans",
    step: int = 4,
):
    """Return the largest font (down to ~max_size/3) at which ``text`` fits
    within ``max_width``. Used for song titles so they shrink instead of
    getting an ellipsis. If even the floor size overflows, return that anyway
    — it'll clip, but that's better than the title silently disappearing."""
    min_size = max(40, max_size // 3)
    if not text:
        return _load_font(max_size, family)
    size = max_size
    while size > min_size:
        font = _load_font(size, family)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= step
    return _load_font(min_size, family)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance for an sRGB color (0..1)."""

    def lin(c: int) -> float:
        x = c / 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _text_colors_for(
    bg: tuple[int, int, int],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Pick (title, artist) text colors that contrast with ``bg``.

    Restricted to a black/white pair (with a slightly muted secondary) so we
    never end up with a clashy tinted title — just legible against whatever
    background mode is in use. Threshold is the WCAG crossover where black
    and white have equal contrast against ``bg``.
    """
    if _relative_luminance(bg) > 0.179:
        # Light background → dark text.
        return ((0, 0, 0), (60, 60, 60))
    # Dark background → light text (matches the original styling).
    return ((255, 255, 255), (200, 200, 200))


def _parse_background(value) -> tuple[str, tuple[int, int, int] | None]:
    """Parse a display.background config value.

    Returns (mode, color) where mode is one of ``"auto"`` (average color of
    the whole album image), ``"corners"`` (average of the four corner
    pixels), ``"mode"`` (most common color in the album image),
    ``"blur"`` (a heavily blurred, canvas-filling copy of the album art),
    or ``"color"`` (use the returned RGB tuple). Accepted forms:
      - ``"auto"``                — average color of the album image
      - ``"corners"``             — average of the four corner pixels
      - ``"mode"``                — most common (dominant) color
      - ``"blur"``                — blurred album art fills the canvas
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
    if v == "corners":
        return ("corners", None)
    if v == "mode":
        return ("mode", None)
    if v == "blur":
        return ("blur", None)
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
        f"display.background must be 'auto', 'corners', 'mode', 'blur', "
        f"'black', 'white', or '#rrggbb' (got {value!r})"
    )


class Composer:
    """Compose an album-art + track-metadata image sized for the Frame TV."""

    def __init__(
        self,
        orientation: str,
        output_dir: pathlib.Path,
        background: str = "black",
        font: str = "sans",
        genre_font: bool = False,
        layout: str = "standard",
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
        if self.background_mode == "color":
            self.background_key = "%02x%02x%02x" % self.background_color
        else:
            self.background_key = self.background_mode  # "auto", "corners", "blur"

        self.font_family = _validate_font_family(font)
        self.genre_font = genre_font

        layout = (layout or "standard").lower()
        if layout not in ("standard", "minimal"):
            raise ValueError(
                f"display.layout must be 'standard' or 'minimal' (got {layout!r})"
            )
        self.layout = layout

    def _resolve_font(self, track: TrackInfo) -> str:
        """Return the font family key to use for this track.

        When ``genre_font`` is enabled, Discogs style/genre data is checked
        against the mood map. If nothing matches (or genre_font is off),
        falls back to the configured ``display.font``.
        """
        if self.genre_font:
            mood = _genre_to_mood(track.genre, track.style)
            if mood:
                return mood
        return self.font_family

    def output_path(self, track: TrackInfo) -> pathlib.Path:
        # Composed images are per-track (artist + title) because the image
        # bakes in the song title — two songs off the same album need two
        # different composed files. Background mode and resolved font are
        # mixed in so toggling them produces fresh files instead of serving
        # stale ones.
        font_key = self._resolve_font(track)
        # Font doesn't affect the minimal layout (no text), so omit it from
        # the key there — keeps cached minimal files stable across font tweaks.
        font_part = "none" if self.layout == "minimal" else font_key
        key_input = (
            f"{track.display_key}|bg={self.background_key}"
            f"|font={font_part}|layout={self.layout}"
        )
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

        font_key = self._resolve_font(track)
        if self.genre_font and font_key != self.font_family:
            log.info(
                "Genre font: %s/%s -> %s mood",
                track.genre or "?",
                track.style or "?",
                font_key,
            )

        # The minimal layout gives up the side-by-side text column, so the
        # cover can grow to fill more of the canvas.
        cover_size = 2000 if self.layout == "minimal" else 1920
        cover, cover_unpadded, bg = self._prepare_cover(cover_path, cover_size)

        if self.layout == "minimal":
            img = self._compose_minimal(cover, cover_unpadded, bg)
        elif self.orientation == "landscape":
            img = self._compose_landscape(track, cover, cover_unpadded, bg, font_key)
        else:
            img = self._compose_portrait(track, cover, cover_unpadded, bg, font_key)

        img.save(out, format="JPEG", quality=90)
        log.info("Composed image saved: %s", out)
        return out

    # ----- layouts -----

    def _compose_landscape(
        self,
        track: TrackInfo,
        cover: Image.Image,
        cover_unpadded: Image.Image,
        bg: tuple[int, int, int],
        font_key: str,
    ) -> Image.Image:
        w, h = LANDSCAPE
        img = self._make_canvas((w, h), cover_unpadded, bg)
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
        title_font = _fit_font(draw, title, text_width, max_size=160, family=font_key)
        artist_font = _load_font(100, font_key)
        artist = _truncate(draw, track.artist or "", artist_font, text_width)

        title_color, artist_color = _text_colors_for(bg)

        t_bbox = draw.textbbox((0, 0), title, font=title_font)
        a_bbox = draw.textbbox((0, 0), artist, font=artist_font)
        title_h = t_bbox[3] - t_bbox[1]
        artist_h = a_bbox[3] - a_bbox[1]
        gap = 60
        total_h = title_h + gap + artist_h
        top = (h - total_h) // 2

        draw.text((text_x, top), title, font=title_font, fill=title_color)
        draw.text((text_x, top + title_h + gap), artist, font=artist_font, fill=artist_color)
        return img

    def _compose_portrait(
        self,
        track: TrackInfo,
        cover: Image.Image,
        cover_unpadded: Image.Image,
        bg: tuple[int, int, int],
        font_key: str,
    ) -> Image.Image:
        w, h = PORTRAIT
        img = self._make_canvas((w, h), cover_unpadded, bg)
        draw = ImageDraw.Draw(img)

        cover_size = cover.width
        cover_x = (w - cover_size) // 2
        cover_y = 480
        img.paste(cover, (cover_x, cover_y))

        center_x = w // 2
        text_width = w - 240
        text_top = cover_y + cover_size + 240

        title = track.title or ""
        title_font = _fit_font(draw, title, text_width, max_size=180, family=font_key)
        artist_font = _load_font(120, font_key)
        artist = _truncate(draw, track.artist or "", artist_font, text_width)

        title_color, artist_color = _text_colors_for(bg)

        t_bbox = draw.textbbox((0, 0), title, font=title_font)
        title_h = t_bbox[3] - t_bbox[1]
        gap = 80

        draw.text((center_x, text_top), title, font=title_font,
                  fill=title_color, anchor="mt")
        draw.text((center_x, text_top + title_h + gap), artist, font=artist_font,
                  fill=artist_color, anchor="mt")
        return img

    def _compose_minimal(
        self,
        cover: Image.Image,
        cover_unpadded: Image.Image,
        bg: tuple[int, int, int],
    ) -> Image.Image:
        """Center the album cover on the canvas with no text overlay."""
        size = LANDSCAPE if self.orientation == "landscape" else PORTRAIT
        img = self._make_canvas(size, cover_unpadded, bg)
        w, h = size
        cw = cover.width
        ch = cover.height
        img.paste(cover, ((w - cw) // 2, (h - ch) // 2))
        return img

    def _prepare_cover(
        self, cover_path: pathlib.Path, size: int
    ) -> tuple[Image.Image, Image.Image, tuple[int, int, int]]:
        """Load the album image, resize to fit ``size``, and return:

        - the *padded* square cover (used for compositing onto the canvas)
        - the *unpadded* cover (preserves the source aspect — used as the
          blur source so non-square covers don't bleed black/colored bands
          into the canvas edges)
        - the background fill color (used for non-blur canvases and for the
          square padding so seams disappear)
        """
        with Image.open(cover_path) as src:
            cover = src.convert("RGB")
        cover.thumbnail((size, size), Image.Resampling.LANCZOS)
        unpadded = cover

        bg = self._background_for(cover)

        if cover.size != (size, size):
            padded = Image.new("RGB", (size, size), bg)
            off = ((size - cover.width) // 2, (size - cover.height) // 2)
            padded.paste(cover, off)
            cover = padded
        return cover, unpadded, bg

    def _background_for(self, cover: Image.Image) -> tuple[int, int, int]:
        # In blur mode the canvas is the blurred album art, but we still
        # need a representative color for cover-padding seams and for the
        # title/artist contrast picker. Average is the safest choice.
        if self.background_mode in ("auto", "blur"):
            # Resizing to 1x1 with LANCZOS averages all pixels into a single
            # color — cheaper and more accurate than walking the histogram.
            small = cover.resize((1, 1), Image.Resampling.LANCZOS)
            r, g, b = small.getpixel((0, 0))[:3]
            return (r, g, b)
        if self.background_mode == "corners":
            # Average the four corner pixels of the (already-resized) cover.
            # Sampled before any square padding so we read the actual album
            # corners, not the padding we'd be adding.
            w, h = cover.size
            corners = [
                cover.getpixel((0, 0)),
                cover.getpixel((w - 1, 0)),
                cover.getpixel((0, h - 1)),
                cover.getpixel((w - 1, h - 1)),
            ]
            r = sum(c[0] for c in corners) // 4
            g = sum(c[1] for c in corners) // 4
            b = sum(c[2] for c in corners) // 4
            return (r, g, b)
        if self.background_mode == "mode":
            # Quantize to 16 clusters, then merge palette entries that are
            # perceptually indistinguishable so a "black" background made
            # up of (0,0,0) + (12,8,10) + (20,18,22) registers as one
            # dominant group rather than three separate ones.
            small = cover.resize((200, 200), Image.Resampling.LANCZOS)
            quantized = small.quantize(colors=16, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette()
            entries = [
                (
                    count,
                    (palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]),
                )
                for count, idx in quantized.getcolors()
            ]

            # Greedy single-link clustering in RGB space. Threshold of 40
            # (Euclidean distance) is tight enough to keep visually distinct
            # shades apart but loose enough that photographic noise, jpeg
            # artifacts, and gradient banding within a "single" color all
            # fold together. Processing in descending count order makes
            # heavy clusters absorb their small neighbors rather than the
            # reverse.
            threshold_sq = 40 * 40
            clusters: list[tuple[int, tuple[int, int, int], int]] = []
            for count, rgb in sorted(entries, key=lambda e: -e[0]):
                merged = False
                for i, (ctotal, crep, crep_count) in enumerate(clusters):
                    dr, dg, db = rgb[0] - crep[0], rgb[1] - crep[1], rgb[2] - crep[2]
                    if dr * dr + dg * dg + db * db <= threshold_sq:
                        # Keep the representative from whichever original
                        # entry has more pixels — the heavy side wins.
                        new_rep, new_count = (
                            (rgb, count) if count > crep_count else (crep, crep_count)
                        )
                        clusters[i] = (ctotal + count, new_rep, new_count)
                        merged = True
                        break
                if not merged:
                    clusters.append((count, rgb, count))

            dominant_total, dominant_rgb, _ = max(clusters, key=lambda c: c[0])
            return dominant_rgb
        return self.background_color  # type: ignore[return-value]

    def _make_canvas(
        self,
        size: tuple[int, int],
        cover_unpadded: Image.Image,
        fill: tuple[int, int, int],
    ) -> Image.Image:
        """Build the background canvas: blurred album art in 'blur' mode,
        otherwise a solid ``fill`` color."""
        if self.background_mode == "blur":
            return self._blurred_canvas(cover_unpadded, size)
        return Image.new("RGB", size, fill)

    def _blurred_canvas(
        self, cover_unpadded: Image.Image, size: tuple[int, int]
    ) -> Image.Image:
        """Scale the album art to fully cover ``size`` (object-fit: cover),
        center-crop, then apply a heavy gaussian blur."""
        w, h = size
        cw, ch = cover_unpadded.size
        scale = max(w / cw, h / ch)
        nw = max(1, round(cw * scale))
        nh = max(1, round(ch * scale))
        big = cover_unpadded.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - w) // 2
        top = (nh - h) // 2
        cropped = big.crop((left, top, left + w, top + h))
        # radius is in pixels — at 3840px wide a radius of 80 obliterates
        # all detail without going so soft that the colors smear into mud.
        return cropped.filter(ImageFilter.GaussianBlur(radius=80))
