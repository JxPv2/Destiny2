import os
import time
import hashlib
import logging
import urllib.parse
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yaml

from pipeline_utils import (
    bootstrap_system_paths,
    SmartIndentFormatter,
    setup_root_console_logging,
    load_json_file,
    save_json_file,
    get_current_timestamp,
    CONFIG_FILE,
)

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS
# ==============================================================================
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
DOWNLOAD_DIR = SYSTEM_PATHS["download_dir"]
STATE_FILE = SYSTEM_PATHS["state_file"]

os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
logger = logging.getLogger("VersionChecker")
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

formatter = SmartIndentFormatter("%(asctime)s [%(levelname)s] -> %(message)s")

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# LoggerAdapters for hierarchical indentation
class IndentAdapter(logging.LoggerAdapter):
    def __init__(self, logger, indent_level):
        super().__init__(logger, {})
        self.indent_level = indent_level

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["indent"] = self.indent_level
        return msg, kwargs

section_logger = IndentAdapter(logger, 1)   # 2 spaces
detail_logger = IndentAdapter(logger, 2)    # 4 spaces

# ==============================================================================
# SECTION 3: UTILITY HELPERS
# ==============================================================================
def calculate_sha256(byte_stream):
    """ Generates standard deterministic sha256 checksum strings from incoming byte arrays. """
    return hashlib.sha256(byte_stream).hexdigest()

def sanitize_filename_part(text):
    """ Normalizes configuration strings to clear safe filename elements. """
    return text.lower().strip().replace(" ", "_")

def extract_date_from_response(response):
    """ Parses Content-Disposition to extract the date from the online file title. """
    content_disp = response.headers.get("Content-Disposition", "")
    if not content_disp:
        return None

    fname_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', content_disp, re.IGNORECASE)
    if not fname_match:
        return None

    filename = fname_match.group(1).strip('"').strip("'")
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if not date_match:
        return None

    return date_match.group(1)

# ==============================================================================
# SECTION 4: NETWORK LAYER ENDPOINT RESOLVERS
# ==============================================================================
def fetch_bungie_manifest_version(api_key=None, max_tries=3, delay=5):
    """ Connects to the live Bungie API endpoint to check version markers. """
    url = "https://www.bungie.net/Platform/Destiny2/Manifest/"
    headers = {"X-API-Key": api_key} if api_key else {}

    for attempt in range(1, max_tries + 1):
        try:
            detail_logger.info(f"Requesting live Bungie Manifest version (Attempt {attempt}/{max_tries})...")
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("ErrorCode") != 1:
                detail_logger.warning(f"⚠️ Bungie API internal fault wrapper code reported error: {data.get('Status')}")
                return None
            return data.get("Response", {}).get("version")
        except requests.RequestException as e:
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} hitting Bungie API: {e}")
            if attempt < max_tries:
                time.sleep(delay)
    detail_logger.error("❌ Critical network failure: Exhausted all retries attempting to query Bungie Manifest endpoint.")
    return None

def fetch_csv_content_by_gid(spreadsheet_id, gid, file_path, max_tries=3, delay=5):
    """ Pulls dedicated plain text target table values using specific spreadsheet GID keys. """
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code == 200:
                if not response.content or len(response.content) < 10:
                    detail_logger.warning(f"⚠️ Empty or near-empty CSV response for GID {gid}. Treating as failure.")
                    return None
                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ GID {gid} returned HTML instead of CSV. Sheet may be private or rate-limited.")
                    return None
                online_date = extract_date_from_response(response)
                if online_date:
                    date_path = os.path.splitext(file_path)[0] + ".date"
                    with open(date_path, "w", encoding="utf-8") as df:
                        df.write(online_date)
                return response.content
            elif response.status_code == 404:
                detail_logger.error(f"❌ HTTP 404 Error: Resource ID/GID sequence not found online [ID: {spreadsheet_id}, GID: {gid}]")
                return None
            else:
                detail_logger.warning(f"⚠️ Unexpected HTTP status response code {response.status_code} received on attempt {attempt}/{max_tries}")
        except requests.RequestException as e:
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} pulling GID {gid}: {e}")
            if attempt < max_tries:
                time.sleep(delay)
    return None

