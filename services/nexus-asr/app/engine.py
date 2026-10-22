"""SenseVoiceSmall inference: FunASR (default) or sherpa-onnx (optional)."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:  # sherpa-onnx is an optional low-memory provider.
    import sherpa_onnx
except ImportError:  # pragma: no cover - only when requirements are trimmed
    sherpa_onnx = None

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
        if sherpa_onnx is None:
            raise RuntimeError(
                "sherpa-onnx is not installed; install requirements-sherpa.txt "
                "or use provider: funasr"
            )
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


# SenseVoice returns leading control tags such as
# ``<|zh|><|NEUTRAL|><|Speech|><|withitn|>`` before the transcript. They must
# never reach the user message.
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_LEADING_TAGS_RE = re.compile(r"^(?:<\|[^|]*\|>)+")


def clean_transcript(text: str | None) -> str:
    return _TAG_RE.sub("", text or "").strip()


def parse_sensevoice_tags(text: str | None) -> tuple[str, str, str]:
    leading = _LEADING_TAGS_RE.match(text or "")
    if not leading:
        return "", "", ""
    values = [
        item.replace("<|", "").replace("|>", "").strip()
        for item in _TAG_RE.findall(leading.group(0))
    ]
    values += [""] * (3 - len(values))
    return values[0], values[1], values[2]


class FunAsrEngine:
    """SenseVoiceSmall through the official FunASR ``AutoModel`` pipeline.

    FunASR loads a PyTorch checkpoint plus the FSMN-VAD frontend. The pipeline
    is CPU-only and shared by request threads; ``generate`` is serialised so a
    single checkpoint is never mutated concurrently.
    """

    def __init__(
        self,
        *,
        model_dir: Path,
        vad_model: str = "fsmn-vad",
        vad_max_segment_ms: int = 30000,
        num_threads: int = 4,
        language: str = "auto",
        use_itn: bool = True,
        cache_dir: Path | None = None,
        hub: str = "ms",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.vad_model = vad_model
        self.vad_max_segment_ms = vad_max_segment_ms
        self.num_threads = num_threads
        self.language = language
        self.use_itn = use_itn
        self.cache_dir = Path(cache_dir) if cache_dir else self.model_dir.parent / "modelscope"
        self.hub = hub
        self._model = None
        self._lock = threading.Lock()
        self.load_ms: float = 0.0
        self._loaded = threading.Event()

    @property
    def ready(self) -> bool:
        return self._model is not None and self._loaded.is_set()

    def load(self) -> None:
        if self.ready:
            return
        if sherpa_onnx is None:
            raise RuntimeError(
                "sherpa-onnx is not installed; install requirements-sherpa.txt "
                "or use provider: funasr"
            )
        started = time.perf_counter()
        # FunASR stores downloaded VAD assets under MODELSCOPE_CACHE. Set it
        # before importing funasr so a fresh Linux host uses the fixed cache.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MODELSCOPE_CACHE", str(self.cache_dir))
        try:
            import torch
            from funasr import AutoModel
        except ImportError as exc:  # pragma: no cover - checked at install time
            raise RuntimeError(
                "funasr/torch is not installed; run ./setup-mac.sh or pip install -r requirements.txt"
            ) from exc

        torch.set_num_threads(self.num_threads)
        logger.info(
            "loading FunASR model=%s vad=%s threads=%d cache=%s",
            self.model_dir, self.vad_model, self.num_threads, self.cache_dir,
        )
        model = AutoModel(
            model=str(self.model_dir),
            vad_model=self.vad_model,
            vad_kwargs={"max_single_segment_time": self.vad_max_segment_ms},
            device="cpu",
            disable_update=True,
            disable_pbar=True,
            log_level="ERROR",
            hub=self.hub,
        )
        self._model = model
        self.load_ms = (time.perf_counter() - started) * 1000
        self._loaded.set()
        logger.info("FunASR model loaded in %.0f ms", self.load_ms)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> Transcript:
        if not self.ready:
            raise RuntimeError("ASR engine is not loaded")
        if samples.size == 0:
            raise ValueError("empty audio samples")

        kwargs: dict[str, object] = {
            "input": np.ascontiguousarray(samples, dtype=np.float32),
            "fs": sample_rate,
            "batch_size": 1,
            "disable_pbar": True,
            "use_itn": self.use_itn,
        }
        if self.language != "auto":
            kwargs["language"] = self.language

        started = time.perf_counter()
        try:
            with self._lock:
                result = self._model.generate(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 500 by the API
            raise RuntimeError(f"FunASR inference failed: {exc}") from exc
        elapsed = time.perf_counter() - started

        raw_text = ""
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                raw_text = str(first.get("text") or "")
        text = clean_transcript(raw_text)
        detected_language, emotion, event = parse_sensevoice_tags(raw_text)
        if self.language != "auto":
            detected_language = self.language
        duration = samples.size / sample_rate
        return Transcript(
            text=text,
            language=detected_language,
            emotion=emotion,
            event=event,
            elapsed_ms=round(elapsed * 1000, 2),
            rtf=round(elapsed / duration, 4) if duration > 0 else 0.0,
        )


def build_engine(settings) -> AsrEngine | FunAsrEngine:
    """Create the configured engine; the model is loaded by the caller."""

    if settings.asr.provider == "funasr":
        return FunAsrEngine(
            model_dir=settings.asr.model_dir,
            vad_model=settings.asr.vad_model,
            vad_max_segment_ms=settings.asr.vad_max_segment_ms,
            num_threads=settings.asr.num_threads,
            language=settings.asr.language,
            use_itn=settings.asr.use_itn,
            cache_dir=settings.asr.cache_dir,
            hub=settings.asr.hub,
        )
    return AsrEngine(
        model_path=settings.asr.model_dir / "model.int8.onnx",
        tokens_path=settings.asr.model_dir / "tokens.txt",
        num_threads=settings.asr.num_threads,
        language=settings.asr.language,
        use_itn=settings.asr.use_itn,
    )
