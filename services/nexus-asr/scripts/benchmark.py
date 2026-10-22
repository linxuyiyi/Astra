#!/usr/bin/env python3
"""Measure real SenseVoiceSmall latency, CER, gateway overhead and resources.

The script talks to the running ASR service and the running Nexus gateway. It
never mocks inference. Run it after both services are up:

    .venv/bin/python scripts/benchmark.py --output results/perf-v1.5.0.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
BENCH = ROOT / "testdata" / "bench"
PUNCT = set("，。！？、；：,.!?;:\"“”‘’（）()[]【】<>《》—–-·… \t\r\n")


def normalize(text: str) -> str:
    text = text.lower()
    return "".join(char for char in text if char not in PUNCT)


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (a != b),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return round(edit_distance(ref, hyp) / len(ref), 4)


def read_gateway_key() -> str:
    key = os.environ.get("NEXUS_GATEWAY_API_KEY", "").strip()
    if key:
        return key
    env = REPO / "ops" / "env" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("NEXUS_GATEWAY_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


class ResourceSampler:
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.samples: list[tuple[float, float]] = []
        self.rss: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = None

    def __enter__(self):
        if not self.pid:
            return self
        try:
            import psutil
        except ImportError:
            return self
        try:
            self._process = psutil.Process(self.pid)
            self._process.cpu_percent(None)
        except Exception:  # noqa: BLE001
            self._process = None
            return self

        def loop():
            while not self._stop.is_set():
                try:
                    self.samples.append((time.perf_counter(), self._process.cpu_percent(None)))
                    self.rss.append(self._process.memory_info().rss)
                except Exception:  # noqa: BLE001
                    return
                self._stop.wait(0.1)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def summary(self) -> dict:
        if not self.samples:
            return {}
        cpu = [value for _, value in self.samples if value >= 0]
        return {
            "cpu_percent_avg": round(statistics.mean(cpu), 1) if cpu else None,
            "cpu_percent_max": round(max(cpu), 1) if cpu else None,
            "rss_mb_start": round(min(self.rss) / 1024 / 1024, 1) if self.rss else None,
            "rss_mb_peak": round(max(self.rss) / 1024 / 1024, 1) if self.rss else None,
            "samples": len(cpu),
        }


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "p50": round(statistics.median(values), 2),
    }


def asr_pid() -> int | None:
    path = ROOT / ".run" / "nexus-asr.pid"
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def post(client: httpx.Client, url: str, path: Path | bytes, filename: str, headers: dict | None = None):
    content = path.read_bytes() if isinstance(path, Path) else path
    started = time.perf_counter()
    response = client.post(
        url,
        data={"model": "sensevoice-small", "response_format": "json"},
        files={"file": (filename, content, "audio/wav")},
        headers=headers or {},
    )
    wall_ms = (time.perf_counter() - started) * 1000
    return response, wall_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", default="http://127.0.0.1:19899")
    parser.add_argument("--gateway", default="http://127.0.0.1:18110/v1")
    parser.add_argument("--gateway-key", default="")
    parser.add_argument("--output", default=str(ROOT / "results" / "perf-v1.5.0.json"))
    parser.add_argument("--hot-runs", type=int, default=10)
    args = parser.parse_args()

    key = args.gateway_key or read_gateway_key()
    refs = json.loads((BENCH / "refs.json").read_text())
    refs.update({
        item["id"]: {"text": item["text"], "kind": item["kind"], "file": item.get("file_16k") or f"{item['id']}.wav"}
        for item in json.loads((ROOT / "testdata" / "refs.json").read_text())
    })
    cases = [
        ("short-3s", BENCH / "short-3s.wav", args.hot_runs),
        ("zh-5s", ROOT / "testdata" / "zh-5s.wav", args.hot_runs),
        ("zh-10s", ROOT / "testdata" / "zh-10s.wav", 3),
        ("long-30s", BENCH / "long-30s.wav", 2),
        ("mixed-1", ROOT / "testdata" / "mixed-1.wav", 2),
        ("mixed-2", ROOT / "testdata" / "mixed-2.wav", 2),
        ("business-4s", BENCH / "business-4s.wav", 3),
    ]
    abnormal = [
        ("silence", BENCH / "silence-3s.wav"),
        ("noise", BENCH / "noise-3s.wav"),
        ("empty", Path("/tmp/nexus-asr-empty.bin")),
        ("invalid", Path("/tmp/nexus-asr-invalid.bin")),
    ]
    Path("/tmp/nexus-asr-empty.bin").write_bytes(b"")
    Path("/tmp/nexus-asr-invalid.bin").write_bytes(b"this is not audio")

    timeout = httpx.Timeout(120, connect=5)
    result: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "asr_base": args.asr,
        "gateway_base": args.gateway,
        "gateway_key_configured": bool(key),
        "hot_runs": args.hot_runs,
        "cases": {},
        "abnormal": {},
        "gateway_overhead": {},
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with httpx.Client(timeout=timeout) as client:
        # warm both paths once so the first case is not a cold process
        post(client, args.asr + "/v1/audio/transcriptions", BENCH / "short-3s.wav", "warm.wav")
        if key:
            post(client, args.gateway + "/audio/transcriptions", BENCH / "short-3s.wav", "warm.wav", headers)

        for name, path, runs in cases:
            direct_wall, direct_total, direct_elapsed = [], [], []
            gateway_wall, gateway_total, gateway_elapsed, proxy_overhead = [], [], [], []
            transcripts: list[str] = []
            resources: list[dict] = []
            for _ in range(runs):
                with ResourceSampler(asr_pid()) as sampler:
                    response, wall = post(client, args.asr + "/api/asr/transcribe", path, path.name)
                    if response.status_code != 200:
                        raise RuntimeError(f"direct {name}: {response.status_code} {response.text[:200]}")
                    body = response.json()
                direct_wall.append(wall)
                direct_total.append(float(body["total_ms"]))
                direct_elapsed.append(float(body["elapsed_ms"]))
                transcripts.append(body["text"])
                resources.append(sampler.summary())
                if key:
                    with ResourceSampler(asr_pid()) as sampler:
                        response, wall = post(client, args.gateway + "/audio/transcriptions", path, path.name, headers)
                        if response.status_code != 200:
                            raise RuntimeError(f"gateway {name}: {response.status_code} {response.text[:200]}")
                        gateway_wall.append(wall)
                        gateway_total.append(float(response.headers.get("x-asr-total-ms", "0")))
                        gateway_elapsed.append(float(response.headers.get("x-asr-elapsed-ms", "0")))
                        gateway_asr_total = float(response.headers.get("x-asr-total-ms", "0"))
                        proxy_overhead.append(wall - gateway_asr_total)
                        gateway_text = response.json()["text"]
                        resources.append(sampler.summary())
            reference = refs.get(name, {}).get("text", "")
            hypothesis = transcripts[-1] if transcripts else ""
            entry = {
                "file": str(path.relative_to(ROOT)),
                "audio_seconds": round(float(body["duration"]), 3),
                "reference": reference,
                "transcript": hypothesis,
                "cer": cer(reference, hypothesis) if reference else None,
                "direct": {
                    "wall_ms": stats(direct_wall),
                    "asr_total_ms": stats(direct_total),
                    "inference_ms": stats(direct_elapsed),
                    "rtf": round(statistics.mean(direct_elapsed) / 1000 / float(body["duration"]), 4),
                },
                "resources": {
                    "cpu_percent_avg": round(statistics.mean(
                        [r["cpu_percent_avg"] for r in resources if r.get("cpu_percent_avg") is not None]
                    ), 1) if any(r.get("cpu_percent_avg") is not None for r in resources) else None,
                    "cpu_percent_max": max(
                        [r["cpu_percent_max"] for r in resources if r.get("cpu_percent_max") is not None],
                        default=None,
                    ),
                    "rss_mb_peak": max(
                        [r["rss_mb_peak"] for r in resources if r.get("rss_mb_peak") is not None],
                        default=None,
                    ),
                },
            }
            if key:
                entry["gateway"] = {
                    "wall_ms": stats(gateway_wall),
                    "asr_total_ms": stats(gateway_total),
                    "inference_ms": stats(gateway_elapsed),
                    "proxy_overhead_ms": stats(proxy_overhead),
                    "wall_overhead_ms": round(
                        statistics.mean(gateway_wall) - statistics.mean(direct_wall), 2
                    ),
                    "asr_total_overhead_ms": round(
                        statistics.mean(gateway_total) - statistics.mean(direct_total), 2
                    ),
                    "transcript": gateway_text,
                }
                result["gateway_overhead"][name] = entry["gateway"]["proxy_overhead_ms"].get("p50")
            result["cases"][name] = entry

        for name, path in abnormal:
            response, wall = post(client, args.asr + "/v1/audio/transcriptions", path, path.name)
            gateway_response = None
            if key:
                gateway_response, _ = post(client, args.gateway + "/audio/transcriptions", path, path.name, headers)
            result["abnormal"][name] = {
                "direct_status": response.status_code,
                "direct_body": response.text[:200],
                "direct_ms": round(wall, 2),
                "gateway_status": gateway_response.status_code if gateway_response else None,
                "gateway_body": gateway_response.text[:200] if gateway_response else None,
            }

        # Oversized upload: 12 MiB + 1 byte, no audio decode.
        oversized = b"0" * (13 * 1024 * 1024)
        response, wall = post(client, args.asr + "/api/asr/transcribe", oversized, "big.wav")
        gateway_response = None
        if key:
            gateway_response, _ = post(client, args.gateway + "/audio/transcriptions", oversized, "big.wav", headers)
        result["abnormal"]["oversized"] = {
            "direct_status": response.status_code,
            "direct_body": response.text[:200],
            "direct_ms": round(wall, 2),
            "gateway_status": gateway_response.status_code if gateway_response else None,
            "gateway_body": gateway_response.text[:200] if gateway_response else None,
        }

        models = client.get(args.asr + "/api/asr/models").json()["models"][0]
        result["model"] = models
        try:
            import psutil
            memory = psutil.virtual_memory()
            result["system"] = {
                "total_mb": round(memory.total / 1024 / 1024),
                "used_mb": round(memory.used / 1024 / 1024),
                "available_mb": round(memory.available / 1024 / 1024),
            }
        except Exception:  # noqa: BLE001
            pass

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
