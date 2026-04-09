import asyncio
import io
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf


def to_wav_bytes(audio_data: np.ndarray, sample_rate: int = 44100) -> bytes:
    """Convert a numpy audio array to WAV bytes in memory."""
    buf = io.BytesIO()
    sf.write(buf, audio_data, sample_rate, format="WAV")
    return buf.getvalue()


async def record_snippet(
    duration: float,
    sample_rate: int = 44100,
    device=None,
    channels: int = 1,
) -> bytes:
    """Record audio from microphone and return WAV bytes."""
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

    return to_wav_bytes(audio_data, sample_rate)


async def record_with_snapshots(
    total_duration: float,
    snapshot_at: list[float],
    sample_rate: int = 44100,
    device=None,
    channels: int = 1,
    on_snapshot=None,
) -> bytes:
    """Record audio, firing snapshots at specified times while continuing to record.

    Args:
        total_duration: Total seconds to record.
        snapshot_at: List of times (in seconds) to snapshot the buffer so far.
                     Each snapshot is sent to on_snapshot as WAV bytes.
        sample_rate: Sample rate in Hz.
        device: Audio device index or None for default.
        channels: Number of audio channels.
        on_snapshot: async callable(duration, wav_bytes) called for each snapshot.

    Returns:
        WAV bytes of the full recording.
    """
    loop = asyncio.get_event_loop()
    chunks: list[np.ndarray] = []
    samples_recorded = 0
    lock = threading.Lock()

    # Sort snapshots and track which have fired
    pending_snapshots = sorted(snapshot_at)
    snapshot_samples = [int(t * sample_rate) for t in pending_snapshots]

    def callback(indata, frames, time_info, status):
        nonlocal samples_recorded
        with lock:
            chunks.append(indata.copy())
            samples_recorded += frames

    def get_buffer_copy():
        with lock:
            if not chunks:
                return np.empty((0, channels), dtype="int16")
            return np.concatenate(chunks, axis=0)

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
        device=device,
        callback=callback,
    )

    total_samples = int(total_duration * sample_rate)

    with stream:
        while samples_recorded < total_samples:
            # Check if any snapshots should fire
            while snapshot_samples and samples_recorded >= snapshot_samples[0]:
                snap_duration = pending_snapshots.pop(0)
                snapshot_samples.pop(0)

                buf = get_buffer_copy()
                snap_samples = int(snap_duration * sample_rate)
                snap_buf = buf[:snap_samples]
                wav = to_wav_bytes(snap_buf, sample_rate)

                if on_snapshot:
                    asyncio.run_coroutine_threadsafe(
                        on_snapshot(snap_duration, wav), loop
                    )

            await asyncio.sleep(0.1)

    full_audio = get_buffer_copy()[:total_samples]
    return to_wav_bytes(full_audio, sample_rate)
