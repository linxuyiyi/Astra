from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import wave

from app.engine import AsrEngine

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def engine() -> AsrEngine:
    instance = AsrEngine(
        model_path=ROOT / "models/sensevoice-small-int8/model.int8.onnx",
        tokens_path=ROOT / "models/sensevoice-small-int8/tokens.txt",
        num_threads=4,
        language="auto",
        use_itn=True,
    )
    instance.load()
    return instance


def load_wav() -> np.ndarray:
    with wave.open(str(ROOT / "testdata/zh-5s.wav"), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def test_transcribe(engine: AsrEngine):
    result = engine.transcribe(load_wav())
    assert "会议纪要" in result.text
    assert result.language == "zh"
    assert result.elapsed_ms > 0
    assert result.rtf < 1


def test_concurrent_transcribe(engine: AsrEngine):
    samples = load_wav()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: engine.transcribe(samples), range(8)))
    assert all("会议纪要" in item.text for item in results)
    assert len({item.text for item in results}) == 1
