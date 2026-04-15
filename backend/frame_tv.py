import asyncio
import logging
import pathlib

from samsungtvws import SamsungTVWS

log = logging.getLogger("framedisplay")

# Errors that mean the underlying websocket has gone stale and the call
# should be retried after a forced reconnect. BrokenPipeError /
# ConnectionResetError both subclass OSError; websocket-client raises its
# own WebSocketException family on top.
_RECONNECT_ERRORS: tuple[type[BaseException], ...] = (OSError,)
try:
    from websocket import WebSocketException

    _RECONNECT_ERRORS = (*_RECONNECT_ERRORS, WebSocketException)
except ImportError:
    pass


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

    # Hard ceiling on any single samsungtvws call. select_image in
    # particular can block forever waiting for a response event the TV
    # never sends in a form the library recognizes — observed as a silent
    # hang that wedges the whole listen loop. The timeout lets us abandon
    # the call and keep processing; the dangling worker thread will finish
    # or be cleaned up at process exit.
    _CALL_TIMEOUT = 20.0

    async def _call(self, fn, *args, **kwargs):
        """Run a sync samsungtvws call in a thread with a timeout, retrying
        once after a forced reconnect if the websocket is stale.

        The samsungtvws client opens its websocket lazily and reuses it. If
        the TV (or a NAT in between) drops the connection during an idle
        period, the next call hits BrokenPipeError on the first send. We
        catch that, close the art websocket so samsungtvws will re-open it,
        and retry the call once. If the call hangs instead of erroring, the
        timeout eventually surfaces as a TimeoutError for the caller.

        Note: ``self._art`` is a SamsungTVArt instance with its OWN
        connection, separate from ``self._tv``'s. Closing ``self._tv`` does
        nothing for the art API, so we must close ``self._art`` here.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=self._CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Frame TV call %s timed out after %.0fs; forcing reconnect",
                fn.__name__, self._CALL_TIMEOUT,
            )
            try:
                await asyncio.to_thread(self._art.close)
            except Exception:
                log.debug("Frame TV close after timeout failed", exc_info=True)
            raise
        except _RECONNECT_ERRORS as e:
            log.warning("Frame TV call %s failed (%s); reconnecting and retrying", fn.__name__, e)
            try:
                await asyncio.to_thread(self._art.close)
            except Exception:
                log.debug("Frame TV close during reconnect failed", exc_info=True)
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=self._CALL_TIMEOUT,
            )

    async def upload_and_display(self, image_path: pathlib.Path) -> None:
        """Upload the composed image and make it the active art."""
        async with self._lock:
            # Close the art websocket before every upload so samsungtvws
            # opens a fresh one on the next call. Long-lived sockets here
            # go stale between uploads (the TV or an intermediate NAT drops
            # them during idle periods) and surface as BrokenPipe on the
            # next send. The retry in _call catches this, but reconnecting
            # up front avoids the noisy warning + retry round trip on every
            # upload. The cost is one extra websocket handshake per upload,
            # which is trivial relative to the image transfer itself.
            try:
                await asyncio.to_thread(self._art.close)
            except Exception:
                log.debug("Frame TV pre-upload close failed", exc_info=True)

            try:
                data = image_path.read_bytes()
            except OSError as e:
                log.warning("Failed to read composed image %s: %s", image_path, e)
                return

            try:
                content_id = await self._call(
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

            # Once any _call in this sequence times out, the websocket is
            # confused enough that subsequent calls are likely to hang too.
            # Bail out early rather than burning _CALL_TIMEOUT per remaining
            # call — the next upload will reconnect from scratch anyway.
            try:
                await self._call(self._art.select_image, content_id, None, True)
            except asyncio.TimeoutError:
                log.warning("Frame TV select_image timed out; bailing out of upload sequence")
                return
            except Exception:
                log.exception("Frame TV select_image failed for %s", content_id)
                return

            # Only flip art mode if it's currently off — calling set_artmode(True)
            # while the TV is already in art mode hangs forever waiting for an
            # ack the TV never sends.
            artmode_ok = True
            try:
                current = await self._call(self._art.get_artmode)
            except asyncio.TimeoutError:
                log.warning("Frame TV get_artmode timed out; skipping art mode + cleanup")
                artmode_ok = False
                current = None
            except Exception:
                log.exception("Frame TV get_artmode failed")
                current = None
            if artmode_ok and current != "on":
                try:
                    await self._call(self._art.set_artmode, True)
                except asyncio.TimeoutError:
                    log.warning("Frame TV set_artmode timed out; skipping cleanup")
                    artmode_ok = False
                except Exception:
                    log.exception("Frame TV set_artmode failed")
                    # Keep going — the image is selected even if the mode toggle failed.

            # Best-effort cleanup of the prior upload so "My Collection" doesn't
            # fill up with stale track images.
            prev = self._last_content_id
            self._last_content_id = content_id
            if artmode_ok and prev and prev != content_id:
                try:
                    await self._call(self._art.delete, prev)
                    log.info("Frame TV deleted previous upload: %s", prev)
                except asyncio.TimeoutError:
                    log.warning("Frame TV delete of previous upload %s timed out", prev)
                except Exception:
                    log.warning("Frame TV delete of previous upload %s failed", prev, exc_info=True)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._art.close)
        except Exception:
            log.exception("Frame TV art close failed")
        try:
            await asyncio.to_thread(self._tv.close)
        except Exception:
            log.exception("Frame TV close failed")
