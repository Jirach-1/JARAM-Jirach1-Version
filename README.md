<div align="center">
  <img src="JARAM.ico" alt="JARAM Logo" width="64" height="64">
  <h1>JARAM</h1>
  <p><strong>Just Another Roblox Account Manager</strong></p>
  <p>Manage multiple Roblox accounts with automatic relaunching, server tracking, OCR-driven detection, automation, and optional Discord control.</p>
</div>


## Recent Highlights

- **Auto Item is now Auto Actions**: build reusable sequences from clicks, drags, key presses, paste, scroll, waits, webhooks, conditionals, loops, and calls to other action rows.
- **Discord Control**: run account and utility operations through permission-controlled slash commands.
- **Cookie Encryption**: password-protect stored Roblox cookies and the Discord bot token, with optional protection for existing backups.
- **Merchant Fix**: choose between a whole-PC Classic block and per-process Smart mode for log-based merchant detection.
- **Improved account operations**: launch priorities, multi-user launch tools, descriptions, last-launch tracking, and filtered bulk deletion.
- **Expanded observability**: a live Auto Actions monitor, persistent Multiscope state across manager handoffs, and Discord command audit logs.

## Features

### Multi-Account Manager
- Launch and monitor many Roblox clients
- Auto-reconnect on crash/disconnect
- Optional **Spares Mode** to keep standby accounts for fast handoffs
- Window-limit enforcement, orphan cleanup, and an optional age-based kill watchdog (with Discord webhook ping)
- Per-account launch priorities; higher-priority waiting accounts launch first
- Dashboard controls to restart every session or terminate managed processes

### Account + Cookie Tools
- `Accounts` tab plus the richer `File > Manage Users` editor (automatic `users.json` backups)
- Multi-select launching, saved account descriptions, activity/last-launch tracking, and filtered bulk deletion
- Cookie helpers, including **Login with Browser** (Selenium) to extract `.ROBLOSECURITY`
- Supports private servers (link-based) and public places (place ID)
- Optional password-based AES-GCM encryption for stored cookies and the Discord bot token
- Lock/unlock, change-password, decrypt, and emergency-reset controls under `File > Cookie Encryption...`

### Private Server Support
- Accepts direct private server links and share links (auto-resolved)
- Supported formats:
  - `https://www.roblox.com/games/[PLACE_ID]/[GAME_NAME]?privateServerLinkCode=[CODE]`
  - `https://www.roblox.com/share?code=[CODE]&type=Server`

### OCR Merchant / Event Detection
- Built-in OCR engine using **RapidOCR (ONNXRuntime)**
- **DirectML** GPU acceleration with a CPU fallback option
- ROI calibration, color filters, cooldowns, and frame-diff skipping
- Discord webhook alerts for merchants (e.g. **Jester** / **Mari**) with biome + server context

### Multiscope (Server / Biome / Merchant Tracker)
- Groups accounts by the exact server they are in
- Tracks per-server biome (BloxstrapRPC), in-menu state, merchants, and event counts
- Live `Multiscope` tab + persisted all-time counters in `found_stats.json`
- Webhook alerts with per-event rate limiting and custom ping targets
- Runs outside the UI process and preserves tracking state across pause/resume handoffs

### Anti AFK Engine
- Per-account actions (`space`, `ws`, `zoom`, `AutoReconnect`)
- Configurable key delay (ms) and optional main-menu AutoReconnect

### Auto Actions Automation
- Reusable action rows containing clicks, drags, keyboard input, paste, scrolling, waits, webhooks, loops, conditionals, and control flow
- Trigger rows normally, from OCR filters, merchant detections, or an immediate call from another action row
- Per-row cooldowns, repeat modes, biome restrictions, and per-user whitelist/blacklist targeting
- Color and OCR conditionals, webhook embeds/screenshots, and per-user paste values
- Built-in and user-defined presets with JSON import/export
- Global hotkey, manual **Test Selected** runner, and a live monitor for triggers, queues, cooldowns, and errors
- Coordinates with Anti AFK and BES so input and throttling do not conflict with a running sequence

### Discord Control
- Optional Discord bot with configurable admin user IDs, role IDs, and Discord Administrator access
- Account autocomplete and slash commands for status, enable/disable, Auto Actions toggles, flag clearing, and restarting sessions
- Remote PSL grabbing plus Roblox block/unblock utilities
- Commands: `/help`, `/status`, `/enable`, `/disable`, `/action`, `/clearflags`, `/restartall`, `/psl`, `/block`, and `/unblock` (`/list` is available in supported builds)
- Auto-start support, an in-app command reference/log, and command usage records in `logs/discord_command_usage.jsonl`

### Resource Controls
- `Trimmer` tab: periodically trims Roblox working-set (optional threshold)
- `BES` tab: CPU throttling via Battle Encoder Shirase (menu vs in-game, exempt users)

