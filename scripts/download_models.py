"""Download SenseVoiceSmall ONNX model into models/SenseVoiceSmall-onnx."""

from pathlib import Path

from huggingface_hub import snapshot_download

dest = Path("models/SenseVoiceSmall-onnx").resolve()
dest.mkdir(parents=True, exist_ok=True)

print("Downloading haixuantao/SenseVoiceSmall-onnx from Hugging Face...")
snapshot_download("haixuantao/SenseVoiceSmall-onnx", local_dir=dest)
print(f"SenseVoiceSmall ONNX ready at {dest}")
