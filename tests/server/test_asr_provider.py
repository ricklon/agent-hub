"""Tests for ASR provider factory wiring."""

from __future__ import annotations

from agent_hub.providers import asr


def test_get_provider_builds_funasr_onnx(monkeypatch) -> None:
    asr._cache.clear()


def test_get_provider_funasr_onnx_defaults(monkeypatch) -> None:
    asr._cache.clear()
    captured = {}

    class FakeONNXProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import agent_hub.providers.asr.funasr_onnx_provider as onnx_provider

    monkeypatch.setattr(onnx_provider, "FunASRONNXProvider", FakeONNXProvider)

    asr.get_provider("funasr_onnx", {})

    assert captured["model_dir"] == "models/SenseVoiceSmall-onnx"
    assert captured["quantize"] is True
    asr._cache.clear()
    captured = {}

    class FakeONNXProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import agent_hub.providers.asr.funasr_onnx_provider as onnx_provider

    monkeypatch.setattr(onnx_provider, "FunASRONNXProvider", FakeONNXProvider)

    provider = asr.get_provider(
        "funasr_onnx",
        {
            "asr": {
                "funasr_onnx": {
                    "model_dir": "models/test",
                    "language": "auto",
                    "intra_op_num_threads": 2,
                    "quantize": True,
                }
            }
        },
    )

    assert provider is not None
    assert captured == {
        "model_dir": "models/test",
        "language": "auto",
        "intra_op_num_threads": 2,
        "quantize": True,
    }
    asr._cache.clear()
