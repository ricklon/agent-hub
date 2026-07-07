"""Copy silero_vad.onnx from the installed silero_vad package into models/."""

import shutil
from pathlib import Path

import silero_vad  # type: ignore[import-untyped]

src = Path(silero_vad.__file__).parent / "data" / "silero_vad.onnx"
dest = Path("models/silero_vad.onnx")
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(src, dest)
print(f"Copied {src} -> {dest}")
