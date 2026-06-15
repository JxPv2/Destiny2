# Destiny 2 Loadout Swap Macro

A lightweight Python macro that swaps Destiny 2 loadouts via global hotkeys. Press a bound key and the script opens the in-game loadout menu, navigates to the correct grid slot, and clicks it.

> ⚠️ **WARNING**: Destiny 2 uses BattlEye anti-cheat. External input automation may be detected and can result in permanent account bans. Use this tool **at your own risk**.

---

## Requirements

- Python 3.8+
- Windows (focus guard requires `pywin32`; script runs on other platforms with guard disabled)

## Installation

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install pyautogui pynput pywin32
```

## First Run

1. Run the script:
   ```bash
   python loadout_swap.py
   ```
2. On first launch, a `config.json` is created next to the script.
3. Edit `config.json` to match your screen coordinates and keybinds.
4. Restart the script after editing the config.

## Configuration (`config.json`)

The config file supports `//` and `/* */` comments.

| Key | Description | Example |
|-----|-------------|---------|
| `menu_key` | Key to open/close the in-game loadout menu | `"f1"` |
| `nav_key` | Key to move to the loadout tab | `"left"` |
| `exit_key` | Key to stop the script | `"pause"` |
| `start_x` | X coordinate of loadout 1 (top-left) | `115` |
| `start_y` | Y coordinate of loadout 1 (top-left) | `384` |
| `offset` | Pixel spacing between loadout icons | `97` |
| `delay_menu_open` | Seconds to wait after opening menu | `0.45` |
| `delay_nav` | Seconds to wait after navigating | `0.45` |
| `delay_click` | Seconds to wait after clicking | `0.45` |
| `window_title_substrings` | Window titles that allow the macro to fire | `["Destiny 2"]` |
| `keybinds` | Map of `loadout1`..`loadout20` to hotkey strings | see below |
| `reload_key` | Key to reload config without restarting | `"home"` |
| `debug` | If `true`, prints actions without moving mouse | `false` |

### Keybind Format

| Format | Meaning |
|--------|---------|
| `1` – `0` | Top-row number keys |
| `numpad_1` – `numpad_0` | Numpad keys |
| `-`, `+` | Minus / plus keys |
| `ctrl_1` | Hold **Ctrl** + press `1` |
| `shift_numpad_1` | Hold **Shift** + press Numpad `1` |
| `alt_q` | Hold **Alt** + press `q` |
| `ctrl_shift_1` | Hold **Ctrl+Shift** + press `1` |

**Modifiers must be in this order:** `ctrl`, `shift`, `alt`, `cmd`.

### Example `keybinds`

```json
"keybinds": {
    "loadout1":  "numpad_1",
    "loadout2":  "numpad_2",
    "loadout3":  "numpad_3",
    "loadout4":  "",
    "loadout5":  "ctrl_1",
    "loadout6":  "shift_numpad_1",
    "loadout7":  "alt_f1",
    "loadout8":  "",
    "loadout9":  "numpad_7",
    "loadout10": "numpad_8"
}
```

Leave a value empty (`""`) to unbind a loadout.

## Grid Layout

Destiny 2's loadout grid is **4 columns × 5 rows** (20 loadouts total).

```
 1   2   3   4
 5   6   7   8
 9  10  11  12
13  14  15  16
17  18  19  20
```

The script calculates the click position from `start_x`, `start_y`, and `offset`.

## Finding Your Coordinates

1. Open Destiny 2 and display the loadout menu.
2. Use any screen coordinate tool (e.g. built-in Windows Magnifier, or a simple Python script with `pyautogui.displayMousePosition()`).
3. Hover over **loadout 1** (top-left) and note the X, Y values.
4. Hover over **loadout 2** (to the right) and note the difference in X — that's your `offset`.
5. Update `config.json` and restart.

## Usage

```bash
python loadout_swap.py
```

The script listens for your configured hotkeys globally. Press the bound key for a loadout and the macro fires. Press `exit_key` (default `Pause`) to stop the script.

Press `reload_key` (default `Home`) to reload `config.json` without restarting.

## Troubleshooting

### Modifier hotkeys (Ctrl/Shift/Alt) not working

Some background software (AutoHotkey, gaming mouse/keyboard software, Discord overlay) may intercept modifiers before pynput sees them. Try:
- Closing other macro/key-remapping tools
- Using a different modifier combination
- Switching to a non-modifier keybind

### Focus guard not working

The focus guard only works on Windows with `pywin32` installed. On other platforms it is silently disabled (macro fires in any window).

### Mouse clicks in the wrong place

- The coordinates are **absolute screen pixels**, not relative to the game window. If you move the Destiny 2 window, you must update `start_x` and `start_y`.
- Increase `delay_menu_open` / `delay_nav` / `delay_click` if your game is running at low FPS and the menu takes longer to open.

### Script not responding to keys

- Make sure no other program is using the same global hotkey.
- Check that the script window is not minimized in a way that pauses execution.

## License

Use at your own risk. See anti-cheat warning above.
