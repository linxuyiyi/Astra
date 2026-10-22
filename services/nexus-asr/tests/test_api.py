from __future__ import annotations

import io
import wave

from fastapi.testclient import TestClient

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def wav_bytes(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def silent_wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def test_health(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model"] == "sensevoice-small"
    assert body["uptime_seconds"] >= 0


def test_models(client: TestClient):
    response = client.get("/api/asr/models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) == 1
    assert models[0]["loaded"] is True
    assert models[0]["provider"] == "funasr"
    assert "zh" in models[0]["languages"]


def test_openai_models(client: TestClient):
    response = client.get("/v1/models")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert "sensevoice-small" in ids
    assert "sensevoice" in ids


def test_openai_transcribe_wav(client: TestClient):
    files = {"file": ("zh-5s.wav", wav_bytes("testdata/zh-5s.wav"), "audio/wav")}
    response = client.post(
        "/v1/audio/transcriptions",
        data={"model": "sensevoice", "response_format": "json"},
        files=files,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "会议纪要" in body["text"]
    assert "<|" not in body["text"]
    assert set(body) == {"text"}
    assert response.headers["x-asr-elapsed-ms"]
    assert response.headers["x-asr-total-ms"]
    assert response.headers["x-asr-duration"].startswith("5.0")


def test_openai_rejects_unknown_model(client: TestClient):
    files = {"file": ("zh-5s.wav", b"not-audio", "audio/wav")}
    response = client.post(
        "/v1/audio/transcriptions",
        data={"model": "unknown", "response_format": "json"},
        files=files,
    )
    assert response.status_code == 404


def test_openai_rejects_verbose_json(client: TestClient):
    files = {"file": ("zh-5s.wav", b"not-audio", "audio/wav")}
    response = client.post(
        "/v1/audio/transcriptions",
        data={"model": "sensevoice", "response_format": "verbose_json"},
        files=files,
    )
    assert response.status_code == 400


def test_transcribe_wav(client: TestClient):
    files = {"file": ("zh-5s.wav", wav_bytes("testdata/zh-5s.wav"), "audio/wav")}
    response = client.post("/api/asr/transcribe", files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "会议纪要" in body["text"]
    assert 4.9 < body["duration"] < 5.2
    assert body["rtf"] < 1
    assert body["total_ms"] < 5000
    assert body["request_id"]
    assert response.headers["x-request-id"] == body["request_id"]


def test_transcribe_webm(client: TestClient):
    # Generate a WebM/Opus copy of the same clip with PyAV so the API path is
    # exercised with the format Chrome MediaRecorder produces.
    import av
    import tempfile
    from pathlib import Path

    source = ROOT / "testdata" / "zh-5s.wav"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "zh-5s.webm"
        with av.open(str(source)) as inp, av.open(str(target), "w") as out:
            stream = out.add_stream("libopus", rate=48000, layout="mono")
            stream.bit_rate = 32000
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            for frame in inp.decode(audio=0):
                for converted in resampler.resample(frame):
                    for packet in stream.encode(converted):
                        out.mux(packet)
            for converted in resampler.resample(None):
                for packet in stream.encode(converted):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        data = target.read_bytes()

    response = client.post(
        "/api/asr/transcribe",
        files={"file": ("recording.webm", data, "audio/webm")},
    )
    assert response.status_code == 200, response.text
    assert "会议纪要" in response.json()["text"]


def test_empty_upload(client: TestClient):
    response = client.post(
        "/api/asr/transcribe",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "empty_audio"


def test_invalid_audio(client: TestClient):
    response = client.post(
        "/api/asr/transcribe",
        files={"file": ("x.wav", b"this is not audio", "audio/wav")},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_audio"


def test_audio_too_long(client: TestClient):
    data = silent_wav(61.0)
    response = client.post(
        "/api/asr/transcribe",
        files={"file": ("long.wav", data, "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "audio_too_long"


def test_upload_too_large(client: TestClient):
    # 12 MB + 1; rejected from Content-Length before decoding.
    payload = b"\x00" * (12 * 1024 * 1024 + 1)
    response = client.post(
        "/api/asr/transcribe",
        files={"file": ("big.wav", payload, "audio/wav")},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "payload_too_large"


def test_missing_file(client: TestClient):
    response = client.post("/api/asr/transcribe")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
