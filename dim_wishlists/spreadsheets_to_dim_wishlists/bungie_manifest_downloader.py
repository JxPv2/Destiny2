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
    CONFIG_FILE,
)

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS
# ==============================================================================
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
STATE_FILE = SYSTEM_PATHS["state_file"]
OUTPUT_DIR = SYSTEM_PATHS["manifest_dir"]

os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
logger = logging.getLogger("ManifestDownloader")
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
custom_formatter = SmartIndentFormatter(fmt=LOG_LAYOUT)

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(custom_formatter)
logger.addHandler(file_handler)

# Default component tables required to analyze weapons, perks, and rolls
DEFAULT_COMPONENTS = [
    "DestinyInventoryItemDefinition",
    "DestinyPlugSetDefinition",
    "DestinySandboxPerkDefinition",
    "DestinySocketTypeDefinition"
]

# ==============================================================================
# SECTION 3: CORE DOWNLOAD PIPELINE
# ==============================================================================
def execute_manifest_download():
    logger.info("=" * 50)
    logger.info("🚀 Initializing Destiny 2 Live Manifest Downloader...")
    logger.info("=" * 50)

    state = load_json_file(STATE_FILE, lambda: {"bungie_manifest": {}, "spreadsheets": {}})

    if "bungie_manifest" not in state:
        state["bungie_manifest"] = {}
    manifest_state = state["bungie_manifest"]

    download_required = manifest_state.get("bungie_manifest_download_required", False)
    compile_required = manifest_state.get("bungie_manifest_compile_required", False)

    PROCESSED_MANIFEST_PATH = os.path.join(OUTPUT_DIR, "bungie_manifest_processed.json")

    # If compile is needed but download is not, try compiling from cached raw files
    if compile_required and not download_required:
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            logger.info("🔄 Compile required but processed manifest missing. Attempting compilation from cached raw files...")
            from bungie_manifest_compiler import compile_manifest
            try:
                compile_manifest()
                logger.info("🎉 Manifest compilation completed from cached raw files.")
                manifest_state["bungie_manifest_compile_required"] = False
                save_json_file(STATE_FILE, state)
            except Exception as compile_error:
                logger.critical(f"❌ Manifest compilation failed: {compile_error}")
                logger.error("⚠️ Forcing download retry on next run.")
                manifest_state["bungie_manifest_download_required"] = True
                save_json_file(STATE_FILE, state)
        else:
            logger.info("🟩 Processed manifest already exists. Compile flag may be stale. Clearing.")
            manifest_state["bungie_manifest_compile_required"] = False
            save_json_file(STATE_FILE, state)
        return

    if not download_required:
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            logger.warning("⚠️ Processed manifest file missing despite up-to-date version flag. Forcing compilation from existing raw downloads...")
            from bungie_manifest_compiler import compile_manifest
            try:
                compile_manifest()
                logger.info("🎉 Manifest compilation completed from cached raw files.")
                manifest_state["bungie_manifest_compile_required"] = False
                save_json_file(STATE_FILE, state)
            except Exception as compile_error:
                logger.critical(f"❌ Manifest compilation failed: {compile_error}")
                logger.error("⚠️ Retaining compile flag in an UNSTABLE state so the pipeline retries next run.")
                manifest_state["bungie_manifest_compile_required"] = True
                save_json_file(STATE_FILE, state)
        else:
            logger.info("🟩 Manifest state up to date. No fresh database download required. Exiting pipeline stage.")
        return

    from bungie_manifest_compiler import compile_manifest

    if not os.path.exists(CONFIG_FILE):
        logger.critical(f"Aborting execution: Profile layout file '{CONFIG_FILE}' is missing.")
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    api_config = config.get("bungie_api", {})
    api_key = api_config.get("api_key")
    target_components = api_config.get("components", DEFAULT_COMPONENTS)

    max_tries = 3
    delay = 5

    logger.info("Querying Bungie metadata endpoint to extract raw asset components...")
    manifest_meta_url = "https://www.bungie.net/Platform/Destiny2/Manifest/"
    headers = {"X-API-Key": api_key} if api_key else {}
    component_paths = None

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(manifest_meta_url, headers=headers, timeout=10)
            response.raise_for_status()
            meta_data = response.json()

            if meta_data.get("ErrorCode") != 1:
                logger.error(f"❌ Bungie API rejection: {meta_data.get('ErrorStatus')} -> {meta_data.get('Message')}")
                return

            component_paths = meta_data.get("Response", {}).get("jsonWorldComponentContentPaths", {}).get("en", {})
            break

        except (requests.RequestException, ValueError) as e:
            if attempt < max_tries:
                logger.warning(f"⚠️ Bungie Metadata endpoint unresolved (Attempt {attempt}/{max_tries}). Retrying in {delay}s... Error: {e}")
                time.sleep(delay)
            else:
                logger.critical(f"❌ Critical failure: Failed to sync updated manifest metadata URLs after {max_tries} attempts: {e}")
                return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    download_failures = 0

    for component in target_components:
        if not component_paths or component not in component_paths:
            logger.warning(f"⚠️ Requested definition table '{component}' not found in active Bungie metadata schema. Skipping.")
            continue

        relative_path = component_paths[component]
        download_url = f"https://www.bungie.net{relative_path}"
        output_file_path = os.path.join(OUTPUT_DIR, f"{component}.json")

        logger.info(f"📥 Syncing component database payload: '{component}'...")

        for attempt in range(1, max_tries + 1):
            try:
                component_res = requests.get(download_url, headers=headers, timeout=45)
                component_res.raise_for_status()

                parsed_data = component_res.json()

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

    if download_failures == 0:
        logger.info("🎉 All requested Bungie asset databases extracted and synchronized successfully.")

        try:
            logger.info("🔄 Automatically launching Manifest Compiler...")
            compile_manifest()
        except Exception as compile_error:
            logger.critical(f"❌ Manifest compilation failed post-download: {compile_error}")
            logger.error("⚠️ Retaining update flags in an UNSTABLE state so the pipeline retries next run.")
            return

        manifest_state["bungie_manifest_download_required"] = False
        manifest_state["bungie_manifest_compile_required"] = False
        manifest_state["last_check"] = datetime.now(timezone.utc).isoformat()

        save_json_file(STATE_FILE, state)
        logger.info("Local configuration cache flags set to STABLE state.")
    else:
        logger.error(f"⚠️ Manifest compilation complete with errors. Total failures encountered: {download_failures}. Retaining update flags.")

if __name__ == "__main__":
    setup_root_console_logging()
    execute_manifest_download()