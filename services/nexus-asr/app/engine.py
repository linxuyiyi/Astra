"""SenseVoice-Small INT8 inference via the official sherpa-onnx Python API."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sherpa_onnx

logger = logging.getLogger("nexus_asr.engine")


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    emotion: str
    event: str
    elapsed_ms: float
    rtf: float


def _clean_tag(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("<|", "").replace("|>", "").strip()


class AsrEngine:
    """A single loaded recognizer shared by concurrent request threads.

    sherpa-onnx streams are independent; the recognizer only owns the model
    and ONNX Runtime session. Concurrency was validated with four simultaneous
    decodes in tests.
    """

    def __init__(
        self,
        *,
        model_path: Path,
        tokens_path: Path,
        num_threads: int = 4,
        language: str = "auto",
        use_itn: bool = True,
    ) -> None:
        self.model_path = Path(model_path)
        self.tokens_path = Path(tokens_path)
        self.num_threads = num_threads
        self.language = "" if language == "auto" else language
        self.use_itn = use_itn
        self._recognizer: sherpa_onnx.OfflineRecognizer | None = None
        self.load_ms: float = 0.0
        self._loaded = threading.Event()

    @property
    def ready(self) -> bool:
        return self._recognizer is not None and self._loaded.is_set()

    def load(self) -> None:
        if self.ready:
            return
        started = time.perf_counter()
        logger.info(
            "loading model model=%s threads=%d language=%s itn=%s",
            self.model_path.name, self.num_threads, self.language or "auto", self.use_itn,
        )
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.model_path),
            tokens=str(self.tokens_path),
            num_threads=self.num_threads,
            use_itn=self.use_itn,
            language=self.language,
            debug=False,
        )
        self._recognizer = recognizer
        self.load_ms = (time.perf_counter() - started) * 1000
        self._loaded.set()
        logger.info("model loaded in %.0f ms", self.load_ms)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> Transcript:
        if not self.ready:
            raise RuntimeError("ASR engine is not loaded")
        if samples.size == 0:
            raise ValueError("empty audio samples")

        started = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        result = stream.result
        elapsed = time.perf_counter() - started

        duration = samples.size / sample_rate
        return Transcript(
            text=(result.text or "").strip(),
            language=_clean_tag(getattr(result, "lang", "")),
            emotion=_clean_tag(getattr(result, "emotion", "")),
            event=_clean_tag(getattr(result, "event", "")),
            elapsed_ms=round(elapsed * 1000, 2),
            rtf=round(elapsed / duration, 4) if duration > 0 else 0.0,
        )
