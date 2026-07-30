"""Benchmark Moonshine vs SenseVoice (funasr_onnx) on LibriSpeech audio.

Runs both through the real agent_hub provider interfaces, so the numbers
reflect what the server actually executes, including SenseVoice tag parsing.

Needs network (fetches the corpus) and pyarrow, which is not a project
dependency:

    uv pip install pyarrow
    uv run --extra full python scripts/bench_asr.py

Env vars:
    BENCH_ARCHS   comma-separated Moonshine archs to test (default: tiny)
    BENCH_LIMIT   number of utterances (default: 73, the whole set)

Constrain CPU to model a droplet:

    docker run --rm --cpus 1 -v $PWD/scripts/bench_asr.py:/app/bench.py:ro \
        --entrypoint sh agent-hub-agent-hub:latest \
        -c "uv pip install pyarrow -q && uv run --no-sync python /app/bench.py"

Caveat: LibriSpeech is clean, read audiobook speech. Absolute WER on ESP32
mic audio in a room will be worse for every provider; treat these as a
relative ranking, not a prediction of field accuracy.
"""

import asyncio
import io
import os
import re
import statistics
import sys
import time

import pyarrow.parquet as pq
import scipy.signal as ss
import soundfile as sf
from huggingface_hub import hf_hub_download

from agent_hub.providers.asr import get_provider

LIMIT = int(os.environ.get("BENCH_LIMIT", "73"))


def normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation, collapse whitespace -> word list.

    LibriSpeech references are uppercase and unpunctuated; SenseVoice emits
    cased, punctuated text. Without this the comparison measures formatting.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.split()


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """Levenshtein edit distance over words -> (errors, ref_length)."""
    if not ref:
        return (len(hyp), 0)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return (prev[-1], len(ref))


def load_samples(limit: int) -> list[tuple[bytes, str, float]]:
    """Return (wav_bytes_16k, reference_text, duration_seconds) tuples."""
    path = hf_hub_download(
        "hf-internal-testing/librispeech_asr_dummy",
        "clean/validation-00000-of-00001.parquet",
        repo_type="dataset",
    )
    table = pq.read_table(path).to_pylist()
    out = []
    for row in table[:limit]:
        audio, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            audio = ss.resample_poly(audio, 16000, sr)
        buf = io.BytesIO()
        sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
        out.append((buf.getvalue(), row["text"], len(audio) / 16000))
    return out


async def run(name: str, config: dict, samples: list) -> None:
    provider = get_provider(name, config)
    # Warm up so model load doesn't land in the first measurement.
    await provider.transcribe(samples[0][0])

    errors = total = 0
    latencies = []
    audio_total = 0.0
    for wav, ref, dur in samples:
        t0 = time.perf_counter()
        result = await provider.transcribe(wav)
        latencies.append(time.perf_counter() - t0)
        e, n = wer(normalize(ref), normalize(result.text))
        errors += e
        total += n
        audio_total += dur

    lat_sum = sum(latencies)
    print(f"\n=== {name} ===")
    print(f"  utterances     {len(samples)}  ({audio_total:.1f}s of audio)")
    print(f"  WER            {100 * errors / total:.2f}%  ({errors} errors / {total} words)")
    print(f"  median latency {statistics.median(latencies):.3f}s")
    print(f"  p90 latency    {sorted(latencies)[int(0.9 * len(latencies))]:.3f}s")
    print(f"  real-time factor {lat_sum / audio_total:.3f}")


async def main() -> None:
    samples = load_samples(LIMIT)
    print(f"cpus visible: {os.cpu_count()}  samples: {len(samples)}", file=sys.stderr)
    for arch in os.environ.get("BENCH_ARCHS", "tiny").split(","):
        # get_provider caches by provider name, so the second arch would
        # otherwise get the first arch's model back.
        from agent_hub.providers import asr as asr_mod

        asr_mod._cache.clear()
        await run(
            "moonshine",
            {
                "asr": {
                    "moonshine": {
                        "language": "en",
                        "model_arch": arch,
                        "cache_root": "models/moonshine",
                    }
                }
            },
            samples,
        )
    await run(
        "funasr_onnx",
        {"asr": {"funasr_onnx": {"model_dir": "models/SenseVoiceSmall-onnx"}}},
        samples,
    )


asyncio.run(main())
