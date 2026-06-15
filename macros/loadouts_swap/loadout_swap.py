import json
import re
import sys
import threading
import time
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

import pyautogui
from pynput import keyboard as kb

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("loadout_swap")

# ─── Anti-cheat warning ───
ANTI_CHEAT_WARNING = """
╔═══════════════════════════════════════════════════════════════╗
║  WARNING: Destiny 2 uses BattlEye anti-cheat.                 ║
║  External input automation may be detected and can result     ║
║  in permanent account bans. Use this tool at your own risk.   ║
╚═══════════════════════════════════════════════════════════════╝
"""

# ─── Config handling ───
CONFIG_PATH = Path(__file__).with_name("config.json")


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments from JSON text."""
    text = re.sub(r"//.*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


DEFAULT_CONFIG = """{
    // --- UI Navigation Keys ---
    // Keys used to open/close the in-game loadout menu.
    // NOTE: menu_key / nav_key / exit_key use pyautogui key names.
    "menu_key": "f1",
    "nav_key": "left",
    "exit_key": "pause",

    // --- Grid Position ---
    // Starting coordinates for loadout 1 (top-left) and pixel spacing.
    // 1920*1080: start_x=115 | start_y=384 | offset=97
    "start_x": 115,
    "start_y": 384,
    "offset": 97,

    // --- Timing (seconds) ---
    // Increase these if your system or game is running at low FPS.
    "delay_menu_open": 0.45,
    "delay_nav": 0.45,
    "delay_click": 0.45,

    // --- Window Focus Guard ---
    // The macro only fires if the active window title contains ANY of these.
    "window_title_substrings": ["Destiny 2"],

    // --- Keybind Format Guide ---
    // Modifiers must be written in this exact order: ctrl, shift, alt, cmd
    //   "1" to "0"                 = top-row number keys
    //   "numpad_1" to "numpad_0"   = numpad keys
    //   "-" or "+"                 = minus / plus keys
    //   "ctrl_1"                   = hold Ctrl + press 1
    //   "shift_numpad_1"           = hold Shift + press numpad 1
    //   "alt_q"                    = hold Alt + press q
    //   "ctrl_shift_1"             = hold Ctrl+Shift + press 1
    "keybinds": {
        "loadout1":  "numpad_1",
        "loadout2":  "numpad_2",
        "loadout3":  "numpad_3",
        "loadout4":  "",
        "loadout5":  "numpad_4",
        "loadout6":  "numpad_5",
        "loadout7":  "numpad_6",
        "loadout8":  "",
        "loadout9":  "numpad_7",
        "loadout10": "numpad_8",
        "loadout11": "numpad_9",
        "loadout12": "",
        "loadout13": "/",
        "loadout14": "*",
        "loadout15": "-",
        "loadout16": "",
        "loadout17": "",
        "loadout18": "",
        "loadout19": "",
        "loadout20": ""
    },

    // --- Extra ---
    "reload_key": "home",
    "debug": false
}"""


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        logger.info(f"Created default config: {CONFIG_PATH}")
        return json.loads(strip_comments(DEFAULT_CONFIG))

    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = f.read()
    try:
        cfg = json.loads(strip_comments(raw))
    except json.JSONDecodeError as e:
        logger.error(f"Config JSON error: {e}")
        sys.exit(1)
    return cfg


def validate_config(cfg: Dict[str, Any]) -> None:
    required = (
        "menu_key", "nav_key", "exit_key",
        "start_x", "start_y", "offset", "keybinds",
    )
    for key in required:
        if key not in cfg:
            raise ValueError(f"Missing required config key: {key}")
    if not isinstance(cfg["keybinds"], dict):
        raise ValueError("keybinds must be an object")


# ─── Globals (populated by apply_config) ───
CFG: Dict[str, Any] = {}
KEYBINDS: Dict[str, str] = {}
ALL_KEYBINDS: Dict[str, str] = {}
MENU_KEY: str = ""
NAV_KEY: str = ""
EXIT_KEY: str = ""
RELOAD_KEY: str = ""
START_X: int = 0
START_Y: int = 0
OFFSET: int = 0
WINDOW_TITLES: list = []
DEBUG: bool = False
DELAYS: Dict[str, float] = {}


def apply_config(cfg: Dict[str, Any]) -> None:
    global CFG, KEYBINDS, ALL_KEYBINDS, MENU_KEY, NAV_KEY, EXIT_KEY, RELOAD_KEY
    global START_X, START_Y, OFFSET, WINDOW_TITLES, DEBUG, DELAYS

    CFG = cfg
    validate_config(cfg)

    MENU_KEY = cfg["menu_key"]
    NAV_KEY = cfg["nav_key"]
    EXIT_KEY = cfg["exit_key"]
    RELOAD_KEY = cfg.get("reload_key", "")
    START_X = cfg["start_x"]
    START_Y = cfg["start_y"]
    OFFSET = cfg["offset"]
    WINDOW_TITLES = [s.lower() for s in cfg.get("window_title_substrings", ["Destiny 2"])]
    DEBUG = cfg.get("debug", False)

    ALL_KEYBINDS = cfg["keybinds"]
    KEYBINDS = {k: v for k, v in ALL_KEYBINDS.items() if v}

    DELAYS = {
        "menu_open": cfg.get("delay_menu_open", 0.45),
        "nav": cfg.get("delay_nav", 0.45),
        "click": cfg.get("delay_click", 0.45),
    }

    # We handle sleeps manually; keep pyautogui's internal pause tiny.
    pyautogui.PAUSE = 0.01


def reload_config() -> None:
    logger.info("Reloading config...")
    try:
        cfg = load_config()
        apply_config(cfg)
        logger.info("Config reloaded successfully.")
        print_keybinds()
    except Exception as e:
        logger.error(f"Failed to reload config: {e}")


# ─── Focus guard ───
try:
    import win32gui
    _PLATFORM = "windows"
except ImportError:
    _PLATFORM = "other"


def get_active_window_title() -> str:
    if _PLATFORM != "windows":
        return ""
    try:
        return win32gui.GetWindowText(win32gui.GetForegroundWindow())
    except Exception:
        return ""


def is_target_focused() -> bool:
    if _PLATFORM != "windows":
        return True
    try:
        title = get_active_window_title().lower()
        return any(sub in title for sub in WINDOW_TITLES)
    except Exception:
        return False


# ─── Macro lock ───
_macro_lock = threading.Lock()


def perform_macro(loadout: int) -> None:
    if not _macro_lock.acquire(blocking=False):
        logger.debug("Macro already running, skipping.")
        return

    try:
        if DEBUG:
            col = (loadout - 1) % 4
            row = (loadout - 1) // 4
            x = START_X + col * OFFSET
            y = START_Y + row * OFFSET
            logger.info(f"[DEBUG] Would swap to loadout {loadout} at ({x}, {y})")
            return

        col = (loadout - 1) % 4
        row = (loadout - 1) // 4
        x = START_X + col * OFFSET
        y = START_Y + row * OFFSET

        logger.info(f"Swapping to loadout {loadout} at ({x}, {y})")

        pyautogui.press(MENU_KEY)
        time.sleep(DELAYS["menu_open"])

        pyautogui.press(NAV_KEY)
        time.sleep(DELAYS["nav"])

        pyautogui.moveTo(x, y)
        pyautogui.click()
        time.sleep(DELAYS["click"])

        pyautogui.press(MENU_KEY)

    except Exception as e:
        logger.error(f"Macro error: {e}")
    finally:
        _macro_lock.release()


# ─── Keyboard handling ───
MODIFIER_MAP = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
}

MODIFIER_ORDER = ("ctrl", "shift", "alt", "cmd")

VK_TOPROW = {
    ord("1"): "1", ord("2"): "2", ord("3"): "3", ord("4"): "4",
    ord("5"): "5", ord("6"): "6", ord("7"): "7", ord("8"): "8",
    ord("9"): "9", ord("0"): "0",
}

VK_NUMPAD = {
    97: "1", 98: "2", 99: "3", 100: "4", 101: "5",
    102: "6", 103: "7", 104: "8", 105: "9", 96: "0",
}

# pynput sometimes reports numpad keys as Key enum members (e.g. Key.numpad1)
# instead of KeyCode objects. Map them to the same format the config uses.
_NUMPAD_KEY_MAP = {
    "numpad0": "numpad_0", "numpad1": "numpad_1",
    "numpad2": "numpad_2", "numpad3": "numpad_3",
    "numpad4": "numpad_4", "numpad5": "numpad_5",
    "numpad6": "numpad_6", "numpad7": "numpad_7",
    "numpad8": "numpad_8", "numpad9": "numpad_9",
    # Alternative names pynput may use when NumLock is off
    "numpad_insert": "numpad_0",   "numpad_end": "numpad_1",
    "numpad_down": "numpad_2",     "numpad_page_down": "numpad_3",
    "numpad_left": "numpad_4",     "numpad_begin": "numpad_5",
    "numpad_right": "numpad_6",    "numpad_home": "numpad_7",
    "numpad_up": "numpad_8",       "numpad_page_up": "numpad_9",
}

_current_mods: Counter = Counter()


def key_to_string(key) -> str:
    if isinstance(key, kb.Key):
        name = key.name.lower()
        if name in _NUMPAD_KEY_MAP:
            return _NUMPAD_KEY_MAP[name]
        return MODIFIER_MAP.get(name, name)
    elif isinstance(key, kb.KeyCode):
        # VK codes are the most reliable way to identify numpad keys.
        if key.vk in VK_NUMPAD:
            return f"numpad_{VK_NUMPAD[key.vk]}"
        elif key.vk in VK_TOPROW:
            return VK_TOPROW[key.vk]
        # When modifiers are held, some systems report a control character in
        # key.char but still keep the correct vk. If char is a plain digit,
        # treat it as a top-row number (numpad would have been caught above).
        elif key.char and key.char.isdigit():
            return key.char.lower()
        elif key.char:
            return key.char.lower()
        else:
            return f"vk_{key.vk}"
    return str(key).lower()


def get_active_key_string(key) -> Optional[str]:
    base = key_to_string(key)
    if base in MODIFIER_MAP.values():
        return None

    mods = [m for m in MODIFIER_ORDER if m in _current_mods]
    if mods:
        return f"{'_'.join(mods)}_{base}"
    return base


def print_keybinds() -> None:
    print("\n" + "=" * 40)
    print("Destiny 2 Loadout Swap Macro")
    print("=" * 40)
    print(f"\nExit key: [{EXIT_KEY}]")
    if RELOAD_KEY:
        print(f"Reload key: [{RELOAD_KEY}]")
    print("\nLoadout Keybinds:")
    for loadout, bind in ALL_KEYBINDS.items():
        display = bind if bind else ""
        print(f"  {loadout}: {display}")
    print("\nListening for keybinds...")
    print("=" * 40)


# ─── DEBUG: uncomment to see exactly what pynput reports ───
def _debug_key(key):
    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    name = getattr(key, "name", None)
    logger.info(f"RAW KEY: {key!r} | name={name} | vk={vk} | char={char!r}")


def on_press(key):
#     _debug_key(key)  # uncomment for diagnostics

    key_str = key_to_string(key)

    # ── 1. Track modifiers FIRST ──
    if key_str in MODIFIER_MAP:
        _current_mods[MODIFIER_MAP[key_str]] += 1
        return

    # ── 2. Build full key string (e.g. "ctrl_f5", "shift_numpad_1") ──
    active_key = get_active_key_string(key)
    if not active_key:
        return

    # ── 3. Check exit (raw key match preserves original behaviour) ──
    if key_str == EXIT_KEY.lower():
        logger.info("Exit key pressed. Stopping...")
        return False

    # ── 4. Check reload (must match exact, including modifiers) ──
    if RELOAD_KEY and active_key == RELOAD_KEY.lower():
        reload_config()
        return

    # ── 5. Focus guard ──
    if not is_target_focused():
        return

    # ── 6. Match loadout binding ──
    for loadout, bind in KEYBINDS.items():
        if active_key == bind.lower():
            num = int(loadout[7:])  # "loadout12" -> 12
            threading.Thread(
                target=perform_macro, args=(num,), daemon=True
            ).start()
            return


def on_release(key):
    key_str = key_to_string(key)
    if key_str in MODIFIER_MAP:
        mod = MODIFIER_MAP[key_str]
        _current_mods[mod] -= 1
        if _current_mods[mod] <= 0:
            del _current_mods[mod]


# ─── Entry point ───
def main() -> None:
    print(ANTI_CHEAT_WARNING)

    cfg = load_config()
    apply_config(cfg)
    print_keybinds()

    with kb.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

    logger.info("Stopped.")


if __name__ == "__main__":
    main()