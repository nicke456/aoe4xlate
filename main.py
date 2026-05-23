"""
AoE4 Chat Translator — entry point.

Usage:
    python main.py                  # browser source only (http://localhost:8766/overlay.html)
    python main.py --desktop        # always-on-top transparent window (requires pywebview)
    python main.py --no-overlay     # terminal-only, no browser source or window
    python main.py --log-only       # just print parsed messages, no translation

Environment variable overrides (useful for secrets without editing config.yaml):
    AOE4XLATE_DEEPL_KEY
    AOE4XLATE_ANTHROPIC_KEY
"""

import argparse
import datetime
import os
import sys
import threading
import time
from pathlib import Path

import yaml

import overlay as ov
import players
import translator as tr
from watcher import ChatMessage, LogWatcher


# ── Config ──────────────────────────────────────────────────────────────────

def _find_log() -> Path:
    """Check standard and OneDrive-redirected Documents locations."""
    import ctypes
    candidates = [
        Path.home() / "Documents" / "My Games" / "Age of Empires IV" / "warnings.log",
        Path.home() / "OneDrive" / "Documents" / "My Games" / "Age of Empires IV" / "warnings.log",
    ]
    # Also ask Windows for the real Documents folder (handles OneDrive redirect)
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL = 5
        candidates.insert(0, Path(buf.value) / "My Games" / "Age of Empires IV" / "warnings.log")
    except Exception:
        pass
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # fall back to first candidate so watcher can wait for it


_DEFAULT_LOG = _find_log()

_DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    # Fill in defaults
    cfg.setdefault("log_file", "")
    cfg.setdefault("target_language", "en")
    cfg.setdefault("translation_backend", "google")
    cfg.setdefault("deepl_api_key", "")
    cfg.setdefault("overlay_port", 8765)
    cfg.setdefault("overlay_http_port", 8766)
    cfg.setdefault("max_messages", 20)
    cfg.setdefault("poll_interval", 0.25)
    cfg.setdefault("show_own_messages", True)
    cfg.setdefault("watch_channels", [])
    cfg.setdefault("hotkey", "ctrl+shift+\\")

    # Environment variable overrides for secrets
    cfg["deepl_api_key"] = os.environ.get("AOE4XLATE_DEEPL_KEY", cfg["deepl_api_key"])

    return cfg


# ── Channel label ────────────────────────────────────────────────────────────

_CHANNEL_LABELS = {0: "Post", 16: "All"}


def channel_label(ch: int) -> str:
    return _CHANNEL_LABELS.get(ch, f"ch{ch}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AoE4 Chat Translator")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Path to config.yaml")
    parser.add_argument("--desktop", action="store_true", help="Open an always-on-top transparent window (requires pywebview)")
    parser.add_argument("--no-overlay", action="store_true", help="Disable the browser source overlay and desktop window")
    parser.add_argument("--log-only", action="store_true", help="Print parsed messages only, no translation")
    parser.add_argument("--test", action="store_true", help="Simulate chat messages on a loop instead of watching the log")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    log_path = cfg["log_file"] or str(_DEFAULT_LOG)
    if not Path(log_path).exists():
        print(f"[main] Log file not found: {log_path}")
        print("[main] Start AoE4 and load into a match to generate the log file.")
        print("[main] Waiting for log file to appear…")

    # Set up translation
    translate = None
    if not args.log_only:
        try:
            translate = tr.build(
                backend=cfg["translation_backend"],
                target_lang=cfg["target_language"],
                deepl_api_key=cfg["deepl_api_key"],
            )
            print(f"[main] Translation backend: {cfg['translation_backend']} → {cfg['target_language']}")
        except Exception as e:
            print(f"[main] Translation disabled: {e}")

    # Set up overlay servers
    if not args.no_overlay:
        ov.start_ws_server(port=cfg["overlay_port"])
        ov.start_http_server(port=cfg["overlay_http_port"])
        print(f"[main] WebSocket on ws://localhost:{cfg['overlay_port']}")

    # Track current match to reset player cache on new match
    _state = {"last_match_id": None}

    def on_message(msg: ChatMessage):
        if msg.is_own and not cfg["show_own_messages"]:
            return

        # Reset player cache when we enter a new match
        if msg.match_id != _state["last_match_id"]:
            players.clear_cache()
            _state["last_match_id"] = msg.match_id

        # Resolve player name in a background thread to avoid blocking the watcher
        threading.Thread(target=_handle, args=(msg,), daemon=True).start()

    def _handle(msg: ChatMessage):
        player_name = players.resolve(msg.sender_id) if not msg.is_own else "You"
        text = msg.raw_message
        translated = None

        if translate and not msg.is_own:
            try:
                translated = translate(text)
            except Exception as e:
                print(f"[main] Translation error: {e}")

        ts = datetime.datetime.now().isoformat()
        ch = channel_label(msg.channel)

        if translated:
            print(f"[{ch}] {player_name}: {text}  →  {translated}")
        else:
            print(f"[{ch}] {player_name}: {text}")

        if not args.no_overlay:
            ov.broadcast(
                {
                    "type": "chat",
                    "sender_id": msg.sender_id,
                    "player_name": player_name,
                    "raw_message": text,
                    "translated": translated,
                    "channel": msg.channel,
                    "match_id": msg.match_id,
                    "is_own": msg.is_own,
                    "timestamp": ts,
                }
            )

    print("[main] Running. Press Ctrl+C to stop.")

    if args.test:
        source_thread = threading.Thread(
            target=_test_loop, args=(on_message,), daemon=True
        )
    else:
        watcher = LogWatcher(
            log_path=log_path,
            callback=on_message,
            poll_interval=cfg["poll_interval"],
            watch_channels=cfg["watch_channels"],
        )
        source_thread = threading.Thread(target=watcher.run, daemon=True)

    if args.desktop and not args.no_overlay:
        source_thread.start()
        _launch_desktop_window(cfg)
    else:
        source_thread.start()
        try:
            source_thread.join()
        except KeyboardInterrupt:
            print("\n[main] Stopped.")


def _test_loop(callback):
    """Simulate a repeating cycle of chat messages for UI testing."""
    import itertools
    from watcher import ChatMessage

    LOCAL_ID = 43843
    cycle = itertools.cycle([
        # (delay_before, sender_id, name, message, channel)
        (3, 24435821, "我是你的爸爸", "一起攻击右边！",        16),
        (0, 11460579, "SomePlayer",   "red is rushing me",    16),
        (3, 99999,    "Ally",         "ok I'll help",         16),
        (7, LOCAL_ID, "You",          "gg wp",                0),
    ])

    print("[test] Simulating chat messages...")
    for delay, sender_id, name, text, channel in cycle:
        time.sleep(delay)
        # Pre-populate the player cache so we don't hit the API
        players._cache[sender_id] = name
        msg = ChatMessage(
            local_player_id=LOCAL_ID,
            sender_id=sender_id,
            raw_message=text,
            filtered_message=text,
            channel=channel,
            match_id=999999,
            is_own=(sender_id == LOCAL_ID),
        )
        callback(msg)


def _launch_desktop_window(cfg: dict):
    from desktop_overlay_tk import OverlayWindow
    overlay = OverlayWindow(
        ws_port=cfg["overlay_port"],
        max_messages=cfg["max_messages"],
        hotkey=cfg.get("hotkey", ""),
    )
    overlay.run()


if __name__ == "__main__":
    main()
