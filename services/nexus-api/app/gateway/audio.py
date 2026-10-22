"""有界、内存中的 multipart 音频请求；不使用 UploadFile 的磁盘 spool。"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import httpx
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from app.gateway.errors import invalid_request

AUDIO_PATH = "/v1/audio/transcriptions"
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_AUDIO_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024


@dataclass
class AudioUpload:
    fields: dict[str, str]
    filename: str
    content_type: str
    audio: bytes

    def encode(self, model: str) -> tuple[bytes, str]:
        request = httpx.Request(
            "POST", "http://multipart.invalid",
            data={**self.fields, "model": model},
            files={"file": (self.filename, self.audio, self.content_type)},
        )
        return request.read(), request.headers["content-type"]


def parse_audio_upload(body: bytes, content_type: str) -> AudioUpload:
    media, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise invalid_request("语音识别需要 multipart/form-data 和 file、model")
    parts: list[tuple[dict[bytes, bytes], bytes]] = []
    headers: dict[bytes, bytes] = {}
    header_name = bytearray()
    header_value = bytearray()
    data = bytearray()
    finished = False

    def begin():
        if len(parts) >= 8:
            raise invalid_request("音频表单字段过多")
        headers.clear()
        data.clear()

    def header_end():
        headers[bytes(header_name).lower()] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def part_end():
        parts.append((dict(headers), bytes(data)))

    def end():
        nonlocal finished
        finished = True

    parser = MultipartParser(boundary, {
        "on_part_begin": begin,
        "on_header_field": lambda d, s, e: header_name.extend(d[s:e]),
        "on_header_value": lambda d, s, e: header_value.extend(d[s:e]),
        "on_header_end": header_end,
        "on_part_data": lambda d, s, e: data.extend(d[s:e]),
        "on_part_end": part_end,
        "on_end": end,
    })
    try:
        parser.write(body)
        parser.finalize()
        if not finished:
            raise ValueError("incomplete multipart")
        fields: dict[str, str] = {}
        audio = None
        for part_headers, content in parts:
            _, params = parse_options_header(part_headers.get(b"content-disposition", b""))
            name = params.get(b"name", b"").decode("utf-8")
            if name == "file":
                if audio is not None or b"filename" not in params:
                    raise ValueError("duplicate or invalid file")
                audio = (
                    params[b"filename"].decode("utf-8") or "recording.wav",
                    part_headers.get(b"content-type", b"application/octet-stream").decode("ascii"),
                    content,
                )
            else:
                if name not in {"model", "language", "prompt", "temperature", "response_format", "stream"}:
                    raise ValueError("unsupported field")
                if name in fields or len(content) > 16 * 1024 or b"filename" in params:
                    raise ValueError("duplicate or invalid field")
                fields[name] = content.decode("utf-8")
        if audio is None or not audio[2]:
            raise ValueError("missing or empty audio")
        if len(audio[2]) > MAX_AUDIO_BYTES:
            from app.gateway.errors import payload_too_large
            raise payload_too_large("音频超过 12 MiB 上限")
        if fields.get("stream", "false").lower() not in {"false", "0"}:
            raise ValueError("stream unsupported")
        if fields.get("response_format", "json") != "json":
            raise ValueError("only json response supported")
        return AudioUpload(fields, *audio)
    except (ValueError, UnicodeError, MultipartParseError) as exc:
        raise invalid_request("音频表单不合法；仅支持单个 file 和非流式 JSON 转写") from exc


def probe_audio() -> bytes:
    """半秒静音用于检查真实解码/转写协议，不含用户音频。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 16000)
    return output.getvalue()
