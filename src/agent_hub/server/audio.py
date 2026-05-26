"""Opus codec helpers and VAD for the WebSocket voice session.

The device sends raw Opus packets (no container). Each packet is one
WS binary frame. The server decodes them to PCM for ASR, then encodes
TTS PCM output back to Opus for playback on the device.

Opus sample rates used in this project:
  16000 Hz — typical device input (updated from ClientHello)
  24000 Hz — TTS output (both Edge TTS and KittenTTS targets)

Frame duration:
  Device uplink (mic): 60 ms / 960 samples at 16 kHz (XIAO ESP32-S3 default)
  TTS downlink: 20 ms / 480 samples at 24 kHz
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections import deque
from typing import Awaitable, Callable

import numpy as np
import opuslib_next as opuslib

_CHANNELS = 1
_ENCODER_FRAME_MS = 20  # TTS output always uses 20 ms frames


class OpusDecoder:
    """Decodes raw Opus packets to signed 16-bit PCM bytes."""

    def __init__(self, sample_rate: int = 16000, frame_duration_ms: int = 20) -> None:
        self._dec = opuslib.Decoder(sample_rate, _CHANNELS)
        self._frame_sz = sample_rate * frame_duration_ms // 1000

    def decode(self, packet: bytes) -> bytes:
        """Decode one Opus packet to PCM (int16 LE).

        Args:
            packet: Raw Opus packet as received from the device WS frame.

        Returns:
            PCM bytes: frame_samples * 2 bytes (int16 little-endian, mono).
        """
        return self._dec.decode(packet, self._frame_sz)  # type: ignore[return-value]


class OpusEncoder:
    """Encodes signed 16-bit PCM bytes to a sequence of Opus packets."""

    def __init__(self, sample_rate: int = 16000, frame_duration_ms: int = 60) -> None:
        self._enc = opuslib.Encoder(sample_rate, _CHANNELS, opuslib.APPLICATION_AUDIO)
        self._frame_sz = sample_rate * frame_duration_ms // 1000
        self._frame_bytes = self._frame_sz * _CHANNELS * 2  # 2 bytes per int16

    def encode(self, pcm_bytes: bytes) -> list[bytes]:
        """Encode PCM bytes into 20 ms Opus packets.

        Args:
            pcm_bytes: Raw PCM int16 LE bytes (any length; padded if needed).

        Returns:
            List of raw Opus packets ready to send as binary WS frames.
        """
        packets: list[bytes] = []
        for i in range(0, len(pcm_bytes), self._frame_bytes):
            frame = pcm_bytes[i : i + self._frame_bytes]
            if len(frame) < self._frame_bytes:
                frame = frame + b"\x00" * (self._frame_bytes - len(frame))
            packets.append(self._enc.encode(frame, self._frame_sz))  # type: ignore[arg-type]
        return packets


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM (int16 LE) bytes in a WAV container.

    Args:
        pcm_bytes: Raw signed 16-bit little-endian PCM.
        sample_rate: Sample rate in Hz.
        channels: Number of channels (default 1 = mono).

    Returns:
        RIFF WAV bytes suitable for sending to the OpenAI Whisper API.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


def rms_energy(pcm_bytes: bytes) -> float:
    """Calculate the RMS energy of PCM int16 bytes (higher = louder).

    Args:
        pcm_bytes: PCM int16 LE bytes.

    Returns:
        RMS value in the range [0, 32767].
    """
    if not pcm_bytes:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples**2)))


async def mp3_to_pcm(mp3_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Convert MP3 bytes to raw PCM (int16 LE) at sample_rate via ffmpeg.

    Used for Edge TTS output, which is always MP3. Requires ffmpeg on PATH
    (pre-installed in the Docker image).

    Args:
        mp3_bytes: Raw MP3 audio bytes.
        sample_rate: Target output sample rate in Hz.

    Returns:
        Raw PCM int16 LE bytes at sample_rate, mono.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero code.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "quiet",
        "-i", "pipe:0",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-f", "s16le",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pcm_bytes, _ = await proc.communicate(mp3_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode} while converting MP3 to PCM")
    return pcm_bytes


async def pcm_resample(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM int16 LE mono audio from from_rate to to_rate via ffmpeg.

    Args:
        pcm_bytes: Raw signed 16-bit little-endian mono PCM.
        from_rate: Input sample rate in Hz.
        to_rate: Output sample rate in Hz.

    Returns:
        Resampled PCM bytes at to_rate. Returns input unchanged if rates match.
    """
    if from_rate == to_rate:
        return pcm_bytes
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "quiet",
        "-f", "s16le", "-ar", str(from_rate), "-ac", "1", "-i", "pipe:0",
        "-ar", str(to_rate), "-ac", "1", "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate(pcm_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg resampler exited {proc.returncode}")
    return out


