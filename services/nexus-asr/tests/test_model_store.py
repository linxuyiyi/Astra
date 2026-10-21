from __future__ import annotations

from pathlib import Path

import pytest

from app import model_store
from app.config import load_settings

ROOT = Path(__file__).resolve().parent.parent


def test_actual_model_verifies():
    info = model_store.verify_model(ROOT / "models/sensevoice-small-int8")
    assert info.model_bytes == model_store.MODEL_SIZE
    assert info.tokens_bytes == model_store.TOKENS_SIZE
    assert info.model_path.is_file()
    assert info.tokens_path.is_file()


def test_missing_model_offline_fails(tmp_path: Path):
    with pytest.raises(model_store.ModelMissingError):
        model_store.ensure_model(tmp_path, auto_download=False, offline=True)


def test_wrong_size_is_rejected(tmp_path: Path):
    (tmp_path / "model.int8.onnx").write_bytes(b"not the model")
    (tmp_path / "tokens.txt").write_bytes(b"")
    with pytest.raises(model_store.ModelIntegrityError):
        model_store.verify_model(tmp_path)


def test_config_defaults():
    settings = load_settings()
    assert settings.asr.provider == "sherpa-onnx"
    assert settings.asr.device == "cpu"
    assert settings.asr.num_threads >= 1
    assert settings.server.max_audio_seconds == 60
    assert settings.server.max_upload_mb == 12
