# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
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
# Resolve canonical folders. INPUT_DIR is where the downloader drops the raw
# Bungie JSON tables; OUTPUT_FILE is the unified, filtered artifact consumed
# by the wishlist converter.
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
INPUT_DIR = SYSTEM_PATHS["manifest_dir"]
OUTPUT_FILE = os.path.join(INPUT_DIR, "bungie_manifest_processed.json")

os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
# Module-level logger isolated from root noise. We clear existing handlers to
# survive reloads in interactive environments without duplicate lines.
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
    """
    Determine whether a DestinyInventoryItemDefinition represents an "enhanced"
    perk (a higher-tier version introduced in later expansions).

    Bungie marks enhanced perks inconsistently across API versions, so we check
    three signals:
        1. tooltipNotifications displayStyle flag (most reliable when present).
        2. itemTypeDisplayName containing the word "enhanced".
        3. itemTypeAndTierDisplayName containing the word "enhanced".
    """
    # Signal 1: Bungie sometimes embeds a UI hint in tooltip notifications.
    notifications = item_data.get("tooltipNotifications", [])
    for note in notifications:
        if note.get("displayStyle") == "ui_display_style_enhanced_perk":
            return True

    # Signal 2 & 3: Fallback string matching on display metadata.
    item_type_display = item_data.get("itemTypeDisplayName", "").lower()
    item_type_tier = item_data.get("itemTypeAndTierDisplayName", "").lower()

    if "enhanced" in item_type_display or "enhanced" in item_type_tier:
        return True

    return False


# Module-level cache for plug-category ignore decisions. A single plug hash
# can appear in thousands of weapon sockets; caching avoids redundant dict
# lookups and string prefix checks.
_filter_cache = {}

def is_ignored_socket(plug_hash, raw_items, ignore_list):
    """
    Return True if the plug identified by plug_hash belongs to a category that
    the user wants excluded from the processed manifest (e.g., shaders, mods,
    ornaments, or subclass fragments that accidentally share socket layouts).

    The ignore_list comes from config.yaml and contains prefix strings such as
    "v400" or "crafting". We match with startswith() so a single prefix can
    cover an entire family of plug categories.
    """
    # Fast-path: we have already judged this hash in a previous socket.
    if plug_hash in _filter_cache:
        return _filter_cache[plug_hash]

    # Resolve the plug's full definition from the raw items table.
    plug_def = raw_items.get(str(plug_hash))
    if not plug_def:
        # Unknown plug hash (orphaned reference). Do not ignore; let it pass
        # through so downstream code can see the gap explicitly.
        _filter_cache[plug_hash] = False
        return False

    # Bungie categorizes plugs via plugCategoryIdentifier (e.g.,
    # "v400.plugs.weapons.masterworks.trackers" or "enhancements.seasonal_artifact").
    category = plug_def.get("plug", {}).get("plugCategoryIdentifier", "")

    # Any ignore_list prefix match disqualifies this plug.
    is_ignored = any(category.startswith(cat) for cat in ignore_list)
    _filter_cache[plug_hash] = is_ignored
    return is_ignored

