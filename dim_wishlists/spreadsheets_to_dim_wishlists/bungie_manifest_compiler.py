import os
import json
import logging
import yaml
from datetime import datetime

from pipeline_utils import (
    bootstrap_system_paths,
    SmartIndentFormatter,
    setup_root_console_logging,
    CONFIG_FILE,
)

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS
# ==============================================================================
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
INPUT_DIR = SYSTEM_PATHS["manifest_dir"]
OUTPUT_FILE = os.path.join(INPUT_DIR, "bungie_manifest_processed.json")

os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
logger = logging.getLogger("ManifestCompiler")
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
custom_formatter = SmartIndentFormatter(fmt=LOG_LAYOUT)

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(custom_formatter)
logger.addHandler(file_handler)

# ==============================================================================
# SECTION 3: FILTERING ENGINE (GATEKEEPER)
# ==============================================================================
def is_enhanced_perk(item_data):
    notifications = item_data.get("tooltipNotifications", [])
    for note in notifications:
        if note.get("displayStyle") == "ui_display_style_enhanced_perk":
            return True

    item_type_display = item_data.get("itemTypeDisplayName", "").lower()
    item_type_tier = item_data.get("itemTypeAndTierDisplayName", "").lower()

    if "enhanced" in item_type_display or "enhanced" in item_type_tier:
        return True

    return False

_filter_cache = {}

def is_ignored_socket(plug_hash, raw_items, ignore_list):
    if plug_hash in _filter_cache:
        return _filter_cache[plug_hash]

    plug_def = raw_items.get(str(plug_hash))
    if not plug_def:
        _filter_cache[plug_hash] = False
        return False

    category = plug_def.get("plug", {}).get("plugCategoryIdentifier", "")

    is_ignored = any(category.startswith(cat) for cat in ignore_list)
    _filter_cache[plug_hash] = is_ignored
    return is_ignored

