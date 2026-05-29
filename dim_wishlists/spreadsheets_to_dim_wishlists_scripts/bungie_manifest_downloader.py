# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
import os
import json
import time
import logging
from datetime import datetime, timezone
import requests
import yaml

from pipeline_utils import (
    bootstrap_system_paths,
    SmartIndentFormatter,
    setup_root_console_logging,
    load_json_file,
    save_json_file,
    setup_module_logger,
    ensure_manifest_state,
    CONFIG_FILE,
)

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS
# ==============================================================================
# Resolve canonical folder paths (logs, state, manifests) so the script can be
# run from any working directory without breaking relative lookups.
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
STATE_FILE = SYSTEM_PATHS["state_file"]
OUTPUT_DIR = SYSTEM_PATHS["manifest_dir"]

# Ensure the log directory exists before the FileHandler is attached.
os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
# Create a dedicated logger for this module. We isolate it from the root logger
# so other libraries' noise does not pollute our manifest-specific log files.
logger = setup_module_logger("bungie_manifest_downloader", LOG_DIR)

# ==============================================================================
# SECTION 3: CONSTANTS
# ==============================================================================
# Destiny 2 manifest is split into many "definitions" tables. These four are the
# minimum set required to reconstruct weapons, their plug (perk) sets, sandbox
# perk metadata, and socket typing information.
DEFAULT_COMPONENTS = [
    "DestinyInventoryItemDefinition",   # Weapons, armor, items, currencies
    "DestinyPlugSetDefinition",         # Perk pools associated with sockets
    "DestinySandboxPerkDefinition",     # Perk descriptions and display metadata
    "DestinySocketTypeDefinition"       # Socket behavior rules (intrinsic, mod, perk, etc.)
]

