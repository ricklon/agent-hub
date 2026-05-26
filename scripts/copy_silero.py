"""Copy silero_vad.onnx from the installed silero_vad package into models/."""

import os
import shutil

import silero_vad  # type: ignore[import-untyped]

src = os.path.join(os.path.dirname(silero_vad.__file__), "data", "silero_vad.onnx")
dest = "models/silero_vad.onnx"
shutil.copy2(src, dest)
print(f"Copied {src} -> {dest}")
