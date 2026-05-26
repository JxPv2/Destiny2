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
# Resolve canonical folders so the script can be invoked from any cwd.
SYSTEM_PATHS = bootstrap_system_paths()
LOG_DIR = SYSTEM_PATHS["log_dir"]
DOWNLOAD_DIR = SYSTEM_PATHS["download_dir"]
STATE_FILE = SYSTEM_PATHS["state_file"]

os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# SECTION 2: LOGGING
# ==============================================================================
# Module-level logger for the version checker. It captures high-level stage
# transitions (spreadsheet processing banners) and network retry warnings.
# Per-workbook detail is emitted through IndentAdapter loggers for visual
# hierarchy.
logger = logging.getLogger("VersionChecker")
logger.setLevel(logging.INFO)

# Defensive reset: survive reloads in long-running scheduler processes.
if logger.hasHandlers():
    logger.handlers.clear()

formatter = SmartIndentFormatter("%(asctime)s [%(levelname)s] -> %(message)s")

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ==============================================================================
# LoggerAdapters for hierarchical indentation
# ==============================================================================
# These adapters inject an "indent" key into the LogRecord extra dict.
# SmartIndentFormatter reads this key and prepends spaces so nested output
# (spreadsheet -> workbook -> download attempt) is visually scannable.
class IndentAdapter(logging.LoggerAdapter):
    def __init__(self, logger, indent_level):
        super().__init__(logger, {})
        self.indent_level = indent_level

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["indent"] = self.indent_level
        return msg, kwargs

# Two indentation tiers:
#   section_logger -> spreadsheet-level banners (2 spaces).
#   detail_logger  -> per-workbook download results (4 spaces).
section_logger = IndentAdapter(logger, 1)
detail_logger = IndentAdapter(logger, 2)

# ==============================================================================
# SECTION 3: UTILITY HELPERS
# ==============================================================================
def calculate_sha256(byte_stream):
    """
    Generate a SHA-256 hex digest from a byte string.

    Used to detect content changes in downloaded Google Sheets. Even a single
    cell edit changes the checksum, so we can reliably determine whether the
    local copy is stale without parsing the CSV.
    """
    return hashlib.sha256(byte_stream).hexdigest()

def sanitize_filename_part(text):
    """
    Normalize a string into a safe filename fragment.

    Steps: lowercase, strip whitespace, replace spaces with underscores.
    Example: "Boss Damage" -> "boss_damage".
    """
    return text.lower().strip().replace(" ", "_")

def extract_date_from_response(response):
    """
    Parse the Content-Disposition header to extract a YYYY-MM-DD date.

    Google Sheets export responses sometimes include a filename like:
        Content-Disposition: attachment; filename="MySheet 2024-03-15.csv"
    We regex-search for an ISO-like date inside that filename. If found,
    we write it to a sidecar .date file so scrapers can read provenance
    without re-querying the API.

    Returns the date string or None if the header is absent or unparsable.
    """
    content_disp = response.headers.get("Content-Disposition", "")
    if not content_disp:
        return None

    # Regex: match filename=... allowing quoted or unquoted values.
    fname_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', content_disp, re.IGNORECASE)
    if not fname_match:
        return None

    # Strip surrounding quotes.
    filename = fname_match.group(1).strip('"').strip("'")
    # Look for YYYY-MM-DD anywhere in the filename.
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if not date_match:
        return None

    return date_match.group(1)

# ==============================================================================
# SECTION 4: NETWORK LAYER ENDPOINT RESOLVERS
# ==============================================================================
# These functions wrap the external APIs (Bungie, Google Sheets) with
# consistent retry logic, timeout handling, and HTML-guard checks.

