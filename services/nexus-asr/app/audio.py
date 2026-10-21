"""Audio decoding: any browser recording format -> mono 16 kHz float32 PCM.

PyAV bundles FFmpeg, so WAV, WebM/Opus (Chrome MediaRecorder), MP4/AAC and
other common containers are handled without a system ffmpeg binary.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import av
import numpy as np

logger = logging.getLogger("nexus_asr.audio")

TARGET_SAMPLE_RATE = 16000
TARGET_LAYOUT = "mono"
TARGET_FORMAT = "s16"

# Keep FFmpeg's own logging quiet; we log a structured summary ourselves.
av.logging.set_level(av.logging.ERROR)


class AudioError(ValueError):
    """Invalid or unusable audio upload."""


class EmptyAudioError(AudioError):
    pass


class AudioTooLongError(AudioError):
    pass


class UnsupportedAudioError(AudioError):
    pass


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray
    duration: float
    sample_rate: int = TARGET_SAMPLE_RATE
    source_format: str = ""
    source_sample_rate: int = 0
    source_channels: int = 0


def _flush_resampler(resampler: av.AudioResampler) -> list[np.ndarray]:
    """PyAV requires a final resample(None) to drain buffered frames."""
    chunks: list[np.ndarray] = []
    while True:
        try:
            frames = resampler.resample(None)
        except Exception:  # noqa: BLE001 - FFmpeg raises version-specific errors
            break
        if not frames:
            break
        for frame in frames:
            chunks.append(frame.to_ndarray().reshape(-1))
        # A single flush is normally enough; break to avoid an infinite loop
        # on implementations that keep returning an empty list.
        break
    return chunks


def decode_audio(data: bytes, *, max_seconds: float = 60.0) -> DecodedAudio:
    if not data:
        raise EmptyAudioError("empty upload")

    try:
        container = av.open(io.BytesIO(data), mode="r")
    except Exception as exc:  # noqa: BLE001 - PyAV error types differ by version
        raise UnsupportedAudioError(f"cannot decode audio container: {exc}") from exc

    with container:
        try:
            stream = next((s for s in container.streams if s.type == "audio"), None)
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedAudioError(f"cannot inspect audio streams: {exc}") from exc

        if stream is None:
            raise UnsupportedAudioError("no audio stream found")

        source_format = container.format.name or ""
        source_sample_rate = int(stream.codec_context.sample_rate or 0)
        source_channels = int(stream.codec_context.channels or 0)

        # Reject clearly-too-long files early when the container exposes a
        # duration. WebM from MediaRecorder often does not, so we still guard
        # during decoding below.
        if stream.duration is not None and stream.time_base is not None:
            declared = float(stream.duration * stream.time_base)
            if declared > max_seconds + 1.0:
                raise AudioTooLongError(
                    f"audio is {declared:.1f}s, limit is {max_seconds:.0f}s"
                )

        resampler = av.AudioResampler(
            format=TARGET_FORMAT,
            layout=TARGET_LAYOUT,
            rate=TARGET_SAMPLE_RATE,
        )

        chunks: list[np.ndarray] = []
        samples_so_far = 0
        max_samples = int((max_seconds + 2.0) * TARGET_SAMPLE_RATE)

        try:
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunk = out.to_ndarray().reshape(-1)
                    if chunk.size:
                        chunks.append(chunk)
                        samples_so_far += chunk.size
                        if samples_so_far > max_samples:
                            raise AudioTooLongError(
                                f"audio exceeds the {max_seconds:.0f}s limit"
                            )
        except AudioTooLongError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedAudioError(f"audio decode failed: {exc}") from exc

        for chunk in _flush_resampler(resampler):
            if chunk.size:
                chunks.append(chunk)
                samples_so_far += chunk.size
                if samples_so_far > max_samples:
                    raise AudioTooLongError(
                        f"audio exceeds the {max_seconds:.0f}s limit"
                    )

    if not chunks:
        raise EmptyAudioError("no decoded audio samples")

    pcm16 = np.concatenate(chunks)
    if pcm16.dtype != np.int16:
        pcm16 = pcm16.astype(np.int16, copy=False)

    samples = pcm16.astype(np.float32) / 32768.0
    duration = samples.size / TARGET_SAMPLE_RATE

    if duration < 0.1:
        raise EmptyAudioError(f"audio too short ({duration:.2f}s)")
    if duration > max_seconds + 0.05:
        raise AudioTooLongError(
            f"audio is {duration:.1f}s, limit is {max_seconds:.0f}s"
        )

    logger.info(
        "decoded bytes=%d format=%s src_rate=%d src_channels=%d duration=%.2fs",
        len(data), source_format, source_sample_rate, source_channels, duration,
    )
    return DecodedAudio(
        samples=np.ascontiguousarray(samples, dtype=np.float32),
        duration=round(duration, 3),
        source_format=source_format,
        source_sample_rate=source_sample_rate,
        source_channels=source_channels,
    )