def fetch_csv_content_by_name(spreadsheet_id, sheet_name, max_tries=3, delay=5):
    """ Fallback querying system pulling sheet layouts directly by string literals. """
    encoded_name = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={encoded_name}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code == 200:
                if not response.content or len(response.content) < 10:
                    detail_logger.warning(f"⚠️ Empty or near-empty CSV response for sheet '{sheet_name}'. Treating as failure.")
                    return None
                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ Sheet '{sheet_name}' returned HTML instead of CSV. Sheet may be private or rate-limited.")
                    return None
                payload = response.text
                if "google.visualization.Query.setResponse" in payload and "error" in payload.lower():
                    detail_logger.warning(f"⚠️ Google Visualization Engine engine threw internal schema exception lookup for sheet name: '{sheet_name}'")
                    return None
                return response.content
        except requests.RequestException as e:
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} pulling name '{sheet_name}': {e}")
            if attempt < max_tries:
                time.sleep(delay)
    return None

def fetch_xlsx_workbook(spreadsheet_id, gid, file_path, max_tries=3, delay=5):
    """ Downloads raw binary compressed multi-tab workbook streams from target resources. """
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx&gid={gid}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code == 200:
                if not response.content or len(response.content) < 100:
                    detail_logger.warning(f"⚠️ Empty or near-empty XLSX response for GID {gid}. Treating as failure.")
                    return None
                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ GID {gid} returned HTML instead of XLSX. Sheet may be private or rate-limited.")
                    return None
                online_date = extract_date_from_response(response)
                if online_date:
                    date_path = os.path.splitext(file_path)[0] + ".date"
                    with open(date_path, "w", encoding="utf-8") as df:
                        df.write(online_date)
                    detail_logger.info(f"📆 Captured spreadsheet date header metadata value: {online_date}")
                return response.content
            else:
                detail_logger.warning(f"⚠️ XLSX endpoint responded with standard non-200 connection status flag: {response.status_code}")
        except requests.RequestException as e:
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} pulling XLSX workbook: {e}")
            if attempt < max_tries:
                time.sleep(delay)
    return None

# ==============================================================================
# SECTION 5: WORKBOOK PROCESSOR (for parallel execution)
# ==============================================================================
def process_single_workbook(wb, ss_id, ss_key, ss_state, download_dir):
    """Downloads and checks a single workbook. Returns (config_name, updated_entry, changed)."""
    config_gid = str(wb.get("gid"))
    config_name = wb.get("name")
    use_xlsx = wb.get("use_xlsx", False)
    fallback_used = False

    clean_ss_name = sanitize_filename_part(ss_key)
    clean_wb_name = sanitize_filename_part(config_name)
    file_extension = "xlsx" if use_xlsx else "csv"
    target_filename = f"{clean_ss_name}_{clean_wb_name}.{file_extension}"
    file_path = os.path.join(download_dir, target_filename)

    file_missing = not os.path.exists(file_path)

    csv_content = fetch_csv_content_by_gid(ss_id, config_gid, file_path)

    if not csv_content:
        detail_logger.warning(f"❌ Problem resolving GID '{config_gid}' for '{config_name}'. Trying fallback name lookup routing pass...")
        csv_content = fetch_csv_content_by_name(ss_id, config_name)
        if csv_content:
            fallback_used = True
        else:
            detail_logger.error(f"❌ CRITICAL CONFIG SPECIFICATION ERROR: Both GID and Name lookup sweeps completely failed for tab target '{config_name}'. Skipping parsing block.")
            return config_name, None, False

    current_hash = calculate_sha256(csv_content)
    wb_state = ss_state["workbooks"].get(config_name, {})
    old_hash = wb_state.get("local_saved_hash")

    updated_wb_entry = {
        "local_saved_hash": current_hash,
        "last_check": get_current_timestamp()
    }

    route_label = "Name Fallback" if fallback_used else f"GID {config_gid}"
    changed = False

    if old_hash != current_hash or file_missing:
        if old_hash != current_hash:
            detail_logger.info(f"🚨 UPDATE DETECTED [{route_label}]: Content mutated for tracking tab row object '{config_name}' inside workbook profile '{ss_key}'.")
            updated_wb_entry["workbook_scrape_update_required"] = True
            changed = True
        else:
            detail_logger.info(f"📁 LOCAL RECONSTRUCTION [{route_label}]: File asset not found locally for '{config_name}'. Forcing pipeline execution state flags to trigger downstream re-ingestion.")
            updated_wb_entry["workbook_scrape_update_required"] = True
            changed = True

        if use_xlsx:
            detail_logger.info(f"🔄 Component designated as Excel layout. Triggering dual format binary fetch for XLSX execution block...")
            xlsx_content = fetch_xlsx_workbook(ss_id, config_gid, file_path)
            if xlsx_content:
                with open(file_path, "wb") as f_out:
                    f_out.write(xlsx_content)
                detail_logger.info(f"💾 Excel sheet payload written safely down to disk layout target space: {file_path}")
            else:
                detail_logger.error(f"❌ Error fetching XLSX structural byte array stream for '{config_name}'. File asset tracking remains un-synchronized.")
                return config_name, None, False
        else:
            with open(file_path, "wb") as f_out:
                f_out.write(csv_content)
            detail_logger.info(f"💾 Delimited flat data text structure stream written safely down to disk layout target space: {file_path}")
    else:
        detail_logger.info(f"🟩 UP TO DATE [{route_label}]: Specifications data lines for sheet component '{config_name}' match cache records perfectly.")
        updated_wb_entry["workbook_scrape_update_required"] = wb_state.get("workbook_scrape_update_required", False)

    return config_name, updated_wb_entry, changed

