import asyncio
import logging
import pathlib

from samsungtvws import SamsungTVWS

log = logging.getLogger("framedisplay")


class FrameTV:
    """Async wrapper around samsungtvws' synchronous art API.

    Every blocking call is dispatched to a worker thread so it doesn't stall
    the recognition loop. Network / auth failures are logged and swallowed —
    losing a TV update should never kill the listener.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token_file: pathlib.Path,
        matte: str = "none",
        portrait_matte: str = "none",
    ):
        self.host = host
        self.port = port
        self.token_file = pathlib.Path(token_file)
        self.matte = matte
        self.portrait_matte = portrait_matte

        self.token_file.parent.mkdir(parents=True, exist_ok=True)

        self._tv = SamsungTVWS(
            host=host,
            port=port,
            token_file=str(self.token_file),
            name="frameDisplay",
        )
        self._art = self._tv.art()
        self._last_content_id: str | None = None
        self._lock = asyncio.Lock()

    async def upload_and_display(self, image_path: pathlib.Path) -> None:
        """Upload the composed image and make it the active art."""
        async with self._lock:
            try:
                data = image_path.read_bytes()
            except OSError as e:
                log.warning("Failed to read composed image %s: %s", image_path, e)
                return

            try:
                content_id = await asyncio.to_thread(
                    self._art.upload,
                    data,
                    matte=self.matte,
                    portrait_matte=self.portrait_matte,
                    file_type="jpeg",
                )
            except Exception:
                log.exception("Frame TV upload failed")
                return

            log.info("Frame TV upload complete: content_id=%s", content_id)

            try:
                await asyncio.to_thread(self._art.select_image, content_id, None, True)
            except Exception:
                log.exception("Frame TV select_image failed for %s", content_id)
                return

            try:
                await asyncio.to_thread(self._art.set_artmode, True)
            except Exception:
                log.exception("Frame TV set_artmode failed")
                # Keep going — the image is selected even if the mode toggle failed.

            # Best-effort cleanup of the prior upload so "My Collection" doesn't
            # fill up with stale track images.
            prev = self._last_content_id
            self._last_content_id = content_id
            if prev and prev != content_id:
                try:
                    await asyncio.to_thread(self._art.delete, prev)
                    log.info("Frame TV deleted previous upload: %s", prev)
                except Exception:
                    log.warning("Frame TV delete of previous upload %s failed", prev, exc_info=True)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._tv.close)
        except Exception:
            log.exception("Frame TV close failed")
