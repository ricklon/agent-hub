"""Download FunASR SenseVoiceSmall model into models/SenseVoiceSmall."""

from pathlib import Path

from huggingface_hub import snapshot_download

dest = Path("models/SenseVoiceSmall").resolve()
dest.mkdir(parents=True, exist_ok=True)

print("Downloading FunAudioLLM/SenseVoiceSmall from Hugging Face...")
snapshot_download("FunAudioLLM/SenseVoiceSmall", local_dir=dest)
print(f"SenseVoiceSmall ready at {dest}")