def fetch_bungie_manifest_version(api_key=None, max_tries=3, delay=5):
    """
    Query Bungie's Destiny 2 Manifest endpoint for the current version string.

    The version string changes with every game update (weekly resets, hotfixes,
    seasonal patches). Comparing it to the cached version tells us whether the
    manifest tables need re-downloading.

    Args:
        api_key: optional Bungie API key (increases rate-limit headroom).
        max_tries: retry count for transient failures.
        delay: seconds between retries.

    Returns:
        The version string (e.g., "226960.24.03.15.2000-3") or None on failure.
    """
    url = "https://www.bungie.net/Platform/Destiny2/Manifest/"
    headers = {"X-API-Key": api_key} if api_key else {}

    for attempt in range(1, max_tries + 1):
        try:
            detail_logger.info(f"Requesting live Bungie Manifest version (Attempt {attempt}/{max_tries})...")
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Bungie wraps all responses in an envelope. ErrorCode 1 = success.
            if data.get("ErrorCode") != 1:
                detail_logger.warning(f"⚠️ Bungie API internal fault wrapper code reported error: {data.get('Status')}")
                return None

            return data.get("Response", {}).get("version")

        except requests.RequestException as e:
            # Network-level failure (DNS, timeout, HTTP 5xx). Retry with backoff.
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} hitting Bungie API: {e}")
            if attempt < max_tries:
                time.sleep(delay)

    detail_logger.error("❌ Critical network failure: Exhausted all retries attempting to query Bungie Manifest endpoint.")
    return None

def fetch_csv_content_by_gid(spreadsheet_id, gid, file_path, max_tries=3, delay=5):
    """
    Download a single Google Sheets tab as CSV using its GID (numeric tab ID).

    URL format:
        https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>

    HTML guard:
        If Google returns an HTML page (login redirect, rate-limit, or private
        sheet denial) instead of CSV, we detect it by checking for "<!doctype"
        or "<html" in the first 200 bytes and treat it as a hard failure.

    Date sidecar:
        If the response headers contain a downloadable filename with a date,
        we write that date to <file_path>.date for downstream scrapers.

    Args:
        spreadsheet_id: the Google Sheets document ID.
        gid: the numeric tab ID (visible in the URL when viewing the sheet).
        file_path: local path where the CSV would be saved (used only to derive
                   the sidecar .date filename).

    Returns:
        Raw bytes of the CSV content, or None on failure.
    """
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=12)

            if response.status_code == 200:
                # Guard against empty or near-empty responses (Google sometimes
                # returns 200 with a 1-byte body on rate-limit edge cases).
                if not response.content or len(response.content) < 10:
                    detail_logger.warning(f"⚠️ Empty or near-empty CSV response for GID {gid}. Treating as failure.")
                    return None

                # HTML guard: detect Google login / rate-limit pages.
                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ GID {gid} returned HTML instead of CSV. Sheet may be private or rate-limited.")
                    return None

                # Extract and cache the modification date from headers.
                online_date = extract_date_from_response(response)
                if online_date:
                    date_path = os.path.splitext(file_path)[0] + ".date"
                    with open(date_path, "w", encoding="utf-8") as df:
                        df.write(online_date)

                return response.content

            elif response.status_code == 404:
                # Hard 404: the GID does not exist or the spreadsheet is not
                # publicly visible. No point retrying.
                detail_logger.error(f"❌ HTTP 404 Error: Resource ID/GID sequence not found online [ID: {spreadsheet_id}, GID: {gid}]")
                return None

            else:
                # Unexpected status (429, 503, etc.). Retry if attempts remain.
                detail_logger.warning(f"⚠️ Unexpected HTTP status response code {response.status_code} received on attempt {attempt}/{max_tries}")

        except requests.RequestException as e:
            detail_logger.warning(f"⚠️ Connection exception encountered on attempt {attempt}/{max_tries} pulling GID {gid}: {e}")
            if attempt < max_tries:
                time.sleep(delay)

    return None

