"""Configuration loading for the Nexus ASR server.

The YAML file is the single source of truth. A small number of operational
values can be overridden with NEXUS_ASR_* environment variables for systemd
and container deployments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SERVICE_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when config.yaml is missing or invalid."""


def _as_int(value: Any, name: str, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {result}")
    return result


def _as_float(value: Any, name: str, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {result}")
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 19899
    log_level: str = "info"
    max_upload_mb: int = 12
    max_audio_seconds: float = 60.0
    max_concurrency: int = 2
    queue_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    rate_limit_per_minute: int = 30
    trust_proxy_headers: bool = True
    docs_enabled: bool = True

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@dataclass
class AsrConfig:
    provider: str = "funasr"
    model: str = "sensevoice-small"
    model_dir: Path = SERVICE_ROOT / "models" / "SenseVoiceSmall"
    device: str = "cpu"
    num_threads: int = 4
    language: str = "auto"
    use_itn: bool = True
    auto_download: bool = True
    offline: bool = False
    # sherpa-onnx download locations (only used by that provider).
    model_url: str = ""
    tokens_url: str = ""
    # FunASR pipeline settings. The model is a local directory so a normal
    # start never downloads weights; VAD is cached under cache_dir.
    vad_model: str = "fsmn-vad"
    vad_max_segment_ms: int = 30000
    hub: str = "ms"
    cache_dir: Path = SERVICE_ROOT / "models" / "modelscope"
    languages: list[str] = field(default_factory=lambda: ["zh", "en", "ja", "ko", "yue"])

    @property
    def model_files(self) -> tuple[str, ...]:
        if self.provider == "funasr":
            return (
                "model.pt",
                "config.yaml",
                "tokens.json",
                "am.mvn",
                "chn_jpn_yue_eng_ko_spectok.bpe.model",
                "configuration.json",
            )
        return ("model.int8.onnx", "tokens.txt")


@dataclass
class Settings:
    config_path: Path
    server: ServerConfig
    asr: AsrConfig
    service_root: Path
    version: str = "1.0.0"


def _env_override(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    path = Path(
        config_path
        or os.environ.get("NEXUS_ASR_CONFIG")
        or SERVICE_ROOT / "config.yaml"
    ).expanduser().resolve()

    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path}. Copy config.yaml or set NEXUS_ASR_CONFIG."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a mapping at the top level")

    server_raw = raw.get("server") or {}
    asr_raw = raw.get("asr") or {}
    if not isinstance(server_raw, dict) or not isinstance(asr_raw, dict):
        raise ConfigError("server/asr sections must be mappings")

    server = ServerConfig(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=_as_int(server_raw.get("port", 19899), "server.port", 1),
        log_level=str(server_raw.get("log_level", "info")).lower(),
        max_upload_mb=_as_int(server_raw.get("max_upload_mb", 12), "server.max_upload_mb", 1),
        max_audio_seconds=_as_float(
            server_raw.get("max_audio_seconds", 60), "server.max_audio_seconds", 1
        ),
        max_concurrency=_as_int(
            server_raw.get("max_concurrency", 2), "server.max_concurrency", 1
        ),
        queue_timeout_seconds=_as_float(
            server_raw.get("queue_timeout_seconds", 5),
            "server.queue_timeout_seconds",
            0,
        ),
        request_timeout_seconds=_as_float(
            server_raw.get("request_timeout_seconds", 30),
            "server.request_timeout_seconds",
            1,
        ),
        rate_limit_per_minute=_as_int(
            server_raw.get("rate_limit_per_minute", 30),
            "server.rate_limit_per_minute",
            0,
        ),
        trust_proxy_headers=_as_bool(server_raw.get("trust_proxy_headers", True)),
        docs_enabled=_as_bool(server_raw.get("docs_enabled", True)),
    )

    provider = str(asr_raw.get("provider", "funasr")).strip().lower()
    if provider not in {"funasr", "sherpa-onnx"}:
        raise ConfigError(
            f"unsupported asr.provider: {provider!r} (funasr or sherpa-onnx)"
        )
    default_model = "sensevoice-small" if provider == "funasr" else "sensevoice-small-int8"
    default_dir = "./models/SenseVoiceSmall" if provider == "funasr" else "./models/sensevoice-small-int8"

    model_dir_raw = asr_raw.get("model_dir", default_dir)
    model_dir = Path(str(model_dir_raw)).expanduser()
    if not model_dir.is_absolute():
        model_dir = (path.parent / model_dir).resolve()

    cache_dir_raw = asr_raw.get("cache_dir", "./models/modelscope")
    cache_dir = Path(str(cache_dir_raw)).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = (path.parent / cache_dir).resolve()

    language = str(asr_raw.get("language", "auto")).strip().lower()
    if language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
        raise ConfigError(
            "asr.language must be one of auto/zh/en/ja/ko/yue, "
            f"got {language!r}"
        )

    device = str(asr_raw.get("device", "cpu")).strip().lower()
    if device != "cpu":
        raise ConfigError(f"unsupported asr.device: {device!r} (CPU only)")

    languages_raw = asr_raw.get("languages") or ["zh", "en", "ja", "ko", "yue"]
    if not isinstance(languages_raw, list):
        raise ConfigError("asr.languages must be a list")

    asr = AsrConfig(
        provider=provider,
        model=str(asr_raw.get("model", default_model)),
        model_dir=model_dir,
        device=device,
        num_threads=_as_int(asr_raw.get("num_threads", 4), "asr.num_threads", 1),
        language=language,
        use_itn=_as_bool(asr_raw.get("use_itn", True)),
        auto_download=_as_bool(asr_raw.get("auto_download", True)),
        offline=_as_bool(asr_raw.get("offline", False)),
        model_url=str(asr_raw.get("model_url", "")),
        tokens_url=str(asr_raw.get("tokens_url", "")),
        vad_model=str(asr_raw.get("vad_model", "fsmn-vad")),
        vad_max_segment_ms=_as_int(
            asr_raw.get("vad_max_segment_ms", 30000), "asr.vad_max_segment_ms", 1000
        ),
        hub=str(asr_raw.get("hub", "ms")),
        cache_dir=cache_dir,
        languages=[str(item) for item in languages_raw],
    )

    # Operational overrides. These are intentionally limited to deployment
    # concerns; model geometry stays in config.yaml.
    if value := _env_override("NEXUS_ASR_HOST"):
        server.host = value
    if value := _env_override("NEXUS_ASR_PORT"):
        server.port = _as_int(value, "NEXUS_ASR_PORT", 1)
    if value := _env_override("NEXUS_ASR_LOG_LEVEL"):
        server.log_level = value.lower()
    if value := _env_override("NEXUS_ASR_PROVIDER"):
        provider = value.strip().lower()
        if provider not in {"funasr", "sherpa-onnx"}:
            raise ConfigError(
                f"unsupported NEXUS_ASR_PROVIDER: {provider!r} (funasr or sherpa-onnx)"
            )
        asr.provider = provider
    if value := _env_override("NEXUS_ASR_MODEL_DIR"):
        asr.model_dir = Path(value).expanduser().resolve()
    if value := _env_override("NEXUS_ASR_CACHE_DIR"):
        asr.cache_dir = Path(value).expanduser().resolve()
    if value := _env_override("NEXUS_ASR_VAD_MODEL"):
        asr.vad_model = value
    if value := _env_override("NEXUS_ASR_NUM_THREADS"):
        asr.num_threads = _as_int(value, "NEXUS_ASR_NUM_THREADS", 1)
    if value := _env_override("NEXUS_ASR_MAX_CONCURRENCY"):
        server.max_concurrency = _as_int(value, "NEXUS_ASR_MAX_CONCURRENCY", 1)
    if value := _env_override("NEXUS_ASR_AUTO_DOWNLOAD"):
        asr.auto_download = _as_bool(value)
    if value := _env_override("NEXUS_ASR_OFFLINE"):
        asr.offline = _as_bool(value)

    return Settings(
        config_path=path,
        server=server,
        asr=asr,
        service_root=SERVICE_ROOT,
    )
