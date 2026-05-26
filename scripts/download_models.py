"""Download FunASR SenseVoiceSmall model into models/SenseVoiceSmall."""

import os

from modelscope import snapshot_download  # type: ignore[import-untyped]

dest = os.path.abspath("models/SenseVoiceSmall")
os.makedirs(dest, exist_ok=True)

print("Downloading iic/SenseVoiceSmall from ModelScope...")
snapshot_download("iic/SenseVoiceSmall", local_dir=dest)
print(f"SenseVoiceSmall ready at {dest}")
