"""Model file management: verify, download and install the ASR model.

Only the Python standard library is used here so `download-model.sh` can run
before the virtualenv is fully provisioned.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nexus_asr.model")

MODEL_ID = "sensevoice-small-int8"
MODEL_FILENAME = "model.int8.onnx"
TOKENS_FILENAME = "tokens.txt"

# FunASR / ModelScope SenseVoiceSmall checkpoint. Sizes are checked so a
# truncated download never reaches the inference pipeline; hashing 936 MB on
# every start would add seconds without protecting a fixed local cache.
FUNASR_MODEL_ID = "sensevoice-small"
FUNASR_MODEL_REPO = "iic/SenseVoiceSmall"
FUNASR_FILES = {
    "model.pt": 936_291_369,
    "config.yaml": 1_855,
    "tokens.json": 352_064,
    "am.mvn": 11_203,
    "chn_jpn_yue_eng_ko_spectok.bpe.model": 377_341,
    "configuration.json": 396,
}

MODEL_SIZE = 239_233_841
TOKENS_SIZE = 315_894
MODEL_SHA256 = "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"
TOKENS_SHA256 = "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"

MODEL_URL = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx"
)
TOKENS_URL = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt"
)

USER_AGENT = "nexus-asr/1.0 (+sherpa-onnx sensevoice)"
DOWNLOAD_ATTEMPTS = 3
CHUNK_SIZE = 1024 * 1024


class ModelError(RuntimeError):
    """Base error for model file problems."""


class ModelMissingError(ModelError):
    pass


class ModelIntegrityError(ModelError):
    pass


class ModelDownloadError(ModelError):
    pass


@dataclass(frozen=True)
class FileSpec:
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    model_dir: Path
    model_path: Path
    tokens_path: Path
    model_bytes: int
    tokens_bytes: int
    provider: str = "sherpa-onnx"


def specs(model_url: str = "", tokens_url: str = "") -> tuple[FileSpec, FileSpec]:
    return (
        FileSpec(MODEL_FILENAME, MODEL_SIZE, MODEL_SHA256, model_url or MODEL_URL),
        FileSpec(TOKENS_FILENAME, TOKENS_SIZE, TOKENS_SHA256, tokens_url or TOKENS_URL),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _check_file(path: Path, spec: FileSpec, verify_hash: bool) -> None:
    if not path.is_file():
        raise ModelMissingError(f"missing model file: {path}")
    size = path.stat().st_size
    if size != spec.size:
        raise ModelIntegrityError(
            f"{path.name}: size mismatch, expected {spec.size} bytes, got {size}"
        )
    if verify_hash:
        digest = sha256_file(path)
        if digest != spec.sha256:
            raise ModelIntegrityError(
                f"{path.name}: sha256 mismatch, expected {spec.sha256}, got {digest}"
            )


def verify_funasr_model(model_dir: Path) -> ModelInfo:
    """Validate the local FunASR checkpoint without touching the network.

    Every file must exist and match the known byte size. ``model.pt`` is the
    only large file; a size check catches interrupted downloads while keeping
    startup fast. ``config.yaml`` is also the model's own configuration, so it
    cannot be substituted by the service config.
    """

    model_dir = Path(model_dir)
    missing: list[str] = []
    for name, expected in FUNASR_FILES.items():
        path = model_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        size = path.stat().st_size
        if size != expected:
            raise ModelIntegrityError(
                f"{path.name}: size mismatch, expected {expected} bytes, got {size}"
            )
    if missing:
        raise ModelMissingError(
            f"missing FunASR model files in {model_dir}: {', '.join(missing)}"
        )
    model_path = model_dir / "model.pt"
    tokens_path = model_dir / "tokens.json"
    return ModelInfo(
        model_id=FUNASR_MODEL_ID,
        model_dir=model_dir,
        model_path=model_path,
        tokens_path=tokens_path,
        model_bytes=model_path.stat().st_size,
        tokens_bytes=tokens_path.stat().st_size,
        provider="funasr",
    )


def verify_model(
    model_dir: Path,
    *,
    model_url: str = "",
    tokens_url: str = "",
    verify_hash: bool = True,
) -> ModelInfo:
    model_dir = Path(model_dir)
    model_spec, tokens_spec = specs(model_url, tokens_url)
    model_path = model_dir / model_spec.name
    tokens_path = model_dir / tokens_spec.name
    _check_file(model_path, model_spec, verify_hash)
    _check_file(tokens_path, tokens_spec, verify_hash)
    return ModelInfo(
        model_id=MODEL_ID,
        model_dir=model_dir,
        model_path=model_path,
        tokens_path=tokens_path,
        model_bytes=model_path.stat().st_size,
        tokens_bytes=tokens_path.stat().st_size,
        provider="sherpa-onnx",
    )


def _download(spec: FileSpec, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    if part.exists():
        part.unlink()

    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            logger.info(
                "downloading %s (attempt %s/%s) from %s",
                spec.name, attempt, DOWNLOAD_ATTEMPTS, spec.url,
            )
            request = urllib.request.Request(
                spec.url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                if total and total != spec.size:
                    raise ModelDownloadError(
                        f"{spec.name}: server reports {total} bytes, expected {spec.size}"
                    )
                received = 0
                last_report = 0.0
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 2.0:
                            last_report = now
                            pct = (received / spec.size * 100) if spec.size else 0
                            logger.info(
                                "%s: %.1f MB / %.1f MB (%.1f%%)",
                                spec.name,
                                received / 1024 / 1024,
                                spec.size / 1024 / 1024,
                                pct,
                            )
                    handle.flush()
                    os.fsync(handle.fileno())

            if received != spec.size:
                raise ModelDownloadError(
                    f"{spec.name}: incomplete download, got {received} of {spec.size} bytes"
                )
            digest = sha256_file(part)
            if digest != spec.sha256:
                raise ModelIntegrityError(
                    f"{spec.name}: sha256 mismatch after download "
                    f"(expected {spec.sha256}, got {digest})"
                )
            os.replace(part, target)
            elapsed = time.monotonic() - started
            logger.info(
                "installed %s (%.1f MB in %.1fs)",
                spec.name, spec.size / 1024 / 1024, elapsed,
            )
            return
        except (urllib.error.URLError, TimeoutError, OSError, ModelError) as exc:
            last_error = exc
            logger.warning("%s download failed: %s", spec.name, exc)
            if part.exists():
                part.unlink()
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(2 * attempt)

    raise ModelDownloadError(f"failed to download {spec.name}: {last_error}")


def download_model(
    model_dir: Path,
    *,
    model_url: str = "",
    tokens_url: str = "",
    force: bool = False,
) -> ModelInfo:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    model_spec, tokens_spec = specs(model_url, tokens_url)

    for spec in (model_spec, tokens_spec):
        target = model_dir / spec.name
        if target.exists() and not force:
            try:
                _check_file(target, spec, verify_hash=True)
                logger.info("reuse existing %s", target.name)
                continue
            except ModelError as exc:
                logger.warning("existing %s is invalid: %s", target.name, exc)
                if force:
                    target.unlink()
                else:
                    raise ModelIntegrityError(
                        f"{target.name} is invalid ({exc}). "
                        "Re-run with --force to download a clean copy."
                    ) from exc
        _download(spec, target)

    return verify_model(model_dir, model_url=model_url, tokens_url=tokens_url)


def download_funasr_model(model_dir: Path, *, cache_dir: Path) -> ModelInfo:
    """Download the official ModelScope checkpoint into a fixed directory."""

    try:
        from modelscope import snapshot_download
    except ImportError as exc:  # pragma: no cover - production installs funasr
        raise ModelDownloadError(
            "modelscope is not installed; install requirements.txt or copy the model"
        ) from exc
    model_dir = Path(model_dir)
    cache_dir = Path(cache_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("downloading FunASR model %s into %s", FUNASR_MODEL_REPO, model_dir)
    try:
        snapshot_download(
            FUNASR_MODEL_REPO,
            local_dir=str(model_dir),
            cache_dir=str(cache_dir),
        )
    except Exception as exc:  # noqa: BLE001 - network/library errors are reported uniformly
        raise ModelDownloadError(f"failed to download {FUNASR_MODEL_REPO}: {exc}") from exc
    return verify_funasr_model(model_dir)


def ensure_model(
    model_dir: Path,
    *,
    auto_download: bool,
    offline: bool = False,
    model_url: str = "",
    tokens_url: str = "",
    provider: str = "sherpa-onnx",
    cache_dir: Path | None = None,
) -> ModelInfo:
    """Verify the selected model; download it only when missing and allowed."""
    model_dir = Path(model_dir)
    if provider == "funasr":
        try:
            return verify_funasr_model(model_dir)
        except ModelMissingError as exc:
            if offline or not auto_download:
                raise ModelMissingError(
                    f"{exc}. Run ./download-model.sh or copy the model files into {model_dir}."
                ) from exc
            if cache_dir is None:
                cache_dir = model_dir.parent / "modelscope"
            return download_funasr_model(model_dir, cache_dir=cache_dir)
    if provider != "sherpa-onnx":
        raise ModelError(f"unsupported provider: {provider}")
    try:
        return verify_model(model_dir, model_url=model_url, tokens_url=tokens_url)
    except ModelMissingError as exc:
        if offline or not auto_download:
            raise ModelMissingError(
                f"{exc}. Run ./download-model.sh or copy the model files into {model_dir}."
            ) from exc
        logger.info("model missing, downloading into %s", model_dir)
        return download_model(model_dir, model_url=model_url, tokens_url=tokens_url)
    except ModelIntegrityError:
        # Never silently replace a corrupted model: this could mask a bad
        # deployment or a partially extracted package.
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or download the Nexus ASR model")
    parser.add_argument(
        "--provider",
        default=os.environ.get("NEXUS_ASR_PROVIDER", "funasr"),
        choices=("funasr", "sherpa-onnx"),
    )
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--check", action="store_true", help="verify only (default)")
    parser.add_argument("--download", action="store_true", help="download missing files")
    parser.add_argument("--force", action="store_true", help="replace an invalid file")
    parser.add_argument("--offline", action="store_true", help="never access the network")
    parser.add_argument("--model-url", default="")
    parser.add_argument("--tokens-url", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    default_dir = (
        "models/SenseVoiceSmall" if args.provider == "funasr" else "models/sensevoice-small-int8"
    )
    model_dir = Path(args.model_dir or os.environ.get("NEXUS_ASR_MODEL_DIR", default_dir))
    model_dir = model_dir.expanduser().resolve()
    cache_dir = Path(
        args.cache_dir or os.environ.get("NEXUS_ASR_CACHE_DIR", "models/modelscope")
    ).expanduser().resolve()
    try:
        if args.provider == "funasr":
            if args.download and not args.check:
                if args.offline:
                    raise ModelDownloadError("--download cannot be used with --offline")
                try:
                    info = verify_funasr_model(model_dir)
                except ModelMissingError:
                    info = download_funasr_model(model_dir, cache_dir=cache_dir)
            else:
                info = verify_funasr_model(model_dir)
        elif args.download and not args.check:
            if args.offline:
                raise ModelDownloadError("--download cannot be used with --offline")
            info = download_model(
                model_dir,
                model_url=args.model_url,
                tokens_url=args.tokens_url,
                force=args.force,
            )
        else:
            info = verify_model(
                model_dir,
                model_url=args.model_url,
                tokens_url=args.tokens_url,
            )
    except ModelMissingError as exc:
        if args.download and not args.offline:
            try:
                if args.provider == "funasr":
                    info = download_funasr_model(model_dir, cache_dir=cache_dir)
                else:
                    info = download_model(
                        model_dir,
                        model_url=args.model_url,
                        tokens_url=args.tokens_url,
                        force=args.force,
                    )
            except ModelError as inner:
                print(f"ERROR: {inner}", file=sys.stderr)
                return 1
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    except ModelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"provider:   {info.provider}")
    print(f"model:      {info.model_path} ({info.model_bytes} bytes)")
    print(f"tokens:     {info.tokens_path} ({info.tokens_bytes} bytes)")
    print("verification: OK (size)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
