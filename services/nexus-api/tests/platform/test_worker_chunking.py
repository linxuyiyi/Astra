"""Worker 响应分片按 UTF-8 字节宽度线性切分，拼回后与原文一致。"""

from __future__ import annotations

import time

from app.worker_agent.local_opencode import _chunks
from app.worker_gateway.protocol import MAX_CHUNK_BYTES


def _assert_roundtrip(text: str) -> list[str]:
    started = time.perf_counter()
    pieces = _chunks(text)
    elapsed = time.perf_counter() - started
    assert "".join(pieces) == text
    assert all(len(piece.encode("utf-8")) <= MAX_CHUNK_BYTES for piece in pieces)
    if len(text.encode("utf-8")) > MAX_CHUNK_BYTES:
        assert len(pieces) > 1
        assert max(len(piece.encode("utf-8")) for piece in pieces[:-1]) > MAX_CHUNK_BYTES - 4
    assert elapsed < 2
    return pieces


def test_chunks_600kb_ascii_stay_under_the_cap():
    _assert_roundtrip("a" * 600_000)


def test_chunks_7mb_ascii_is_linear():
    pieces = _assert_roundtrip("x" * 7_000_000)
    assert all(len(piece.encode("utf-8")) == MAX_CHUNK_BYTES for piece in pieces[:-1])


def test_chunks_mixed_utf8_2mb_roundtrip_without_splitting_codepoints():
    unit = "A你😀β"
    text = unit * (2_000_000 // len(unit.encode("utf-8")) + 1)
    pieces = _assert_roundtrip(text)
    assert len("".join(pieces).encode("utf-8")) == len(text.encode("utf-8"))


def test_chunk_boundary_keeps_a_multibyte_character_intact():
    head = "a" * (MAX_CHUNK_BYTES - 1)
    text = head + "你" + "b😀"
    pieces = _chunks(text)
    assert pieces[0] == head
    assert pieces[1].startswith("你")
    assert "".join(pieces) == text
    assert all(len(piece.encode("utf-8")) <= MAX_CHUNK_BYTES for piece in pieces)


def test_empty_text_has_no_chunks():
    assert _chunks("") == []