# ==============================================================================
# SECTION 4: COMPILATION ENGINE LOGIC
# ==============================================================================
def compile_manifest():
    _filter_cache.clear()
    logger.info("=" * 50)
    logger.info("🚀 Initializing Local Bungie Manifest Compiler...")
    logger.info("=" * 50)
    logger.info(f"🧹 Filter cache cleared. Size: {len(_filter_cache)}")

    # Load state to clear compile flag on success
    from pipeline_utils import load_json_file, save_json_file, bootstrap_system_paths
    SYSTEM_PATHS = bootstrap_system_paths()
    STATE_FILE = SYSTEM_PATHS["state_file"]
    state = load_json_file(STATE_FILE, lambda: {"bungie_manifest": {}, "spreadsheets": {}})
    if "bungie_manifest" not in state:
        state["bungie_manifest"] = {}
    manifest_state = state["bungie_manifest"]

    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file '{CONFIG_FILE}' missing. Proceeding with empty ignore list.")
        ignore_list = []
    else:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
            ignore_list = config.get("bungie_manifest_filtering", {}).get("ignored_plug_categories", [])

    items_path = os.path.join(INPUT_DIR, "DestinyInventoryItemDefinition.json")
    plugsets_path = os.path.join(INPUT_DIR, "DestinyPlugSetDefinition.json")

    if not os.path.exists(items_path) or not os.path.exists(plugsets_path):
        logger.critical(f"Aborting execution layout routine: Raw manifest targets are missing inside '{INPUT_DIR}'")
        return

    logger.info("Loading raw JSON definition maps into memory...")
    try:
        with open(items_path, "r", encoding="utf-8") as f:
            raw_items = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.critical(f"❌ Cannot load '{items_path}': {e}. Delete or repair this file and re-run the downloader.")
        return
    except Exception as e:
        logger.critical(f"❌ Unexpected error loading '{items_path}': {e}")
        return

    try:
        with open(plugsets_path, "r", encoding="utf-8") as f:
            raw_plugsets = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.critical(f"❌ Cannot load '{plugsets_path}': {e}. Delete or repair this file and re-run the downloader.")
        return
    except Exception as e:
        logger.critical(f"❌ Unexpected error loading '{plugsets_path}': {e}")
        return

    logger.info("Indexing perks and extracting item definitions in single pass...")
    perk_name_to_hashes = {}  # name -> list of all hashes
    perk_id_to_name = {}
    processed_weapons = {}
    processed_exotics = {}

    # ==============================================================================
    # PASS 1: Index all perks first (itemType 19)
    # ==============================================================================
    logger.info("Pass 1: Indexing all perks...")
    for item_id, item_data in raw_items.items():
        if item_data.get("itemType") != 19:
            continue
        if is_enhanced_perk(item_data):
            continue
        name = item_data.get("displayProperties", {}).get("name")
        if name:
            hash_int = int(item_id)
            perk_name_to_hashes.setdefault(name, []).append(hash_int)
            perk_id_to_name[hash_int] = name

    logger.info(f"Indexed {len(perk_id_to_name)} perks.")

    # ==============================================================================
    # PASS 2: Extract weapons and exotics (itemType 3 or 2+6)
    # ==============================================================================
    logger.info("Pass 2: Processing weapons and exotic armor...")
    for item_id, item_data in raw_items.items():
        item_type = item_data.get("itemType")
        tier_type = item_data.get("inventory", {}).get("tierType")
        item_name = item_data.get("displayProperties", {}).get("name")

        if not item_name:
            continue

        is_weapon = (item_type == 3)
        is_exotic_armor = (item_type == 2 and tier_type == 6)

        if not (is_weapon or is_exotic_armor):
            continue

        hash_int = int(item_id)
        valid_perk_hashes = set()

        for socket_entry in item_data.get("sockets", {}).get("socketEntries", []):
            plug_items = []

            plug_items = []

            # 1. Plug set (random rolls)
            plug_set_hash = socket_entry.get("reusablePlugSetHash") or socket_entry.get("randomizedPlugSetHash")
            if plug_set_hash and str(plug_set_hash) in raw_plugsets:
                plug_set_def = raw_plugsets[str(plug_set_hash)]
                plug_items.extend(plug_set_def.get("reusablePlugItems", []))
                plug_items.extend(plug_set_def.get("randomizedPlugItems", []))

            # 2. Inline reusablePlugItems
            plug_items.extend(socket_entry.get("reusablePlugItems", []))

            # 3. Fixed/default perk
            single_initial = socket_entry.get("singleInitialItemHash")
            if single_initial:
                plug_items.append({"plugItemHash": single_initial})

            for plug_item in plug_items:
                p_hash = plug_item.get("plugItemHash")
                plug_data = raw_items.get(str(p_hash))

                if is_ignored_socket(p_hash, raw_items, ignore_list):
                    continue

                if plug_data and is_enhanced_perk(plug_data):
                    continue

                if p_hash in perk_id_to_name:
                    valid_perk_hashes.add(p_hash)

        version_entry = {"item_id": hash_int, "valid_perks": sorted(list(valid_perk_hashes))}

        if is_weapon:
            processed_weapons.setdefault(item_name, []).append(version_entry)
        elif is_exotic_armor:
            processed_exotics.setdefault(item_name, []).append(version_entry)

    final_manifest = {"weapons": processed_weapons, "exotic_armor": processed_exotics, "perks": perk_name_to_hashes}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out_file:
        json.dump(final_manifest, out_file, indent=2, ensure_ascii=False)

    logger.info(f"🎉 Clean manifest successfully written to: {OUTPUT_FILE}")
    logger.info(f"Mapped {len(processed_weapons)} weapons, {len(processed_exotics)} exotics.")

    # Clear compile flag on success
    manifest_state["bungie_manifest_compile_required"] = False
    save_json_file(STATE_FILE, state)
    logger.info("✅ Manifest compile flag cleared from state.")

if __name__ == "__main__":
    setup_root_console_logging()
    compile_manifest()