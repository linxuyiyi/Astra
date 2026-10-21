from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app.audio import (
    AudioTooLongError,
    EmptyAudioError,
    UnsupportedAudioError,
    decode_audio,
)

ROOT = Path(__file__).resolve().parent.parent


def test_decode_wav():
    audio = decode_audio((ROOT / "testdata/zh-5s.wav").read_bytes(), max_seconds=10)
    assert audio.duration == pytest.approx(5.03, abs=0.05)
    assert audio.sample_rate == 16000
    assert audio.samples.dtype.name == "float32"
    assert abs(audio.samples).max() <= 1.0
    assert audio.source_format == "wav"


def test_decode_rejects_invalid():
    with pytest.raises(UnsupportedAudioError):
        decode_audio(b"not an audio file", max_seconds=10)


def test_decode_rejects_empty():
    with pytest.raises(EmptyAudioError):
        decode_audio(b"", max_seconds=10)


def test_decode_rejects_too_long():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000 * 2)
    with pytest.raises(AudioTooLongError):
        decode_audio(buffer.getvalue(), max_seconds=1)
