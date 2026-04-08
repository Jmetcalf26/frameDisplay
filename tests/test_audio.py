from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from backend.audio import record_snippet


class TestRecordSnippet:
    @pytest.mark.asyncio
    async def test_returns_wav_bytes(self):
        fake_audio = np.zeros((44100, 1), dtype="int16")

        with (
            patch("backend.audio.sd") as mock_sd,
            patch("backend.audio.sf") as mock_sf,
        ):
            mock_sd.rec.return_value = fake_audio
            mock_sd.wait.return_value = None

            # Make sf.write actually write WAV header bytes
            def fake_write(buf, data, sr, format):
                buf.write(b"RIFF" + b"\x00" * 100)

            mock_sf.write.side_effect = fake_write

            result = await record_snippet(duration=1.0, sample_rate=44100)

        assert isinstance(result, bytes)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_calls_sounddevice_with_correct_params(self):
        fake_audio = np.zeros((22050, 1), dtype="int16")

        with (
            patch("backend.audio.sd") as mock_sd,
            patch("backend.audio.sf") as mock_sf,
        ):
            mock_sd.rec.return_value = fake_audio
            mock_sd.wait.return_value = None
            mock_sf.write.side_effect = lambda buf, *a, **kw: buf.write(b"\x00")

            await record_snippet(
                duration=0.5, sample_rate=44100, device=2, channels=1
            )

        mock_sd.rec.assert_called_once_with(
            22050,
            samplerate=44100,
            channels=1,
            dtype="int16",
            device=2,
        )
        mock_sd.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_writes_wav_format(self):
        fake_audio = np.zeros((44100, 1), dtype="int16")

        with (
            patch("backend.audio.sd") as mock_sd,
            patch("backend.audio.sf") as mock_sf,
        ):
            mock_sd.rec.return_value = fake_audio
            mock_sd.wait.return_value = None
            mock_sf.write.side_effect = lambda buf, *a, **kw: buf.write(b"\x00")

            await record_snippet(duration=1.0, sample_rate=44100)

        call_args = mock_sf.write.call_args
        assert call_args[0][2] == 44100  # sample_rate
        assert call_args[1]["format"] == "WAV"