# ==============================================================================
# SECTION 4: COMPILATION ENGINE LOGIC
# ==============================================================================
def compile_manifest():
    """
    Transform raw Bungie JSON tables into a lightweight, wishlist-friendly
    manifest containing only:
        - weapons   -> name -> list of {item_id, valid_perks}
        - exotic_armor -> name -> list of {item_id, valid_perks}
        - perks     -> name -> list of all hashes that share that name

    The compiler runs a two-pass algorithm over DestinyInventoryItemDefinition:
        Pass 1: Index every perk (itemType == 19) so we can resolve names and
                filter enhanced variants before weapons are processed.
        Pass 2: Walk every weapon (itemType == 3) and exotic armor
                (itemType == 2 + tierType == 6), traverse their socket entries,
                collect only non-ignored, non-enhanced perks, and group the
                results by display name.

    On success, the compile_required flag in the shared state file is cleared
    so the pipeline scheduler knows the artifact is fresh.
    """
    # Reset the category filter cache so repeated runs in the same process do
    # not carry stale decisions from a previous config.
    _filter_cache.clear()
    logger.info("=" * 50)
    logger.info("🚀 Initializing Local Bungie Manifest Compiler...")
    logger.info("=" * 50)
    logger.info(f"🧹 Filter cache cleared. Size: {len(_filter_cache)}")

    # --------------------------------------------------------------------------
    # Hydrate shared state so we can clear the compile flag on success.
    # --------------------------------------------------------------------------
    from pipeline_utils import load_json_file, save_json_file, bootstrap_system_paths
    SYSTEM_PATHS = bootstrap_system_paths()
    STATE_FILE = SYSTEM_PATHS["state_file"]
    state = load_json_file(STATE_FILE, lambda: {"bungie_manifest": {}, "spreadsheets": {}})
    if "bungie_manifest" not in state:
        state["bungie_manifest"] = {}
    manifest_state = state["bungie_manifest"]

    # --------------------------------------------------------------------------
    # Load filtering configuration
    # --------------------------------------------------------------------------
    # If the config is missing we degrade gracefully with an empty ignore list
    # rather than crashing. This lets a new clone run the compiler immediately
    # after downloading raw files, even before config.yaml is customized.
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file '{CONFIG_FILE}' missing. Proceeding with empty ignore list.")
        ignore_list = []
    else:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
            ignore_list = config.get("bungie_manifest_filtering", {}).get("ignored_plug_categories", [])

    # --------------------------------------------------------------------------
    # Validate raw inputs
    # --------------------------------------------------------------------------
    # The compiler only needs two of the four downloaded tables:
    #   - DestinyInventoryItemDefinition  (items, perks, plugs, everything)
    #   - DestinyPlugSetDefinition        (random-roll perk pools)
    items_path = os.path.join(INPUT_DIR, "DestinyInventoryItemDefinition.json")
    plugsets_path = os.path.join(INPUT_DIR, "DestinyPlugSetDefinition.json")

    if not os.path.exists(items_path) or not os.path.exists(plugsets_path):
        logger.critical(f"Aborting execution layout routine: Raw manifest targets are missing inside '{INPUT_DIR}'")
        return

    # --------------------------------------------------------------------------
    # Load raw JSON into memory
    # --------------------------------------------------------------------------
    # These files are large (DestinyInventoryItemDefinition is often 100+ MB).
    # We load them fully into RAM because the two-pass algorithm needs random
    # access by string hash. In a future iteration this could be swapped for
    # streaming or a local SQLite cache if memory becomes constrained.
    logger.info("Loading raw JSON definition maps into memory...")
    try:
        with open(items_path, "r", encoding="utf-8") as f:
            raw_items = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        # JSONDecodeError  -> downloader wrote a partial or corrupted file.
        # FileNotFoundError -> race condition or manual deletion since the
        #                      existence check above.
        logger.critical(f"❌ Cannot load '{items_path}': {e}. Delete or repair this file and re-run the downloader.")
        return
    except Exception as e:
        # Catch-all for unexpected I/O or memory errors during load.
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

    # --------------------------------------------------------------------------
    # Prepare output accumulators
    # --------------------------------------------------------------------------
    # perk_name_to_hashes: maps a perk display name to every hash that carries
    #   that name. Needed because Bungie sometimes duplicates perks across
    #   seasons with different hashes (e.g., "Outlaw" vs "Outlaw Refit").
    # perk_id_to_name: reverse lookup for Pass 2 so we can validate a plug
    #   hash is actually a known perk before adding it to a weapon.
    # processed_weapons / processed_exotics: grouped by display name because
    #   Bungie re-uses names across seasons (e.g., "Palindrome (Adept)").
    logger.info("Indexing perks and extracting item definitions in single pass...")
    perk_name_to_hashes = {}  # name -> list of all hashes
    perk_id_to_name = {}
    processed_weapons = {}
    processed_exotics = {}

    # ==============================================================================
    # PASS 1: Index all perks first (itemType 19)
    # ==============================================================================
    # Bungie assigns itemType 19 to "mods" in the broad API sense, but in
    # practice every weapon perk (including barrel, magazine, and trait perks)
    # carries this type. We index them first so Pass 2 can distinguish perks
    # from non-perk plugs (mods, shaders, masterwork trackers) without
    # re-scanning the entire 100 MB table.
    logger.info("Pass 1: Indexing all perks...")
    for item_id, item_data in raw_items.items():
        if item_data.get("itemType") != 19:
            continue
        # Skip enhanced perks. They are not selectable in normal rolls and
        # including them would bloat the wishlist with unobtainable combos.
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
    # itemType 3  -> weapon (kinetic, energy, power, ghosts, sparrows, ships).
    # itemType 2  -> armor. We further restrict to tierType 6 (exotic) because
    #                DIM wishlists only care about exotic armor stat plugs.
    logger.info("Pass 2: Processing weapons and exotic armor...")
    for item_id, item_data in raw_items.items():
        item_type = item_data.get("itemType")
        tier_type = item_data.get("inventory", {}).get("tierType")
        item_name = item_data.get("displayProperties", {}).get("name")

        # Some internal test items or dummy entries have no display name.
        if not item_name:
            continue

        is_weapon = (item_type == 3)
        is_exotic_armor = (item_type == 2 and tier_type == 6)

        if not (is_weapon or is_exotic_armor):
            continue

        hash_int = int(item_id)
        valid_perk_hashes = set()

        # ------------------------------------------------------------------
        # Socket traversal
        # ------------------------------------------------------------------
        # A weapon's roll possibilities are encoded in its socketEntries array.
        # Each socket entry describes one column (barrels, magazines, traits).
        # Bungie stores the selectable plugs in three possible places:
        #   1. reusablePlugSetHash / randomizedPlugSetHash -> points to a
        #      DestinyPlugSetDefinition that lists the full perk pool.
        #   2. reusablePlugItems inline -> fixed plugs (often curated rolls).
        #   3. singleInitialItemHash -> the default plug shown in the UI.
        for socket_entry in item_data.get("sockets", {}).get("socketEntries", []):
            plug_items = []

            # 1. Plug set (random rolls)
            # reusablePlugSetHash   -> static roll pool (e.g., fixed-barrel exotics).
            # randomizedPlugSetHash -> random roll pool (e.g., legendary drops).
            plug_set_hash = socket_entry.get("reusablePlugSetHash") or socket_entry.get("randomizedPlugSetHash")
            if plug_set_hash and str(plug_set_hash) in raw_plugsets:
                plug_set_def = raw_plugsets[str(plug_set_hash)]
                plug_items.extend(plug_set_def.get("reusablePlugItems", []))
                plug_items.extend(plug_set_def.get("randomizedPlugItems", []))

            # 2. Inline reusablePlugItems
            # These appear on exotics and some quest weapons with fixed perks.
            plug_items.extend(socket_entry.get("reusablePlugItems", []))

            # 3. Fixed/default perk
            # Even if a socket has no random pool, it always has a default plug.
            single_initial = socket_entry.get("singleInitialItemHash")
            if single_initial:
                plug_items.append({"plugItemHash": single_initial})

            # ------------------------------------------------------------------
            # Filter each plug through the gatekeeper
            # ------------------------------------------------------------------
            for plug_item in plug_items:
                p_hash = plug_item.get("plugItemHash")
                plug_data = raw_items.get(str(p_hash))

                # Drop ignored categories (masterwork trackers, shaders, etc.).
                if is_ignored_socket(p_hash, raw_items, ignore_list):
                    continue

                # Drop enhanced perks. They are crafting-only or endgame-only
                # and not part of normal random-roll wishlists.
                if plug_data and is_enhanced_perk(plug_data):
                    continue

                # Final validation: only keep hashes that were indexed in Pass 1.
                # This silently drops non-perk plugs (ammo types, cosmetics,
                # subclass fragments) that share socket layouts with weapons.
                if p_hash in perk_id_to_name:
                    valid_perk_hashes.add(p_hash)

        # Bundle the results. We keep a list per name because Bungie releases
        # multiple versions of the same weapon (Adept, Timelost, normal) with
        # different item hashes but identical display names.
        version_entry = {"item_id": hash_int, "valid_perks": sorted(list(valid_perk_hashes))}

        if is_weapon:
            processed_weapons.setdefault(item_name, []).append(version_entry)
        elif is_exotic_armor:
            processed_exotics.setdefault(item_name, []).append(version_entry)

    # --------------------------------------------------------------------------
    # Write the unified processed manifest
    # --------------------------------------------------------------------------
    final_manifest = {"weapons": processed_weapons, "exotic_armor": processed_exotics, "perks": perk_name_to_hashes}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out_file:
        json.dump(final_manifest, out_file, indent=2, ensure_ascii=False)

    logger.info(f"🎉 Clean manifest successfully written to: {OUTPUT_FILE}")
    logger.info(f"Mapped {len(processed_weapons)} weapons, {len(processed_exotics)} exotics.")

    # --------------------------------------------------------------------------
    # Clear compile flag on success
    # --------------------------------------------------------------------------
    # If we made it here, the artifact on disk is consistent with the raw
    # inputs. We clear the flag so the scheduler does not re-run the compiler
    # on the next 8-hour cycle.
    manifest_state["bungie_manifest_compile_required"] = False
    save_json_file(STATE_FILE, state)
    logger.info("✅ Manifest compile flag cleared from state.")

if __name__ == "__main__":
    setup_root_console_logging()
    compile_manifest()