class SileroVAD:
    """Voice activity detector using the Silero VAD ONNX model via onnxruntime.

    Ported from the reference xiaozhi-esp32-server implementation.
    Processes 512-sample chunks at 16kHz with dual-threshold + sliding window.

    Usage::

        vad = SileroVAD(model_path="models/silero_vad.onnx")
        async for msg in ws:
            if "bytes" in msg:
                if vad.push(msg["bytes"]):
                    frames = vad.take()
                    asyncio.create_task(run_pipeline(frames))
    """

    # Speech detected when probability exceeds this
    THRESHOLD: float = 0.5
    # Hysteresis lower bound — below this → definitely silence
    THRESHOLD_LOW: float = 0.2
    # Consecutive silent frames (each 60ms) before speech end is declared
    SILENCE_FRAMES: int = 16  # ~1 second at 60ms/frame (matches reference 1000ms default)
    # Sliding window: N of last M frames must show speech to count as active
    WINDOW_SIZE: int = 8
    WINDOW_THRESHOLD: int = 3
    # Maximum accumulated frames before forcing a pipeline trigger
    MAX_FRAMES: int = 250  # ~15 seconds at 60ms/frame

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 16000,
        frame_duration_ms: int = 60,
    ) -> None:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self._decoder = OpusDecoder(sample_rate, frame_duration_ms)
        self._sample_rate = sample_rate
        self._reset_state()

    def _reset_state(self) -> None:
        self._frames: list[bytes] = []
        self._pcm_buf = bytearray()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._voice_window: list[bool] = []
        self._last_is_voice: bool = False
        self._has_speech: bool = False
        self._silent_count: int = 0
        self._speech_stopped: bool = False

    def _infer_chunk(self, samples_int16: bytes) -> bool:
        """Run one 512-sample chunk through Silero ONNX. Returns True if speech."""
        audio_int16 = np.frombuffer(samples_int16, dtype=np.int16)
        audio_f32 = audio_int16.astype(np.float32) / 32768.0
        audio_input = np.concatenate(
            [self._context, audio_f32.reshape(1, -1)], axis=1
        ).astype(np.float32)

        out, new_state = self._session.run(
            None,
            {
                "input": audio_input,
                "state": self._state,
                "sr": np.array(self._sample_rate, dtype=np.int64),
            },
        )
        self._state = new_state
        self._context = audio_input[:, -64:]
        prob: float = float(out.item())

        if prob >= self.THRESHOLD:
            is_voice = True
        elif prob <= self.THRESHOLD_LOW:
            is_voice = False
        else:
            is_voice = self._last_is_voice
        self._last_is_voice = is_voice

        self._voice_window.append(is_voice)
        if len(self._voice_window) > self.WINDOW_SIZE:
            self._voice_window.pop(0)

        return self._voice_window.count(True) >= self.WINDOW_THRESHOLD

    def push(self, opus_packet: bytes) -> bool:
        """Process one Opus packet. Returns True when a speech turn is complete."""
        self._frames.append(opus_packet)
        try:
            pcm = self._decoder.decode(opus_packet)
            self._pcm_buf.extend(pcm)
        except Exception:
            return False

        chunk_bytes = 512 * 2  # 512 int16 samples
        active = False
        while len(self._pcm_buf) >= chunk_bytes:
            chunk = bytes(self._pcm_buf[:chunk_bytes])
            self._pcm_buf = self._pcm_buf[chunk_bytes:]
            active = self._infer_chunk(chunk)

        if active:
            self._has_speech = True
            self._silent_count = 0
            self._speech_stopped = False
        elif self._has_speech:
            if not active:
                self._silent_count += 1
            if self._silent_count >= self.SILENCE_FRAMES:
                self._speech_stopped = True

        return self._speech_stopped or len(self._frames) >= self.MAX_FRAMES

    def take(self) -> list[bytes]:
        """Return accumulated Opus frames and reset state."""
        frames = self._frames
        self._reset_state()
        return frames