def fetch_csv_content_by_name(spreadsheet_id, sheet_name, max_tries=3, delay=5):
    """
    Fallback downloader that fetches a Google Sheets tab by its display name.

    URL format (Google Visualization API):
        https://docs.google.com/spreadsheets/d/<id>/gviz/tq?tqx=out:csv&sheet=<name>

    This is used when the GID-based export fails (e.g., the sheet owner
    restructured tabs and the GID in config.yaml is stale). The Visualization
    API is more forgiving of tab renames but slightly less reliable for large
    sheets, which is why it is the fallback, not the primary.

    Args:
        spreadsheet_id: the Google Sheets document ID.
        sheet_name: the human-readable tab name.

    Returns:
        Raw bytes of the CSV content, or None on failure.
    """
    encoded_name = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={encoded_name}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=12)

            if response.status_code == 200:
                # Same empty-response and HTML guards as the GID path.
                if not response.content or len(response.content) < 10:
                    detail_logger.warning(f"⚠️ Empty or near-empty CSV response for sheet '{sheet_name}'. Treating as failure.")
                    return None

                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ Sheet '{sheet_name}' returned HTML instead of CSV. Sheet may be private or rate-limited.")
                    return None

                # The Visualization API sometimes returns a JSONP-style error
                # payload even with HTTP 200. Detect and reject it.
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
    """
    Download a Google Sheets tab as an XLSX binary blob.

    URL format:
        https://docs.google.com/spreadsheets/d/<id>/export?format=xlsx&gid=<gid>

    XLSX mode is used for complex multi-tab workbooks where CSV export would
    lose formatting, multiple sheets, or formula results that openpyxl needs.

    The timeout is generous (20s) because XLSX files are larger than CSV.

    Args:
        spreadsheet_id: the Google Sheets document ID.
        gid: the numeric tab ID.
        file_path: local path where the XLSX would be saved (used for the
                   sidecar .date filename).

    Returns:
        Raw bytes of the XLSX content, or None on failure.
    """
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx&gid={gid}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(1, max_tries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=20)

            if response.status_code == 200:
                # XLSX files are never smaller than ~100 bytes (zip header).
                if not response.content or len(response.content) < 100:
                    detail_logger.warning(f"⚠️ Empty or near-empty XLSX response for GID {gid}. Treating as failure.")
                    return None

                content_start = response.content[:200].lower()
                if b"<!doctype" in content_start or b"<html" in content_start:
                    detail_logger.error(f"❌ GID {gid} returned HTML instead of XLSX. Sheet may be private or rate-limited.")
                    return None

                # Cache the modification date from headers.
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
    """
    Download and checksum a single workbook. Designed to run inside a
    ThreadPoolExecutor worker.

    Flow:
        1. Resolve the target filename from the spreadsheet key and workbook name.
        2. Attempt primary download via GID.
        3. If GID fails, attempt fallback download via sheet name.
        4. Calculate SHA-256 of the downloaded bytes.
        5. Compare to the cached hash in the state file.
        6. If changed or missing, write the file to disk and set the
           workbook_scrape_update_required flag.
        7. If use_xlsx is enabled, also fetch the XLSX binary and write it.

    Args:
        wb: the workbook dict from config.yaml (contains name, gid, use_xlsx).
        ss_id: the Google Sheets document ID.
        ss_key: the logical spreadsheet key (e.g., "aegis_endgame").
        ss_state: the mutable state sub-dict for this spreadsheet.
        download_dir: the folder where downloaded files are stored.

    Returns:
        tuple (config_name, updated_entry_dict, changed_bool).
        updated_entry_dict may be None if both download attempts failed.
    """
    config_gid = str(wb.get("gid"))
    config_name = wb.get("name")
    use_xlsx = wb.get("use_xlsx", False)
    fallback_used = False

    # Derive the local filename: <spreadsheet_key>_<workbook_name>.<<ext>
    clean_ss_name = sanitize_filename_part(ss_key)
    clean_wb_name = sanitize_filename_part(config_name)
    file_extension = "xlsx" if use_xlsx else "csv"
    target_filename = f"{clean_ss_name}_{clean_wb_name}.{file_extension}"
    file_path = os.path.join(download_dir, target_filename)

    # Track whether the local file is absent (first run or manual deletion).
    file_missing = not os.path.exists(file_path)

    # ------------------------------------------------------------------
    # Primary download attempt: by GID
    # ------------------------------------------------------------------
    csv_content = fetch_csv_content_by_gid(ss_id, config_gid, file_path)

    # ------------------------------------------------------------------
    # Fallback download attempt: by name
    # ------------------------------------------------------------------
    if not csv_content:
        detail_logger.warning(f"❌ Problem resolving GID '{config_gid}' for '{config_name}'. Trying fallback name lookup routing pass...")
        csv_content = fetch_csv_content_by_name(ss_id, config_name)
        if csv_content:
            fallback_used = True
        else:
            detail_logger.error(f"❌ CRITICAL CONFIG SPECIFICATION ERROR: Both GID and Name lookup sweeps completely failed for tab target '{config_name}'. Skipping parsing block.")
            return config_name, None, False

    # ------------------------------------------------------------------
    # Checksum comparison
    # ------------------------------------------------------------------
    current_hash = calculate_sha256(csv_content)
    wb_state = ss_state["workbooks"].get(config_name, {})
    old_hash = wb_state.get("local_saved_hash")

    # Build the updated state entry. We always refresh last_check and hash.
    updated_wb_entry = {
        "local_saved_hash": current_hash,
        "last_check": get_current_timestamp()
    }

    route_label = "Name Fallback" if fallback_used else f"GID {config_gid}"
    changed = False

    if old_hash != current_hash or file_missing:
        # Content changed OR local file is missing. Either way, downstream
        # scrapers need to re-process this workbook.
        if old_hash != current_hash:
            detail_logger.info(f"🚨 UPDATE DETECTED [{route_label}]: Content mutated for tracking tab row object '{config_name}' inside workbook profile '{ss_key}'.")
            updated_wb_entry["workbook_scrape_update_required"] = True
            changed = True
        else:
            detail_logger.info(f"📁 LOCAL RECONSTRUCTION [{route_label}]: File asset not found locally for '{config_name}'. Forcing pipeline execution state flags to trigger downstream re-ingestion.")
            updated_wb_entry["workbook_scrape_update_required"] = True
            changed = True

        # ------------------------------------------------------------------
        # Write the downloaded content to disk
        # ------------------------------------------------------------------
        if use_xlsx:
            # For XLSX mode, the CSV download was just for the checksum. We now
            # fetch the actual Excel binary and write that to disk.
            detail_logger.info(f"🔄 Component designated as Excel layout. Triggering dual format binary fetch for XLSX execution block...")
            xlsx_content = fetch_xlsx_workbook(ss_id, config_gid, file_path)
            if xlsx_content:
                with open(file_path, "wb") as f_out:
                    f_out.write(xlsx_content)
                detail_logger.info(f"💾 Excel sheet payload written safely down to disk layout target space: {file_path}")
            else:
                # XLSX fetch failed even though CSV succeeded. This is rare but
                # can happen if Google's XLSX exporter is temporarily down.
                # We return None so the state is not updated with a stale hash.
                detail_logger.error(f"❌ Error fetching XLSX structural byte array stream for '{config_name}'. File asset tracking remains un-synchronized.")
                return config_name, None, False
        else:
            # Standard CSV mode: write the bytes we already downloaded.
            with open(file_path, "wb") as f_out:
                f_out.write(csv_content)
            detail_logger.info(f"💾 Delimited flat data text structure stream written safely down to disk layout target space: {file_path}")

    else:
        # Hash matches and file exists. No work needed for this workbook.
        detail_logger.info(f"🟩 UP TO DATE [{route_label}]: Specifications data lines for sheet component '{config_name}' match cache records perfectly.")
        # Preserve any existing scrape flag (should be False, but we do not
        # overwrite it blindly in case another process set it).
        updated_wb_entry["workbook_scrape_update_required"] = wb_state.get("workbook_scrape_update_required", False)

    return config_name, updated_wb_entry, changed

