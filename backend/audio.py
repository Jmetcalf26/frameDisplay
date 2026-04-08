import asyncio
import io

import numpy as np
import sounddevice as sd
import soundfile as sf


async def record_snippet(
    duration: float,
    sample_rate: int = 44100,
    device=None,
    channels: int = 1,
) -> bytes:
    """Record audio from microphone and return WAV bytes in memory."""
    loop = asyncio.get_event_loop()

    audio_data = await loop.run_in_executor(
        None,
        lambda: sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            device=device,
        ),
    )
    await loop.run_in_executor(None, sd.wait)

    buf = io.BytesIO()
    sf.write(buf, audio_data, sample_rate, format="WAV")
    return buf.getvalue()
