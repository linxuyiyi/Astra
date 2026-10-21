"""Nexus ASR Server: FastAPI wrapper around the official sherpa-onnx
SenseVoice-Small INT8 recognizer.

Design constraints:
- model is loaded once at startup and reused;
- audio decoding and inference run in a thread pool, never on the event loop;
- upload size, audio duration, request rate, concurrency and latency are
  bounded;
- audio is processed in memory and never written to disk or logged.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import model_store
from .audio import (
    AudioTooLongError,
    EmptyAudioError,
    UnsupportedAudioError,
    decode_audio,
)
from .config import Settings, load_settings
from .engine import AsrEngine
from .limiter import RateLimiter
from .schemas import (
    ErrorDetail,
    HealthResponse,
    ModelCard,
    ModelsResponse,
    TranscribeResponse,
)

logger = logging.getLogger("nexus_asr")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
READ_CHUNK = 64 * 1024


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _api_error(status: int, code: str, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers=headers,
    )


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    _configure_logging(settings.server.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.started_at = time.time()
        app.state.engine = None
        app.state.model_info = None
        app.state.active_requests = 0
        app.state.total_requests = 0
        app.state.rejected_requests = 0
        app.state.semaphore = asyncio.Semaphore(settings.server.max_concurrency)
        app.state.rate_limiter = RateLimiter(settings.server.rate_limit_per_minute)
        app.state.executor = ThreadPoolExecutor(
            max_workers=settings.server.max_concurrency,
            thread_name_prefix="asr",
        )

        logger.info(
            "starting service config=%s model_dir=%s offline=%s",
            settings.config_path,
            settings.asr.model_dir,
            settings.asr.offline,
        )

        # Hashing 229 MB and loading the ONNX session are blocking; keep them
        # off the event loop. Uvicorn will not accept traffic until lifespan
        # startup completes.
        model_info = await asyncio.to_thread(
            model_store.ensure_model,
            settings.asr.model_dir,
            auto_download=settings.asr.auto_download,
            offline=settings.asr.offline,
            model_url=settings.asr.model_url,
            tokens_url=settings.asr.tokens_url,
        )
        engine = AsrEngine(
            model_path=model_info.model_path,
            tokens_path=model_info.tokens_path,
            num_threads=settings.asr.num_threads,
            language=settings.asr.language,
            use_itn=settings.asr.use_itn,
        )
        await asyncio.to_thread(engine.load)

        app.state.model_info = model_info
        app.state.engine = engine
        logger.info(
            "ready model=%s load_ms=%.0f threads=%d concurrency=%d max_audio=%.0fs",
            settings.asr.model,
            engine.load_ms,
            settings.asr.num_threads,
            settings.server.max_concurrency,
            settings.server.max_audio_seconds,
        )
        try:
            yield
        finally:
            app.state.executor.shutdown(wait=False, cancel_futures=True)
            logger.info("service stopped")

    app = FastAPI(
        title="Nexus ASR Server",
        version="1.0.0",
        description="Local sherpa-onnx SenseVoice-Small INT8 speech recognition",
        docs_url="/docs" if settings.server.docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        response.headers["x-process-time-ms"] = f"{elapsed_ms:.1f}"
        if request.url.path.startswith("/api/asr"):
            logger.info(
                "http method=%s path=%s status=%d request_id=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                request_id,
                elapsed_ms,
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", "")
        logger.info("validation_error request_id=%s errors=%s", request_id, exc.errors())
        return JSONResponse(
            status_code=422,
            content={"detail": ErrorDetail(
                code="invalid_request",
                message="request validation failed",
            ).model_dump()},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "")
        logger.exception("unhandled_error request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content={"detail": ErrorDetail(
                code="internal_error",
                message="internal server error",
            ).model_dump()},
        )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request):
        engine: AsrEngine | None = request.app.state.engine
        ready = bool(engine and engine.ready)
        payload = HealthResponse(
            status="ok" if ready else "loading",
            version=app.version,
            model_loaded=ready,
            model=settings.asr.model,
            uptime_seconds=round(time.time() - request.app.state.started_at, 1),
            active_requests=request.app.state.active_requests,
            total_requests=request.app.state.total_requests,
            rejected_requests=request.app.state.rejected_requests,
        )
        if not ready:
            return JSONResponse(status_code=503, content=payload.model_dump())
        return payload

    @app.get("/api/asr/models", response_model=ModelsResponse, tags=["asr"])
    async def list_models(request: Request):
        engine: AsrEngine | None = request.app.state.engine
        engine_ready = bool(engine and engine.ready)
        return ModelsResponse(models=[ModelCard(
            id=settings.asr.model,
            provider=settings.asr.provider,
            loaded=engine_ready,
            languages=settings.asr.languages,
            num_threads=settings.asr.num_threads,
            load_ms=round(engine.load_ms, 1) if engine else 0.0,
        )])

    @app.post("/api/asr/transcribe", response_model=TranscribeResponse, tags=["asr"])
    async def transcribe(
        request: Request,
        file: Annotated[UploadFile, File(description="WAV / WebM / other audio file")],
    ):
        state = request.app.state
        engine: AsrEngine | None = state.engine
        request_id = getattr(request.state, "request_id", "")
        if not engine or not engine.ready:
            raise _api_error(503, "model_not_ready", "model is not loaded yet")

        state.total_requests += 1

        # Basic per-client frequency limit. Nginx also enforces a per-IP limit.
        retry_after = await state.rate_limiter.check(
            _client_ip(request, settings.server.trust_proxy_headers)
        )
        if retry_after is not None:
            state.rejected_requests += 1
            raise _api_error(
                429,
                "rate_limited",
                "too many transcription requests, retry later",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.server.max_upload_bytes:
                    raise _api_error(413, "payload_too_large", "upload exceeds the size limit")
            except ValueError:
                raise _api_error(400, "invalid_request", "invalid Content-Length header")

        started = time.perf_counter()
        acquired = False
        try:
            try:
                async with asyncio.timeout(settings.server.queue_timeout_seconds):
                    await state.semaphore.acquire()
                acquired = True
            except TimeoutError:
                state.rejected_requests += 1
                raise _api_error(
                    503,
                    "server_busy",
                    "too many concurrent requests",
                    headers={"Retry-After": "1"},
                )

            state.active_requests += 1
            data = bytearray()
            try:
                while True:
                    chunk = await file.read(READ_CHUNK)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > settings.server.max_upload_bytes:
                        raise _api_error(413, "payload_too_large", "upload exceeds the size limit")
            finally:
                await file.close()

            if not data:
                raise _api_error(422, "empty_audio", "upload is empty")

            payload = bytes(data)

            def process(audio_bytes: bytes):
                decode_started = time.perf_counter()
                decoded = decode_audio(
                    audio_bytes,
                    max_seconds=settings.server.max_audio_seconds,
                )
                decode_ms = (time.perf_counter() - decode_started) * 1000
                transcript = engine.transcribe(decoded.samples)
                return decoded, transcript, decode_ms

            loop = asyncio.get_running_loop()
            try:
                decoded, transcript, decode_ms = await asyncio.wait_for(
                    loop.run_in_executor(state.executor, process, payload),
                    timeout=settings.server.request_timeout_seconds,
                )
            except TimeoutError:
                raise _api_error(504, "recognition_timeout", "recognition timed out")

            total_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "transcribe request_id=%s bytes=%d duration=%.2fs decode_ms=%.1f "
                "elapsed_ms=%.1f total_ms=%.1f rtf=%.4f lang=%s chars=%d",
                request_id,
                len(payload),
                decoded.duration,
                decode_ms,
                transcript.elapsed_ms,
                total_ms,
                transcript.rtf,
                transcript.language or "auto",
                len(transcript.text),
            )
            return TranscribeResponse(
                text=transcript.text,
                duration=decoded.duration,
                elapsed_ms=transcript.elapsed_ms,
                decode_ms=round(decode_ms, 2),
                total_ms=round(total_ms, 2),
                rtf=transcript.rtf,
                model=settings.asr.model,
                language=transcript.language,
                request_id=request_id,
            )

        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            logger.info(
                "transcribe_rejected request_id=%s status=%d code=%s",
                request_id, exc.status_code, detail.get("code", "error"),
            )
            raise
        except EmptyAudioError as exc:
            raise _api_error(422, "empty_audio", str(exc)) from exc
        except AudioTooLongError as exc:
            raise _api_error(422, "audio_too_long", str(exc)) from exc
        except UnsupportedAudioError as exc:
            raise _api_error(415, "unsupported_audio", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("transcribe_failed request_id=%s", request_id)
            raise _api_error(500, "internal_error", "recognition failed") from exc
        finally:
            if acquired:
                state.active_requests = max(0, state.active_requests - 1)
                state.semaphore.release()

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