### Extras Menu Tools
- **Found Stats...**: all-time and time-window biome/merchant totals
- **Roblox Multi-Instance**: allow multiple Roblox Player processes
- **Merchant Fix**: Classic whole-PC or Smart per-managed-process asset blocking for merchant log detection
- **RAM Export**: import accounts from Roblox Account Manager into `users.json`
- **Utilities**: block/unblock tools + private server link (PSL) grabber
- **Roblox Log Cleanup**: remove inactive Roblox logs on a configurable schedule

## Quick Start

### Run From Source (Windows)
1. Install **Python 3.14 (64-bit)**.
2. Install dependencies: `pip install -r requirements.txt`
3. Build the native modules with the same interpreter (Visual Studio Build Tools required):
   - `cd native`
   - `python -m pip install -U "pybind11>=3.0" "setuptools>=77"`
   - `python setup.py build_ext --inplace`
   - `cd ..`
4. Run: `python launcher.py`

The `.pyd` ABI tag must match Python. Python 3.14 loads the `cp314` builds; it
will intentionally ignore older `cp312` files. See `native/NATIVE_BUILD.md` for
build and verification details.

### System Requirements
- **Operating System**: Windows 10/11
- **Python**: 3.14, 64-bit (if running from source)
- **Roblox**: Installed and working on the system
- **Optional**:
  - Chrome for browser cookie login + utilities


## Configuration Location

All configuration files are stored in `%APPDATA%\\JARAM\\` (use `File > Show Config Location`):
- `users.json` - accounts + per-user metadata
- `settings.json` - application settings
- `backups\\` - automatic timestamped backups
- `found_stats.json` - all-time biome/merchant counters (Multiscope)
- `block_log.json`, `users_to_block.txt` - Utilities state
- `manager_pause_state.json` - manager/Multiscope state used during pause and resume
- `logs\` - Merchant Fix activity and Discord command audit logs

When Cookie Encryption is enabled, cookies in `users.json` and the configured Discord bot token are encrypted at rest. JARAM asks for the password at startup; automation that needs a cookie remains unavailable until the store is unlocked. The password cannot be recovered, so retain a secure copy outside JARAM.

## Usage Notes

### Adding Accounts
- Use the `Accounts` tab or `File > Manage Users`.
- Cookies:
  - Manual: copy `.ROBLOSECURITY` from browser DevTools.
  - Built-in: click **Login with Browser** (requires Chrome + Selenium).

### OCR Setup
1. Open the `OCR` tab and enable OCR.
2. Calibrate ROI (chat area), adjust color filters and cooldowns.
3. If you see "DirectML provider is not available", install `onnxruntime-directml` or switch OCR device to CPU.

### RAM (Roblox Account Manager) Import
- Use `Extras > RAM Export` to fetch accounts from RAM via its HTTP API and merge/replace `users.json`.

### Auto Actions Setup
1. Open `Auto Actions`, enable the engine, and select the target users.
2. Add an action row, build its sequence, and configure its trigger/behavior.
3. Use **Test Selected** before enabling unattended playback.
4. Use **Monitor** to inspect per-user state, queued/running actions, cooldowns, and errors.

### Discord Control Setup
1. Create a Discord application/bot and copy its bot token.
2. Open the `Discord` tab and enter the token plus at least one trusted admin user or role ID.
3. Save the configuration, start the bot, and confirm that its slash commands sync.
4. Enable auto-start only after testing access from the intended server.

Treat the bot token like a password. Cookie Encryption also protects it in `settings.json` when encryption is enabled.

### Utilities (Blocking/Unblocking/PSL Grabber)
- Use `Extras > Utilities`.
- Utilities run actions using your stored cookies; do not share `users.json`.

### Merchant Fix
- Open `Extras > Merchant Fix`.
- **Classic** applies the asset-delivery block to the whole PC and requires Roblox to be closed during preparation.
- **Smart** applies PID-scoped blocks only to selected accounts managed by JARAM and supports a configurable log trigger/delay.
- Administrator access may be requested for the elevated helper. 
- Use the same window to inspect active blocks and the activity log.

## Troubleshooting
- Input automation not working (Auto Actions / Anti AFK): try running JARAM as Administrator and avoid overlays that block foreground input.
- "Login with Browser" fails: ensure Chrome is installed and dependencies installed (`pip install -r requirements.txt`).
- Discord commands do not appear: confirm `discord.py` is installed, the token is valid, the bot is invited with application-command scope, and check the `Discord Control Log`.
- Cookies are locked: use `File > Cookie Encryption...` to unlock them before launching accounts or running cookie-backed utilities.
- Smart Merchant Fix cannot start: close conflicting JARAM instances, approve the elevation prompt, then review its activity log.

## License and Disclaimer

This project is developed for educational and personal use purposes. Please refer to [LICENSE](LICENSE.md) for terms. You are responsible for:
- Complying with Roblox Terms of Service
- Ensuring account security and cookie protection

We are not responsible for misuse of this application or violations of service terms.

## Support and Community

- Discord: https://discord.gg/theglitchcore

### Reporting Issues
When reporting issues, please include:
- Windows version
- Python version (if running from source)
- Logs from the `Logs` tab
- Steps to reproduce
- Configuration details (do not include cookies)
