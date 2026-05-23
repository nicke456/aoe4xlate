# aoe4xlate

Real-time chat translator for Age of Empires IV. Watches the game's `warnings.log`, translates foreign-language messages, and shows them in a transparent always-on-top overlay.

Built for FFA, where players can coordinate openly in non-English chat. Also useful any time teammates don't share a language.

![overlay screenshot placeholder]

## How it works

AoE4 writes all chat to `warnings.log` via WebSocket messages. This tool tails that file, parses the `MatchReceivedChatMessage` events, resolves player names via the [AoE4World API](https://aoe4world.com/api), translates non-English messages, and pushes them to an overlay.

## Requirements

- Windows (where AoE4 runs)
- Python 3.10+
- AoE4 in **Borderless Windowed** mode for the desktop overlay

## Install

```
git clone https://github.com/YOUR_USERNAME/aoe4xlate.git
cd aoe4xlate
pip install -r requirements.txt
```

## Usage

**Desktop overlay** (transparent always-on-top window):
```
python main.py --desktop
```

**OBS browser source** (add `http://localhost:8766/overlay.html` as a Browser Source):
```
python main.py
```

**Terminal only** (no overlay):
```
python main.py --no-overlay
```

**Test mode** (simulates chat without being in a game):
```
python main.py --desktop --test
```

Drag the overlay by the title bar. Click `✕` to close. Press `Ctrl+Shift+\` (configurable) to show/hide — works even when the game has focus.

## Configuration

Edit `config.yaml`:

| Key | Default | Description |
|-----|---------|-------------|
| `log_file` | *(auto-detect)* | Path to `warnings.log`. Leave empty to auto-detect. |
| `target_language` | `en` | BCP-47 code for your language. |
| `translation_backend` | `google` | `google` (free, no key) or `deepl`. |
| `deepl_api_key` | | Required for the DeepL backend. |
| `show_own_messages` | `true` | Show your own messages in the overlay. |
| `watch_channels` | *(all)* | Filter to specific channel numbers. Known: `16` = all-chat, `0` = post-game. |
| `hotkey` | `ctrl+shift+\` | Global hotkey to show/hide the desktop overlay. Works even when the game has focus. Set to empty string to disable. |

The DeepL API key can also be passed via environment variable: `AOE4XLATE_DEEPL_KEY`.

### Log file location

Auto-detection checks:
1. Windows Shell `My Documents` path (handles OneDrive redirect automatically)
2. `~\Documents\My Games\Age of Empires IV\warnings.log`
3. `~\OneDrive\Documents\My Games\Age of Empires IV\warnings.log`

If none are found, set `log_file` explicitly in `config.yaml`.

## Translation backends

| Backend | Cost | Speed | Notes |
|---------|------|-------|-------|
| `google` | Free | Fast | Uses `deep-translator`. No API key needed. |
| `deepl` | Free tier: 500k chars/month | Fast | Requires [DeepL API key](https://www.deepl.com/pro-api). |

## Known channel numbers

| Value | Meaning |
|-------|---------|
| `16` | All-chat (in-game) |
| `0` | Post-game chat |

Other values are displayed as `CH<n>`. If you discover more, please open an issue.

## Architecture

```
warnings.log → watcher.py → main.py → translator.py → overlay.py → browser / tkinter window
                                    ↓
                               players.py (AoE4World API)
```

- **`watcher.py`** — tails the log, parses `MatchReceivedChatMessage` WebSocket lines
- **`players.py`** — resolves AoE4World profile IDs to player names, in-process cache
- **`translator.py`** — pluggable translation backends
- **`overlay.py`** — WebSocket server + HTTP server for the browser source
- **`overlay.html`** — OBS browser source page (transparent, AoE4-styled, auto-reconnects)
- **`desktop_overlay_tk.py`** — always-on-top tkinter window with color-key transparency
- **`main.py`** — entry point, config, wires everything together

## Prior art

- [FluffyMaguro/AoE4_Overlay](https://github.com/FluffyMaguro/AoE4_Overlay) — reads `warnings.log` for game detection and player stats (GPL-3.0)
- [aoe4world/overlay](https://github.com/aoe4world/overlay) — web streaming overlay

## License

MIT