# ==============================================================================
# SECTION 4: CORE DOWNLOAD PIPELINE
# ==============================================================================
def execute_manifest_download():
    """
    Main orchestrator for the manifest lifecycle.

    State machine overview:
        1. Load persistent JSON state (tracks whether a download or compile is
           required and when the last successful run occurred).
        2. If only compilation is needed (raw JSON already on disk), compile.
        3. If nothing is required but the processed manifest is missing, attempt
           a recovery compilation from existing raw files.
        4. If a download is required, query Bungie's manifest metadata, resolve
           the current CDN URLs for each component table, download them, and
           then trigger the compiler to produce the unified processed manifest.
        5. Update state flags on success so the next pipeline run exits early.
    """

    # --------------------------------------------------------------------------
    # Phase 0: Banner & state hydration
    # --------------------------------------------------------------------------
    logger.info("=" * 80)
    logger.info("🚀 Initializing Destiny 2 Live Manifest Downloader...")
    logger.info("=" * 80)

    # Load the shared state file. If it does not exist yet, seed it with the
    # expected top-level keys so downstream code never has to check for KeyError.
    state = load_json_file(STATE_FILE, lambda: {"bungie_manifest": {}, "spreadsheets": {}})

    manifest_state = ensure_manifest_state(state)

    # These two booleans drive the entire flow. They are set by other scripts
    # (e.g., after a spreadsheet update or a version mismatch detection).
    download_required = manifest_state.get("bungie_manifest_download_required", False)
    compile_required = manifest_state.get("bungie_manifest_compile_required", False)

    PROCESSED_MANIFEST_PATH = os.path.join(OUTPUT_DIR, "bungie_manifest_processed.json")

    # --------------------------------------------------------------------------
    # Phase 1: Compile-only path (raw files already downloaded)
    # --------------------------------------------------------------------------
    # Another script may have flagged that the processed manifest is stale while
    # the raw Bungie JSON components are still current. In that case we skip the
    # network entirely and jump straight to compilation.
    if compile_required and not download_required:
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            logger.info("🔄 Compile required but processed manifest missing. Attempting compilation from cached raw files...")
            # Lazy import avoids circular dependency at module load time.
            from bungie_manifest_compiler import compile_manifest
            try:
                compile_manifest()
                logger.info("🎉 Manifest compilation completed from cached raw files.")
                manifest_state["bungie_manifest_compile_required"] = False
                save_json_file(STATE_FILE, state)
            except Exception as compile_error:
                # Compilation failed (e.g., corrupted raw JSON). Force a full
                # re-download on the next run by raising the download flag.
                logger.critical(f"❌ Manifest compilation failed: {compile_error}")
                logger.error("⚠️ Forcing download retry on next run.")
                manifest_state["bungie_manifest_download_required"] = True
                save_json_file(STATE_FILE, state)
        else:
            # Edge case: the compile flag was set but the artifact already exists.
            # Treat the flag as stale and clear it to avoid redundant work.
            logger.info("🟩 Processed manifest already exists. Compile flag may be stale. Clearing.")
            manifest_state["bungie_manifest_compile_required"] = False
            save_json_file(STATE_FILE, state)
        return

    # --------------------------------------------------------------------------
    # Phase 2: Nothing required path (up-to-date check)
    # --------------------------------------------------------------------------
    if not download_required:
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            # The processed file vanished (deleted, moved, or on a fresh clone).
            # Attempt to rebuild it from raw downloads before giving up.
            logger.warning("⚠️ Processed manifest file missing despite up-to-date version flag. Forcing compilation from existing raw downloads...")
            from bungie_manifest_compiler import compile_manifest
            try:
                compile_manifest()
                logger.info("🎉 Manifest compilation completed from cached raw files.")
                manifest_state["bungie_manifest_compile_required"] = False
                save_json_file(STATE_FILE, state)
            except Exception as compile_error:
                # Recovery failed. Leave compile_required=True so the pipeline
                # retries on its next scheduled run.
                logger.critical(f"❌ Manifest compilation failed: {compile_error}")
                logger.error("⚠️ Retaining compile flag in an UNSTABLE state so the pipeline retries next run.")
                manifest_state["bungie_manifest_compile_required"] = True
                save_json_file(STATE_FILE, state)
        else:
            # Happy path: everything exists and no flags are set.
            logger.info("🟩 Manifest state up to date. No fresh database download required. Exiting pipeline stage.")
        return

    # --------------------------------------------------------------------------
    # Phase 3: Full download path
    # --------------------------------------------------------------------------
    # From here onward download_required is True. We will hit Bungie's API,
    # download every component table, and then compile.

    # Import the compiler here so that if we exit early (e.g., missing config)
    # we do not pay the import cost. Note: this is placed before the config
    # check in the current code; the compiler will be invoked after downloads.
    from bungie_manifest_compiler import compile_manifest

    # The Bungie API key is optional for the public manifest metadata endpoint,
    # but supplying one increases rate-limit headroom and is required by Bungie
    # policy for automated consumers.
    if not os.path.exists(CONFIG_FILE):
        logger.critical(f"Aborting execution: Profile layout file '{CONFIG_FILE}' is missing.")
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    api_config = config.get("bungie_api", {})
    api_key = api_config.get("api_key")
    target_components = api_config.get("components", DEFAULT_COMPONENTS)

    # Retry policy for transient CDN / API hiccups.
    max_tries = 3
    delay = 5

    # --------------------------------------------------------------------------
    # Phase 3a: Resolve current component URLs from Bungie metadata
    # --------------------------------------------------------------------------
    # Bungie publishes a manifest metadata document that maps definition names
    # to their current CDN paths. These paths change with every game update.
    logger.info("Querying Bungie metadata endpoint to extract raw asset components...")
    manifest_meta_url = "https://www.bungie.net/Platform/Destiny2/Manifest/"
    headers = {"X-API-Key": api_key} if api_key else {}
    component_paths = None

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(manifest_meta_url, headers=headers, timeout=10)
            response.raise_for_status()
            meta_data = response.json()

            # Bungie wraps all responses in an envelope. ErrorCode 1 means success.
            if meta_data.get("ErrorCode") != 1:
                logger.error(f"❌ Bungie API rejection: {meta_data.get('ErrorStatus')} -> {meta_data.get('Message')}")
                return

            # Extract the English (en) component path map. Other locales exist but
            # the pipeline currently only needs English definitions.
            component_paths = meta_data.get("Response", {}).get("jsonWorldComponentContentPaths", {}).get("en", {})
            break

        except (requests.RequestException, ValueError) as e:
            # requests.RequestException  -> network-level failure (DNS, timeout, HTTP 5xx).
            # ValueError                 -> JSON decode failure (malformed payload).
            if attempt < max_tries:
                logger.warning(f"⚠️ Bungie Metadata endpoint unresolved (Attempt {attempt}/{max_tries}). Retrying in {delay}s... Error: {e}")
                time.sleep(delay)
            else:
                logger.critical(f"❌ Critical failure: Failed to sync updated manifest metadata URLs after {max_tries} attempts: {e}")
                return

    # Ensure the output directory exists before writing component files.
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Counter used to decide whether we can safely clear state flags later.
    download_failures = 0

    # --------------------------------------------------------------------------
    # Phase 3b: Download each component table
    # --------------------------------------------------------------------------
    for component in target_components:
        # If the metadata schema no longer lists a requested component (e.g.,
        # Bungie renamed a table), skip it rather than crashing.
        if not component_paths or component not in component_paths:
            logger.warning(f"⚠️ Requested definition table '{component}' not found in active Bungie metadata schema. Skipping.")
            continue

        # Bungie returns relative paths; prepend the CDN origin.
        relative_path = component_paths[component]
        download_url = f"https://www.bungie.net{relative_path}"
        output_file_path = os.path.join(OUTPUT_DIR, f"{component}.json")

        logger.info(f"📥 Syncing component database payload: '{component}'...")

        for attempt in range(1, max_tries + 1):
            try:
                # Per-component timeout is generous (45s) because some tables
                # (e.g., DestinyInventoryItemDefinition) are 50+ MB of JSON.
                component_res = requests.get(download_url, headers=headers, timeout=45)
                component_res.raise_for_status()

                parsed_data = component_res.json()

                # Write atomically-ish: json.dump to a fully-qualified path.
                # If this script is killed mid-write, the file may be partial,
                # but the compiler will blow up on the next run and trigger a
                # re-download via the state flags.
                with open(output_file_path, "w", encoding="utf-8") as out_file:
                    json.dump(parsed_data, out_file, indent=2, ensure_ascii=False)

                logger.info(f"✅ Successfully written component data: '{output_file_path}'")
                break

            except (requests.RequestException, ValueError) as e:
                if attempt < max_tries:
                    logger.warning(f"⚠️ Component '{component}' sync failed (Attempt {attempt}/{max_tries}). Retrying in {delay}s... Error: {e}")
                    time.sleep(delay)
                else:
                    logger.error(f"❌ Failed downloading component database payload '{component}' after {max_tries} attempts: {e}")
                    download_failures += 1

    # --------------------------------------------------------------------------
    # Phase 3c: Post-download compilation & state commit
    # --------------------------------------------------------------------------
    if download_failures == 0:
        # All components are fresh on disk. Invoke the compiler to merge them
        # into the unified processed manifest used by downstream wishlist logic.
        logger.info("🎉 All requested Bungie asset databases extracted and synchronized successfully.")

        try:
            logger.info("🔄 Automatically launching Manifest Compiler...")
            compile_manifest()
        except Exception as compile_error:
            # The raw files are good but the compiler choked. We leave the
            # download flag cleared (files are fine) but we do NOT clear the
            # compile flag if it exists, and we return without updating
            # last_check. The next run will attempt compilation again.
            logger.critical(f"❌ Manifest compilation failed post-download: {compile_error}")
            logger.error("⚠️ Retaining update flags in an UNSTABLE state so the pipeline retries next run.")
            return

        # Success: both download and compilation finished. Persist the stable
        # state so the next scheduled run exits immediately in Phase 2.
        manifest_state["bungie_manifest_download_required"] = False
        manifest_state["bungie_manifest_compile_required"] = False
        manifest_state["last_check"] = datetime.now(timezone.utc).isoformat()

        save_json_file(STATE_FILE, state)
        logger.info("Local configuration cache flags set to STABLE state.")
    else:
        # Partial failure: one or more components could not be downloaded.
        # Do NOT clear download_required; the next run will retry the full set.
        logger.error(f"⚠️ Manifest compilation complete with errors. Total failures encountered: {download_failures}. Retaining update flags.")

if __name__ == "__main__":
    # Attach a stdout StreamHandler to the root logger so console output is
    # visible when running manually. File logging is already configured above.
    setup_root_console_logging()
    execute_manifest_download()