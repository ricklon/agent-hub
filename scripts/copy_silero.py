"""Copy silero_vad.onnx from the installed silero_vad package into models/.

Locates the package without importing it: silero_vad's __init__ pulls in
torchaudio, which the DigitalOcean image deliberately doesn't install. Only
the ONNX file is needed — the VAD runs it through onnxruntime at runtime.
"""

import shutil
from importlib.util import find_spec
from pathlib import Path

spec = find_spec("silero_vad")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit("silero_vad is not installed — cannot copy silero_vad.onnx")

src = Path(spec.submodule_search_locations[0]) / "data" / "silero_vad.onnx"
dest = Path("models/silero_vad.onnx")
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(src, dest)
print(f"Copied {src} -> {dest}")
