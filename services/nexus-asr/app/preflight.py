"""Startup preflight: verify or download the model before uvicorn starts."""
from __future__ import annotations

import logging
import sys

from . import model_store
from .config import ConfigError, load_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
        info = model_store.ensure_model(
            settings.asr.model_dir,
            auto_download=settings.asr.auto_download,
            offline=settings.asr.offline,
            model_url=settings.asr.model_url,
            tokens_url=settings.asr.tokens_url,
            provider=settings.asr.provider,
            cache_dir=settings.asr.cache_dir,
        )
    except (ConfigError, model_store.ModelError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"provider: {info.provider}")
    print(f"model ready: {info.model_path} ({info.model_bytes} bytes)")
    print(f"tokens ready: {info.tokens_path} ({info.tokens_bytes} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