# ==============================================================================
# SECTION 6: MAIN SYNCHRONIZATION RUNTIME CORE
# ==============================================================================
def process_checks():
    """
    Orchestrate the entire version-check and download cycle.

    Algorithm:
        1. Load config.yaml and the shared state file.
        2. Query Bungie for the current manifest version. If changed, set the
           manifest download, compile, and wishlist rebuild flags.
        3. For each configured spreadsheet:
            a. Ensure the state file has a sub-dict for this spreadsheet.
            b. Download every workbook in parallel (max 4 workers).
            c. If any workbook changed, set the spreadsheet's wishlist_update_required flag.
        4. Save the updated state file.

    Parallelism rationale:
        Network I/O to Google Sheets is the bottleneck. CPU usage is negligible.
        Using 4 concurrent workers keeps total runtime under ~30 seconds even
        for spreadsheets with 20+ tabs, without hitting Google's rate limit.
    """
    logger.info("=" * 50)
    logger.info("🚀 Starting version check and synchronization loop...")
    logger.info("=" * 50)

    # Guard: config.yaml is mandatory. Without it we do not know which sheets
    # to poll or which Bungie API key to use.
    if not os.path.exists(CONFIG_FILE):
        logger.critical(f"Aborting execution layout routine: Missing core config file at path '{CONFIG_FILE}'")
        return

    # Ensure the download directory exists before workers try to write into it.
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Load existing state or seed a fresh one. The lambda provides the default
    # structure so downstream code never has to check for missing top-level keys.
    state = load_json_file(STATE_FILE, lambda: {"spreadsheets": {}, "bungie_manifest": {}})

    if "spreadsheets" not in state:
        state["spreadsheets"] = {}
    if "bungie_manifest" not in state:
        state["bungie_manifest"] = {}

    # ------------------------------------------------------------------
    # Bungie Manifest version check
    # ------------------------------------------------------------------
    section_logger.info("Checking live Destiny 2 Manifest version from Bungie...")
    api_key = config.get("bungie_api", {}).get("api_key") if "bungie_api" in config else None
    live_version = fetch_bungie_manifest_version(api_key)

    if live_version:
        manifest_state = state["bungie_manifest"]
        old_version = manifest_state.get("local_saved_version")

        # Ensure the wishlist flag key exists (first-run safety).
        if "wishlist_update_required" not in manifest_state:
            manifest_state["wishlist_update_required"] = False

        # Always update the cached version and timestamp so we know when we
        # last successfully contacted Bungie.
        manifest_state["local_saved_version"] = live_version
        manifest_state["last_check"] = get_current_timestamp()

        if old_version != live_version:
            detail_logger.info(f"🚨 UPDATE DETECTED: Bungie Manifest changed from '{old_version}' to '{live_version}'")
            # Raise all three flags: download raw tables, compile them, and
            # rebuild every wishlist because perk hashes may have shifted.
            manifest_state["bungie_manifest_download_required"] = True
            manifest_state["bungie_manifest_compile_required"] = True
            manifest_state["wishlist_update_required"] = True
            detail_logger.info("🔄 Global wishlist rebuild flag set: all DIM wishlists will be regenerated after manifest compile.")
        else:
            detail_logger.info("🟩 Up to date: Bungie Manifest version matches local cache. No structural compilation changes needed.")
    else:
        # Bungie API unreachable. We do NOT clear any existing flags; we simply
        # leave them as-is so the pipeline retries on the next cycle.
        detail_logger.warning("⚠️ Could not verify live manifest version string. Retaining old registry parameters to preserve local safety limits.")

    # ------------------------------------------------------------------
    # Google Sheets workbook checks
    # ------------------------------------------------------------------
    spreadsheets_config = config.get("spreadsheets", {})

    for ss_key, ss_data in spreadsheets_config.items():
        ss_id = ss_data.get("id")
        workbooks = ss_data.get("workbooks", [])

        section_logger.info(f"Processing spreadsheet engine resource target profile: '{ss_key}'")

        # Auto-create the spreadsheet entry in state if this is the first run.
        if ss_key not in state["spreadsheets"]:
            state["spreadsheets"][ss_key] = {"wishlist_update_required": False, "workbooks": {}}

        ss_state = state["spreadsheets"][ss_key]

        # Ensure the wishlist flag key exists (first-run safety).
        if "wishlist_update_required" not in ss_state:
            ss_state["wishlist_update_required"] = False

        # ------------------------------------------------------------------
        # Parallel workbook processing
        # ------------------------------------------------------------------
        # max_workers=4 is a conservative balance between speed and Google
        # Sheets rate-limit tolerance. In practice, 4 concurrent CSV exports
        # from the same IP never trigger 429 errors.
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Submit all workbooks and map futures back to their configs.
            future_to_wb = {
                executor.submit(process_single_workbook, wb, ss_id, ss_key, ss_state, DOWNLOAD_DIR): wb
                for wb in workbooks
            }

            # as_completed() yields results in the order they finish, not the
            # order they were submitted. This keeps the log output responsive.
            for future in as_completed(future_to_wb):
                config_name, updated_entry, changed = future.result()

                if updated_entry is not None:
                    # Commit the updated state entry for this workbook.
                    ss_state["workbooks"][config_name] = updated_entry

                    # If any workbook in this spreadsheet changed, the entire
                    # spreadsheet's wishlist is stale (we cannot easily map
                    # which weapon came from which tab without re-scraping all).
                    if changed:
                        ss_state["wishlist_update_required"] = True

    # ------------------------------------------------------------------
    # Final state commit
    # ------------------------------------------------------------------
    section_logger.info("=" * 50)
    section_logger.info("Synchronization check complete. Pipeline gating flags successfully set.")

    # Atomic(ish) write via pipeline_utils helper.
    save_json_file(STATE_FILE, state)

if __name__ == "__main__":
    setup_root_console_logging()
    process_checks()