from __future__ import annotations

from pydantic import BaseModel, Field


class ModelCard(BaseModel):
    id: str
    provider: str
    loaded: bool
    languages: list[str]
    sample_rate: int = 16000
    num_threads: int
    load_ms: float


class ModelsResponse(BaseModel):
    models: list[ModelCard]


class HealthResponse(BaseModel):
    status: str
    service: str = "nexus-asr"
    version: str
    model_loaded: bool
    model: str
    uptime_seconds: float
    active_requests: int
    total_requests: int
    rejected_requests: int


class TranscribeResponse(BaseModel):
    text: str
    duration: float = Field(description="Audio duration in seconds after decode")
    elapsed_ms: float = Field(description="Inference time")
    decode_ms: float = Field(description="Audio decode + resample time")
    total_ms: float = Field(description="Server-side total time")
    rtf: float
    model: str
    language: str
    request_id: str


class ErrorDetail(BaseModel):
    code: str
    message: str
