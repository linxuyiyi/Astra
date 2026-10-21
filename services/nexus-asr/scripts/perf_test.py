#!/usr/bin/env python3
"""Real HTTP performance test for Nexus ASR Server (Mac / Linux).

Covers:
- 5 s / 10 s / 30 s Chinese WAV
- mixed Chinese/English WAV
- official human-recorded zh/en WAV
- WebM/Opus (the format Chrome MediaRecorder produces)
- 20 sequential requests
- 8 concurrent requests
- CPU time and RSS of the server process during a 30 s request

Usage:
    python scripts/perf_test.py --base http://127.0.0.1:19899 \
        --output results/perf-mac.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OFFICIAL = {
    "official-zh": "开放时间早上9点至下午5点。",
    "official-en": "The tribal chieftain called for the boy and presented him with 50 pieces of code.",
}


def normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower(), flags=re.UNICODE)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (0 if ca == cb else 1),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    return levenshtein(ref, hyp) / len(ref) if ref else 0.0


def parse_ps_time(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return float(value)


def find_server_pid() -> int | None:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,command="], text=True, timeout=10
    )
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        if "uvicorn" in command and "app.main:app" in command and "19899" in command:
            return int(pid_text)
    return None


def snapshot(pid: int) -> tuple[int, float]:
    text = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "rss=,time="], text=True, timeout=10
    ).strip()
    rss, cpu = text.split()
    return int(rss), parse_ps_time(cpu)


def make_webm(wav_path: Path, out_path: Path) -> None:
    import av

    with av.open(str(wav_path)) as inp, av.open(str(out_path), "w") as out:
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


class Runner:
    def __init__(self, base: str, timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self.refs = {
            item["id"]: item["text"]
            for item in json.loads((ROOT / "testdata/refs.json").read_text())
        }

    def close(self) -> None:
        self.client.close()

    def post_bytes(self, data: bytes, filename: str, mime: str) -> tuple[dict, float, int]:
        started = time.perf_counter()
        response = self.client.post(
            f"{self.base}/api/asr/transcribe",
            files={"file": (filename, data, mime)},
        )
        wall_ms = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:300]}
        return body, wall_ms, response.status_code

    def post_file(self, path: Path, mime: str = "audio/wav") -> tuple[dict, float, int]:
        return self.post_bytes(path.read_bytes(), path.name, mime)


def summarize(runs: list[dict]) -> dict:
    totals = sorted(item["server_total_ms"] for item in runs)
    walls = sorted(item["client_wall_ms"] for item in runs)
    return {
        "count": len(runs),
        "server_total_p50_ms": round(statistics.median(totals), 1),
        "server_total_min_ms": round(totals[0], 1),
        "server_total_max_ms": round(totals[-1], 1),
        "client_wall_p50_ms": round(statistics.median(walls), 1),
        "inference_p50_ms": round(statistics.median([r["inference_ms"] for r in runs]), 1),
        "decode_p50_ms": round(statistics.median([r["decode_ms"] for r in runs]), 1),
        "rtf_p50": round(statistics.median([r["rtf"] for r in runs]), 4),
        "p95_server_total_ms": round(totals[min(len(totals) - 1, math.ceil(0.95 * len(totals)) - 1)], 1),
    }


def run_case(runner: Runner, case_id: str, path: Path, runs: int, mime: str = "audio/wav") -> dict:
    reference = runner.refs.get(case_id, OFFICIAL.get(case_id, ""))
    results = []
    for _ in range(runs):
        body, wall, status = runner.post_file(path, mime)
        if status != 200:
            results.append({"status": status, "error": body})
            continue
        results.append({
            "status": status,
            "text": body.get("text", ""),
            "duration": body.get("duration"),
            "server_total_ms": body.get("total_ms"),
            "inference_ms": body.get("elapsed_ms"),
            "decode_ms": body.get("decode_ms"),
            "rtf": body.get("rtf"),
            "language": body.get("language"),
            "client_wall_ms": wall,
            "cer": cer(reference, body.get("text", "")),
        })
    ok = [item for item in results if item["status"] == 200]
    return {
        "id": case_id,
        "file": path.name,
        "mime": mime,
        "reference": reference,
        "runs": results,
        "summary": summarize(ok) if ok else None,
    }


async def concurrency_test(base: str, data: bytes, count: int) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        async def one(index: int):
            started = time.perf_counter()
            response = await client.post(
                f"{base}/api/asr/transcribe",
                files={"file": (f"recording-{index}.wav", data, "audio/wav")},
            )
            wall = (time.perf_counter() - started) * 1000
            return {
                "index": index,
                "status": response.status_code,
                "wall_ms": round(wall, 1),
                "body": response.json() if response.status_code == 200 else response.text[:200],
            }

        started = time.perf_counter()
        results = await asyncio.gather(*(one(i) for i in range(count)))
        wall = (time.perf_counter() - started) * 1000

    successes = [item for item in results if item["status"] == 200]
    return {
        "count": count,
        "wall_ms": round(wall, 1),
        "successes": len(successes),
        "errors": [item for item in results if item["status"] != 200],
        "p50_wall_ms": round(statistics.median([item["wall_ms"] for item in successes]), 1) if successes else None,
        "max_wall_ms": round(max((item["wall_ms"] for item in successes), default=0), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:19899")
    parser.add_argument("--output", default=str(ROOT / "results" / "perf-mac.json"))
    args = parser.parse_args()

    base = args.base.rstrip("/")
    runner = Runner(base)
    try:
        health = runner.client.get(f"{base}/health").json()
        models = runner.client.get(f"{base}/api/asr/models").json()
        print("health:", health)
        print("models:", models)
        if health.get("status") != "ok":
            print("server not ready", file=sys.stderr)
            return 1

        # Warmup (also confirms the thread pool / ONNX arenas are warm).
        for _ in range(2):
            runner.post_file(ROOT / "testdata/zh-5s.wav")

        cases = [
            ("zh-5s", ROOT / "testdata/zh-5s.wav", 5, "audio/wav"),
            ("zh-10s", ROOT / "testdata/zh-10s.wav", 3, "audio/wav"),
            ("zh-30s", ROOT / "testdata/zh-30s.wav", 3, "audio/wav"),
            ("mixed-1", ROOT / "testdata/mixed-1.wav", 2, "audio/wav"),
            ("mixed-2", ROOT / "testdata/mixed-2.wav", 2, "audio/wav"),
            ("official-zh", ROOT / "testdata/official/zh.wav", 2, "audio/wav"),
            ("official-en", ROOT / "testdata/official/en.wav", 2, "audio/wav"),
        ]

        generated = ROOT / "testdata" / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        webm_path = generated / "zh-5s.webm"
        make_webm(ROOT / "testdata/zh-5s.wav", webm_path)
        cases.append(("zh-5s", webm_path, 3, "audio/webm"))

        results = []
        for case_id, path, runs, mime in cases:
            entry = run_case(runner, case_id, path, runs, mime)
            results.append(entry)
            summary = entry["summary"]
            label = f"{case_id} [{mime.split('/')[-1]}]"
            if summary:
                print(
                    f"{label:22} n={summary['count']} "
                    f"total p50={summary['server_total_p50_ms']}ms "
                    f"max={summary['server_total_max_ms']}ms "
                    f"infer={summary['inference_p50_ms']}ms "
                    f"rtf={summary['rtf_p50']} "
                    f"cer={statistics.median([r['cer'] for r in entry['runs']]):.3f}"
                )

        # 20 sequential 5 s requests.
        sequential = []
        for _ in range(20):
            body, wall, status = runner.post_file(ROOT / "testdata/zh-5s.wav")
            sequential.append({
                "status": status,
                "server_total_ms": body.get("total_ms"),
                "wall_ms": round(wall, 1),
            })
        ok_seq = [item for item in sequential if item["status"] == 200]
        sequential_summary = {
            "count": 20,
            "successes": len(ok_seq),
            "errors": [item for item in sequential if item["status"] != 200],
            "p50_wall_ms": round(statistics.median([item["wall_ms"] for item in ok_seq]), 1) if ok_seq else None,
            "p95_wall_ms": round(sorted(item["wall_ms"] for item in ok_seq)[int(0.95 * len(ok_seq)) - 1], 1) if ok_seq else None,
            "max_wall_ms": round(max((item["wall_ms"] for item in ok_seq), default=0), 1),
        }
        print("sequential 20:", sequential_summary)

        concurrency = asyncio.run(
            concurrency_test(base, (ROOT / "testdata/zh-5s.wav").read_bytes(), 8)
        )
        print("concurrency 8:", concurrency)

        # Resource usage during a 30 s request.
        pid = find_server_pid()
        resources = {"pid": pid}
        if pid:
            rss_idle, cpu_idle = snapshot(pid)
            stop = threading.Event()
            samples: list[tuple[int, float]] = []

            def sampler() -> None:
                while not stop.wait(0.1):
                    try:
                        samples.append(snapshot(pid))
                    except Exception:
                        pass

            thread = threading.Thread(target=sampler, daemon=True)
            thread.start()
            started = time.perf_counter()
            body, wall, status = runner.post_file(ROOT / "testdata/zh-30s.wav")
            elapsed = time.perf_counter() - started
            stop.set()
            thread.join(timeout=2)
            rss_end, cpu_end = snapshot(pid)
            cpu_used = max(0.0, cpu_end - cpu_idle)
            peak_rss = max([rss_idle] + [item[0] for item in samples] + [rss_end])
            resources.update({
                "idle_rss_mb": round(rss_idle / 1024, 1),
                "peak_rss_mb": round(peak_rss / 1024, 1),
                "cpu_seconds": round(cpu_used, 3),
                "wall_seconds": round(elapsed, 3),
                "cpu_percent_one_core": round(cpu_used / elapsed * 100, 1),
                "status": status,
                "server_total_ms": body.get("total_ms"),
            })
        print("resources:", resources)

        payload = {
            "meta": {
                "base": base,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "health": health,
                "models": models,
            },
            "cases": results,
            "sequential_20": sequential_summary,
            "concurrency_8": concurrency,
            "resources": resources,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print("wrote", output)
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
