"""Download Piper voices into models/piper.

Run at image build time so the default voice speaks without a download.
Other voices still download the first time a persona uses them.

Defaults to the provider's default voice; override with AGENT_HUB_TTS_PIPER_VOICE,
or name voices as arguments:  python scripts/download_piper.py en_GB-alba-medium
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from piper.download_voices import download_voice

DEST = Path(os.environ.get("AGENT_HUB_TTS_PIPER_DATA_DIR", "models/piper"))
voices = sys.argv[1:] or [os.environ.get("AGENT_HUB_TTS_PIPER_VOICE", "en_US-lessac-medium")]

DEST.mkdir(parents=True, exist_ok=True)
for voice in voices:
    print(f"Downloading Piper voice {voice}...")
    download_voice(voice, DEST)
print(f"Piper voices in {DEST}: {', '.join(voices)}")
