"""Wipe per-session device data between public demo sessions.

Removes what people said and what devices saw:

- ``conversation_history`` rows in the registry database
- the append-only transcript log (``data/transcripts.jsonl``)
- captured camera images (``data/images/<device-id>/*``)
- opt-in ASR audio captures (``server.debug_audio_dir``), when configured

The registry is kept intact so devices do not need re-enrolling mid-event:
agents, personas, enrollment/WebSocket tokens, dashboard operators, and the
audit log all survive.

The LLM spend ledger is kept by default because the cumulative spend cap is
enforced against it — wiping it silently resets that protection. Pass
``--clear-spend`` to reset it too.

Usage::

    just reset-data                          # prompts for confirmation
    just reset-data --dry-run                # show what would be removed
    just reset-data --yes                    # skip the prompt
    just reset-data --yes --clear-spend      # also reset the spend cap
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from agent_hub.config import load_settings
from agent_hub.registry.store import RegistryStore
from agent_hub.server import transcript_log


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reset-data",
        description="Wipe per-session device data (transcripts, images, ASR "
        "captures) between public demo sessions. Keeps the registry.",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed and exit without changing anything",
    )
    parser.add_argument(
        "--clear-spend",
        action="store_true",
        help="also delete the LLM spend ledger (resets the cumulative spend cap)",
    )
    return parser.parse_args(argv)


def _remove_dir_contents(root: Path) -> int:
    """Delete every entry inside ``root``, keeping ``root`` itself.

    Returns the number of top-level entries removed.
    """
    if not root.exists():
        return 0
    removed = 0
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file()) if root.exists() else 0


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as handle:
        return sum(1 for _ in handle)


async def _run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()

    db_path = Path(settings.registry.db_path)
    image_root = Path(settings.server.dashboard_image_root)
    transcript_path = transcript_log.log_path()
    audio_raw = settings.server.debug_audio_dir.strip()
    audio_dir = Path(audio_raw) if audio_raw else None

    store = RegistryStore(db_path) if db_path.exists() else None
    history_messages = 0
    spend_rows = 0
    if store is not None:
        await store.initialize()
        history_messages = await store.conversation_turn_count()
        spend_rows = int((await store.llm_spend_summary())["calls"])

    image_files = _count_files(image_root)
    transcript_lines = _count_lines(transcript_path)
    audio_files = _count_files(audio_dir) if audio_dir is not None else 0

    spend_note = "will CLEAR — resets the cumulative cap" if args.clear_spend else "kept"
    print("Kept: agents, personas, tokens, dashboard operators, audit log.")
    print("Targeted for deletion:")
    print(f"  conversation history : {history_messages} messages   [{db_path}]")
    print(f"  transcript log       : {transcript_lines} lines   [{transcript_path}]")
    print(f"  captured images      : {image_files} files   [{image_root}]")
    if audio_dir is not None:
        print(f"  ASR audio captures   : {audio_files} files   [{audio_dir}]")
    print(f"  LLM spend ledger     : {spend_rows} rows   [{spend_note}]")

    if store is not None and args.dry_run:
        await store._engine.dispose()

    if args.dry_run:
        print("\nDry run — nothing changed.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "\nRefusing to delete data without --yes in a non-interactive shell.",
                file=sys.stderr,
            )
            if store is not None:
                await store._engine.dispose()
            return 1
        reply = input("\nType 'reset' to permanently delete the above: ").strip()
        if reply != "reset":
            print("Aborted — nothing changed.")
            if store is not None:
                await store._engine.dispose()
            return 1

    if store is not None:
        await store.clear_all_history()
        if args.clear_spend:
            await store.clear_llm_spend()
        await store._engine.dispose()

    removed_images = _remove_dir_contents(image_root)
    transcript_removed = transcript_path.exists()
    transcript_path.unlink(missing_ok=True)
    removed_audio = _remove_dir_contents(audio_dir) if audio_dir is not None else 0

    print("\nDone.")
    print(f"  conversation history : cleared ({history_messages} messages)")
    print(f"  transcript log       : {'removed' if transcript_removed else 'nothing to remove'}")
    print(f"  captured images      : {removed_images} entries removed")
    if audio_dir is not None:
        print(f"  ASR audio captures   : {removed_audio} entries removed")
    if args.clear_spend:
        print(f"  LLM spend ledger     : cleared ({spend_rows} rows)")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