class AudioRateController:
    """Paces Opus packet delivery at exactly frame_duration ms intervals.

    Ported from reference xiaozhi-esp32-server audioRateController.py.
    The first PRE_BUFFER_COUNT packets are sent immediately to fill the
    device's jitter buffer; remaining packets are scheduled against a
    virtual playback clock to prevent decoder overflow.
    """

    PRE_BUFFER_COUNT: int = 5
    _FRAME_MS: int = 60

    def __init__(self, frame_duration_ms: int = 60) -> None:
        self._frame_ms = frame_duration_ms
        self._queue: deque[bytes] = deque()
        self._play_pos_ms: float = 0.0
        self._start_ts: float | None = None
        self._last_empty_ts: float = 0.0
        self._empty_event = asyncio.Event()
        self._has_data_event = asyncio.Event()
        self._empty_event.set()
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    def add_audio(self, packet: bytes) -> None:
        if len(self._queue) == 0 and self._play_pos_ms > 0:
            elapsed = (time.monotonic() - self._last_empty_ts) * 1000
            if elapsed >= self._frame_ms:
                assert self._start_ts is not None
                self._start_ts = time.monotonic() - (self._play_pos_ms / 1000)
        self._queue.append(packet)
        self._empty_event.clear()
        self._has_data_event.set()

    def start(self, send_cb: Callable[[bytes], Awaitable[None]]) -> asyncio.Task:  # type: ignore[type-arg]
        async def _loop() -> None:
            try:
                while True:
                    await self._has_data_event.wait()
                    while self._queue:
                        if self._start_ts is None:
                            self._start_ts = time.monotonic()
                        packet = self._queue[0]
                        elapsed_ms = (time.monotonic() - self._start_ts) * 1000
                        wait_ms = self._play_pos_ms - elapsed_ms
                        if wait_ms > 0:
                            await asyncio.sleep(wait_ms / 1000)
                        self._queue.popleft()
                        self._play_pos_ms += self._frame_ms
                        await send_cb(packet)
                    self._empty_event.set()
                    self._has_data_event.clear()
                    self._last_empty_ts = time.monotonic()
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(_loop())
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._queue.clear()
        self._play_pos_ms = 0.0
        self._start_ts = None
        self._empty_event.set()
        self._has_data_event.clear()

    async def wait_until_done(self) -> None:
        """Wait until the device has finished playing the queued audio."""
        await self._empty_event.wait()
        # Extra wait for pre-buffer packets still in the device's playback pipeline
        prebuffer_tail_ms = (self.PRE_BUFFER_COUNT + 2) * self._frame_ms / 1000.0
        await asyncio.sleep(prebuffer_tail_ms)


class SilenceVAD:
    """Accumulates Opus frames and fires when silence follows speech.

    Usage::

        vad = SilenceVAD(sample_rate=16000)
        async for msg in ws:
            if "bytes" in msg:
                if vad.push(msg["bytes"]):
                    frames = vad.take()
                    asyncio.create_task(run_pipeline(frames))

    The threshold and timing values here are reasonable starting points for
    a quiet-room voice assistant. Adjust per environment if needed.
    """

    # RMS energy below this → frame is "silent" (tunable per environment)
    SILENCE_THRESHOLD: float = 300.0
    # Consecutive silent frames before declaring speech ended
    SILENCE_FRAMES: int = 25  # ~500 ms at 20 ms/frame; 8–9 frames at 60 ms/frame
    # Maximum frames per turn before forcing a pipeline trigger
    MAX_FRAMES: int = 750  # ~15 s at 20 ms/frame; ~45 s at 60 ms/frame

    def __init__(self, sample_rate: int = 16000, frame_duration_ms: int = 60) -> None:
        self._decoder = OpusDecoder(sample_rate, frame_duration_ms)
        self._frames: list[bytes] = []
        self._silent_count = 0
        self._has_speech = False

    def push(self, opus_packet: bytes) -> bool:
        """Process one Opus packet. Returns True when a speech turn is complete.

        Args:
            opus_packet: Raw Opus packet from the device WebSocket frame.

        Returns:
            True if enough silence was detected after speech to trigger ASR.
        """
        self._frames.append(opus_packet)
        try:
            pcm = self._decoder.decode(opus_packet)
            energy = rms_energy(pcm)
        except Exception:
            return False

        if energy > self.SILENCE_THRESHOLD:
            self._has_speech = True
            self._silent_count = 0
        elif self._has_speech:
            self._silent_count += 1

        return self._has_speech and (
            self._silent_count >= self.SILENCE_FRAMES
            or len(self._frames) >= self.MAX_FRAMES
        )

    def take(self) -> list[bytes]:
        """Return accumulated Opus frames and reset state.

        Returns:
            List of raw Opus packets comprising one speech turn.
        """
        frames = self._frames
        self._frames = []
        self._silent_count = 0
        self._has_speech = False
        return frames