# ==============================================================================
# SECTION 6: MAIN SYNCHRONIZATION RUNTIME CORE
# ==============================================================================
def process_checks():
    """ Orchestrates pipeline execution flow parameters across live external states. """
    logger.info("=" * 50)
    logger.info("🚀 Starting version check and synchronization loop...")
    logger.info("=" * 50)

    if not os.path.exists(CONFIG_FILE):
        logger.critical(f"Aborting execution layout routine: Missing core config file at path '{CONFIG_FILE}'")
        return

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    state = load_json_file(STATE_FILE, lambda: {"spreadsheets": {}, "bungie_manifest": {}})

    if "spreadsheets" not in state:
        state["spreadsheets"] = {}
    if "bungie_manifest" not in state:
        state["bungie_manifest"] = {}

    section_logger.info("Checking live Destiny 2 Manifest version from Bungie...")
    api_key = config.get("bungie_api", {}).get("api_key") if "bungie_api" in config else None
    live_version = fetch_bungie_manifest_version(api_key)

    if live_version:
        manifest_state = state["bungie_manifest"]
        old_version = manifest_state.get("local_saved_version")

        if "wishlist_update_required" not in manifest_state:
            manifest_state["wishlist_update_required"] = False

        manifest_state["local_saved_version"] = live_version
        manifest_state["last_check"] = get_current_timestamp()

        if old_version != live_version:
            detail_logger.info(f"🚨 UPDATE DETECTED: Bungie Manifest changed from '{old_version}' to '{live_version}'")
            manifest_state["bungie_manifest_download_required"] = True
            manifest_state["bungie_manifest_compile_required"] = True
            manifest_state["wishlist_update_required"] = True
            detail_logger.info("🔄 Global wishlist rebuild flag set: all DIM wishlists will be regenerated after manifest compile.")
        else:
            detail_logger.info("🟩 Up to date: Bungie Manifest version matches local cache. No structural compilation changes needed.")
    else:
        detail_logger.warning("⚠️ Could not verify live manifest version string. Retaining old registry parameters to preserve local safety limits.")

    spreadsheets_config = config.get("spreadsheets", {})

    for ss_key, ss_data in spreadsheets_config.items():
        ss_id = ss_data.get("id")
        workbooks = ss_data.get("workbooks", [])

        section_logger.info(f"Processing spreadsheet engine resource target profile: '{ss_key}'")

        if ss_key not in state["spreadsheets"]:
            state["spreadsheets"][ss_key] = {"wishlist_update_required": False, "workbooks": {}}

        ss_state = state["spreadsheets"][ss_key]

        if "wishlist_update_required" not in ss_state:
            ss_state["wishlist_update_required"] = False

        # Process workbooks in parallel with max 4 concurrent downloads
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_wb = {
                executor.submit(process_single_workbook, wb, ss_id, ss_key, ss_state, DOWNLOAD_DIR): wb 
                for wb in workbooks
            }

            for future in as_completed(future_to_wb):
                config_name, updated_entry, changed = future.result()
                if updated_entry is not None:
                    ss_state["workbooks"][config_name] = updated_entry
                    if changed:
                        ss_state["wishlist_update_required"] = True

    section_logger.info("=" * 50)
    section_logger.info("Synchronization check complete. Pipeline gating flags successfully set.")

    save_json_file(STATE_FILE, state)

if __name__ == "__main__":
    setup_root_console_logging()
    process_checks()