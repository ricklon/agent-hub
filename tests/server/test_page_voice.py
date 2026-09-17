"""Page agent hands-free voice: segmenting speech, what reaches the model, the page script.

Hands-free voice couldn't hear anyone: the VAD handed ASR only the unconsumed
tail of its buffer (a few milliseconds), and counted silence per browser push,
so a turn needed seconds of quiet to end. Issue #96.
"""

from __future__ import annotations

import numpy as np

from agent_hub.server.audio import PcmSileroVAD
from agent_hub.server.page_agent import _AudioStats, voice_turn_messages

CHUNK = 512 * 2  # bytes in one 32 ms VAD chunk


class _ScriptedVAD(PcmSileroVAD):
    """PcmSileroVAD with the model replaced by a per-chunk script."""

    def __init__(self, script: list[bool]) -> None:  # noqa: D107 - no model to load
        self._sample_rate = 16000
        self._script = list(script)
        self._reset_state()

    def _infer_chunk(self, samples_int16: bytes) -> bool:
        return self._script.pop(0) if self._script else False


def _chunks(values: list[int]) -> bytes:
    """One chunk per value, each filled with that sample value (to tell them apart)."""
    return b"".join(np.full(512, v, dtype=np.int16).tobytes() for v in values)


def test_the_turn_keeps_pre_roll_speech_and_trailing_silence() -> None:
    # 12 quiet chunks, 20 speech chunks, then silence.
    script = [False] * 12 + [True] * 20 + [False] * 40
    vad = _ScriptedVAD(script)
    audio = _chunks(list(range(1, 73)))

    ended = False
    pushed = 0
    for i in range(0, len(audio), 4096 * 2):  # browser-sized pushes of 8 chunks
        pushed = i + 4096 * 2
        if vad.push(audio[i : i + 4096 * 2]):
            ended = True
            break

    assert ended
    segment = np.frombuffer(vad.take_pcm(), dtype=np.int16).reshape(-1, 512)[:, 0].tolist()
    # 10 chunks of pre-roll (3..12), the 20 speech chunks (13..32), 16 silent (33..48).
    assert segment == list(range(3, 49))
    # The turn ended 16 chunks (~0.5 s) after speech, whatever the push size.
    assert pushed <= 56 * CHUNK


def test_silence_is_counted_per_chunk_not_per_push() -> None:
    script = [True] * 5 + [False] * 16
    one_push = _ScriptedVAD(list(script))
    assert one_push.push(_chunks([1] * 21)) is True  # all in a single push

    tiny = _ScriptedVAD(list(script))
    ended_at = None
    audio = _chunks([1] * 21)
    for n, i in enumerate(range(0, len(audio), 256), start=1):  # 8 ms pushes
        if tiny.push(audio[i : i + 256]):
            ended_at = n
            break
    assert ended_at == 21 * 4


def test_a_long_turn_is_cut_at_the_maximum() -> None:
    vad = _ScriptedVAD([True] * 400)
    assert vad.push(_chunks([1] * 300)) is True
    assert vad.segment_ms == PcmSileroVAD.MAX_FRAMES * 32
    # Audio after the cut stays for the next turn.
    leftover_before = len(vad._pcm_buf)
    vad.take_pcm()
    assert len(vad._pcm_buf) == leftover_before == 50 * CHUNK


def test_the_utterance_reaches_the_model() -> None:
    conversation = [
        {"role": "user", "content": "old", "created_at": "x"},
        {"role": "assistant", "content": "old reply"},
        {"role": "image", "content": "[photo] a mug"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent reply"},
    ]
    assert voice_turn_messages(conversation, 3, "what time is it?") == [
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent reply"},
        {"role": "user", "content": "what time is it?"},
    ]
    assert voice_turn_messages([], 40, "hello") == [{"role": "user", "content": "hello"}]


def test_audio_stats_report_arrival_then_level() -> None:
    stats = _AudioStats()
    loud = np.full(1600, 1000, dtype=np.int16).tobytes()
    assert stats.add(loud) == "first audio received"
    assert stats.add(loud) is None
    stats._last_report -= _AudioStats.REPORT_EVERY_S + 1
    report = stats.add(loud)
    assert report is not None and "rms 1000, peak 1000" in report


def test_page_script_voice_fixes() -> None:
    from agent_hub.server._page_html import PAGE_HTML

    # WebMCP: one tool object per call, awaited.
    assert "await mc.registerTool({" in PAGE_HTML
    assert "mc.registerTool(t.name, t.description" not in PAGE_HTML
    # An empty wake word (open mic) is sent too, and changes are sent live.
    assert 'voiceWs.send(JSON.stringify({type: "wake_word", word: wakeWord}));' in PAGE_HTML
    assert "if (wakeWord) voiceWs.send" not in PAGE_HTML
    # The voice option applies to hands-free replies.
    assert 'voiceWs.send(JSON.stringify({type: "voice_mode", mode: voiceMode()}));' in PAGE_HTML
    assert 'if (!audioCtx || handsFreeVoice !== "hub") return;' in PAGE_HTML
    assert 'if (handsFreeVoice === "browser") speakBuiltin(msg.text || "");' in PAGE_HTML
    # Heard-but-ignored feedback, and no stray autofocus or favicon request.
    assert 'msg.type === "heard"' in PAGE_HTML
    assert "autofocus" not in PAGE_HTML
    assert '<link rel="icon" href="data:,">' in PAGE_HTML
