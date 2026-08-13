# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# ==============================================================================
# pipeline_utils.py — SHARED UTILITIES FOR THE ENTIRE PIPELINE
# ==============================================================================
# This module is imported by EVERY script in the pipeline.
# It provides common functionality that would otherwise be duplicated:
#   - Path resolution (where to read/write files)
#   - Logging setup (formatters, console output, UTF-8 handling)
#   - Safe file I/O (atomic JSON writes to prevent corruption)
#   - Timestamp generation (ISO-8601 UTC)
#   - Dry-run mode (--dry-run flag for testing without touching production data)
#
# DESIGN PHILOSOPHY:
#   All paths are resolved relative to the current working directory (CWD).
#   This means the pipeline must be run from the repo root where config.yaml lives.
#   GitHub Actions handles this by setting working-directory appropriately.
# ==============================================================================

import os
import json
import logging
import sys
import io
import tempfile
import yaml
import argparse
import logging.handlers
from datetime import datetime, timezone


# ==============================================================================
# DRY-RUN MODE
# ==============================================================================
# --dry-run is a global CLI flag. When active, ALL scripts write outputs to
# "dry_run/" subfolders instead of production paths. This lets you test the
# entire pipeline end-to-end without modifying real data or wishlists.
#
# USAGE: python any_script.py --dry-run
#
# IMPLEMENTATION NOTE:
#   We use parse_known_args() instead of parse_args() so that individual scripts
#   can define their own additional CLI arguments without conflict.
# ==============================================================================

def is_dry_run():
    """Check if --dry-run was passed on the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write outputs to dry_run/ folder instead of production paths"
    )
    # parse_known_args() ignores args the caller already parsed
    args, _ = parser.parse_known_args()
    return args.dry_run


# ==============================================================================
# CONSOLE LOGGING (ROOT LOGGER)
# ==============================================================================
# The root logger gets a StreamHandler so that ALL log messages from every
# script also appear in the terminal. Each script adds its own FileHandler
# on top of this for per-script log files.
#
# UTF-8 FIX:
#   Windows defaults to cp1252 for stdout, which breaks emojis.
#   We force UTF-8 so logs look correct in GitHub Actions, VS Code terminals,
#   and when piped to files.
#
# DUPLICATE PREVENTION:
#   Checks if a StreamHandler already exists before adding another.
#   Safe to call multiple times.
# ==============================================================================

def setup_root_console_logging():
    """
    Adds a StreamHandler to the root logger so all propagated logs hit the shell.
    """
    # Force UTF-8 on stdout so emojis render correctly in piped/redirected output
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    elif hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    root = logging.getLogger()
    # Avoid adding duplicate handlers if this gets called multiple times
    has_stream = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    if not has_stream:
        fmt = "%(asctime)s [%(levelname)s] -> %(message)s"
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(fmt))
        root.addHandler(sh)
        root.setLevel(logging.INFO)


# ==============================================================================
# SECTION 1: CONFIGURATION LOADING
# ==============================================================================
# CONFIG_FILE is the single source of truth for the entire pipeline.
# It lives in the repo root and defines:
#   - Folder paths (where to store downloads, logs, outputs)
#   - Bungie API settings
#   - Which spreadsheets to monitor
#   - Which perk categories to filter out
#
# bootstrap_system_paths() reads the "pipeline_paths" block from config.yaml
# and returns a dict of folder paths. If config.yaml is missing or broken,
# it falls back to hardcoded defaults so the pipeline doesn't crash.
#
# DRY-RUN INTEGRATION:
#   When --dry-run is active, all paths get a "dry_run/" prefix.
#   e.g., "logs" becomes "dry_run/logs", "dim_wishlists" becomes "dry_run/dim_wishlists"
# ==============================================================================

CONFIG_FILE = "config.yaml"  # Expected in the same folder as the running script

def bootstrap_system_paths(config_path=CONFIG_FILE):
    """
    Reads the yaml configuration file early to map pipeline folder parameters.
    Provides robust defaults if the file cannot be accessed or loaded.
    Prefixes all paths with 'dry_run/' when --dry-run flag is active.
    """
    # Hardcoded defaults — used when config.yaml is missing or unreadable
    defaults = {
        "state_file": "local_version_state.json",
        "log_dir": "logs",
        "manifest_dir": "bungie_manifest_data",
        "download_dir": "workbooks_downloaded",
        "scraped_dir": "spreadsheets_scraped_data",
        "wishlist_dir": "dim_wishlists",
        "splitted_wishlist_dir": "dim_wishlists/splitted_dim_wishlists",
    }

    dry_run = is_dry_run()
    prefix = "dry_run/" if dry_run else ""

    # If config.yaml doesn't exist, return defaults with optional dry_run prefix
    if not os.path.exists(config_path):
        return {k: prefix + v for k, v in defaults.items()}

    # Config exists — try to read it
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}
            paths_block = config_data.get("pipeline_paths", {})
            result = {
                "state_file": paths_block.get("state_file", defaults["state_file"]),
                "log_dir": paths_block.get("log_dir", defaults["log_dir"]),
                "manifest_dir": paths_block.get("manifest_dir", defaults["manifest_dir"]),
                "download_dir": paths_block.get("download_dir", defaults["download_dir"]),
                "scraped_dir": paths_block.get("scraped_dir", defaults["scraped_dir"]),
                "wishlist_dir": paths_block.get("wishlist_dir", defaults["wishlist_dir"]),
                "splitted_wishlist_dir": paths_block.get("splitted_wishlist_dir", defaults["splitted_wishlist_dir"])
            }
            # Apply dry_run prefix to all paths if active
            if dry_run:
                result = {k: prefix + v for k, v in result.items()}
            return result
    except Exception:
        # Any YAML parse error → fall back to defaults
        return {k: prefix + v for k, v in defaults.items()}


# ==============================================================================
# SECTION 2: LOGGING FORMATTERS
# ==============================================================================
# SmartIndentFormatter controls visual indentation in log files.
#
# PROBLEM IT SOLVES:
#   When a logger has both FileHandler and StreamHandler, the formatter gets
#   called TWICE for the same record (once per handler). If we increment a
#   counter every time format() is called, we double-count and the indentation
#   logic breaks.
#
# SOLUTION:
#   Mark each record object with a flag (_smart_indent_seen) so we only
#   increment the counter once, regardless of how many handlers process it.
#   Using hasattr/getattr is safer than id(record) because CPython can reuse
#   object memory addresses after garbage collection.
#
# BEHAVIOR:
#   - First 3 records per logger+thread: unindented (header block)
#   - Everything after: indented by 2 spaces
#   - LoggerAdapter can override with extra={"indent": N}
# ==============================================================================

class SmartIndentFormatter(logging.Formatter):
    """
    Unified formatter that:
      - Unindents the first 3 records of each logger's output (header block)
      - Indents everything else by 2 spaces
      - Supports explicit indent overrides via LoggerAdapter
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Maps "logger_name:thread_id" → call count
        self._call_counts = {}

    def formatTime(self, record, datefmt=None):
        """
        Custom timestamp: 2026-05-26 14:53:45,123
        (ISO date + time + comma + 3-digit milliseconds)
        """
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S,") + f"{int(record.msecs):03d}"

    def format(self, record):
        """
        Apply formatting rules:
        1. Check for explicit indent from LoggerAdapter
        2. Otherwise use call-count-based indentation
        """
        formatted_line = super().format(record)

        # Priority 1: explicit indent override from LoggerAdapter
        # Usage: adapter.info("msg", extra={"indent": 2})
        explicit_indent = getattr(record, "indent", None)
        if explicit_indent is not None:
            return "  " * explicit_indent + formatted_line

        # Priority 2: call-count based indentation
        logger_name = record.name
        key = f"{logger_name}:{record.thread}"

        # Only increment the counter the first time we see this record.
        # We mark the record object itself to avoid double-counting when
        # multiple handlers (file + stream) share one formatter instance.
        if not getattr(record, "_smart_indent_seen", False):
            record._smart_indent_seen = True
            count = self._call_counts.get(key, 0)
            self._call_counts[key] = count + 1

        count = self._call_counts.get(key, 0)
        # First 3 records per logger are unindented (header block)
        if count <= 3:
            return formatted_line
        # Everything else indented by 2 spaces
        return "  " + formatted_line


# Backward-compatible aliases — older scripts may reference these names
PipelineIndentedFormatter = SmartIndentFormatter
IndentedFormatter = SmartIndentFormatter


# ==============================================================================
# SECTION 3: ATOMIC FILE OPERATIONS
# ==============================================================================
# JSON files are critical pipeline state. A crash during write would corrupt them.
# 
# save_json_file() uses an atomic write pattern:
#   1. Write to a temp file in the SAME directory as the target
#   2. Use os.replace() to atomically swap temp → target
#
# WHY THIS MATTERS:
#   If the process crashes during step 1, the original file is untouched.
#   If the process crashes during step 2, os.replace() is atomic on modern
#   filesystems — the file is either the old version or the new version, never
#   a partial write.
#
# load_json_file() is the safe reader counterpart:
#   - Returns a default value if file is missing
#   - Returns a default value if file contains invalid JSON
#   - Never raises an exception (caller gets predictable fallback)
# ==============================================================================

def load_json_file(filepath, default_factory=None):
    """
    Safely reads structural data matrices from local disk.
    Returns a fallback template if the file is missing or broken.

    default_factory: callable that returns a default (e.g., lambda: {}).
                     Called fresh each time so you get a new object.
    """
    # File doesn't exist → return default or empty dict
    if not os.path.exists(filepath):
        if default_factory:
            return default_factory()
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # Corrupt JSON → return default or empty dict
        if default_factory:
            return default_factory()
        return {}


def save_json_file(filepath, data):
    """
    Saves JSON data atomically to prevent corruption from partial writes.

    Steps:
    1. Write to a temp file in the same directory as the target
    2. Use os.replace() to atomically swap the temp file into place

    If the process crashes during step 1, the original file is untouched.
    os.replace() is atomic on all modern filesystems (NTFS, ext4, APFS).
    """
    dir_name = os.path.dirname(filepath) or "."
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        suffix=".tmp",
        delete=False
    ) as f:
        json.dump(data, f, indent=2, sort_keys=False)
        temp_path = f.name
    os.replace(temp_path, filepath)


# ==============================================================================
# SECTION 4: TIMESTAMP HELPERS
# ==============================================================================
# All timestamps in the pipeline use strict ISO-8601 format with timezone info.
# This ensures consistency across local runs and GitHub Actions (which runs in UTC).
#
# Example output: 2026-05-26T14:53:45.123456+00:00
# ==============================================================================

def get_current_timestamp():
    """Generates UTC timestamps in strict ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()

# ==============================================================================
# SECTION 5: LOGGING HELPERS
# ==============================================================================
# These factories eliminate the boilerplate repeated in every pipeline script.

class IndentAdapter(logging.LoggerAdapter):
    """
    LoggerAdapter that injects an 'indent' key into the LogRecord extra dict.
    SmartIndentFormatter reads this key and prepends spaces so nested output
    (workbook -> weapon -> perk) is visually scannable.

    Usage:
        logger = logging.getLogger("MyScript")
        adapter = IndentAdapter(logger, 2)
        adapter.info("nested message")  # indented by 2 levels
    """
    def __init__(self, logger, indent_level):
        super().__init__(logger, {})
        self.indent_level = indent_level

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["indent"] = self.indent_level
        return msg, kwargs


class DuplicateInfoFilter(logging.Filter):
    """
    Duplicates specific INFO records to a secondary handler.
    Passes through WARNING+ records normally.

    Why this exists:
        The warnings log is a lean file that operators can tail to see only
        problems. However, warnings without surrounding context (e.g., which
        spreadsheet was being processed when the warning fired) are hard to
        debug. This filter copies select high-level INFO lines (banners, stage
        transitions) into the warnings handler so every warning is self-contained.
    """
    def __init__(self, target_handler, keywords=None):
        super().__init__()
        self.target_handler = target_handler
        self.keywords = keywords or []

    def filter(self, record):
        # Always pass through to the primary handler (return True).
        # Additionally, if this is an INFO record whose message contains one
        # of the contextual keywords, forward a copy to the warnings handler.
        if record.levelno == logging.INFO:
            msg_lower = record.getMessage().lower()
            if any(kw in msg_lower for kw in self.keywords):
                self.target_handler.handle(record)
        return True  # Never block the primary handler


def setup_module_logger(name, log_dir, layout=None, warnings_log=False, dupe_keywords=None):
    """
    Create a fully-configured module logger with FileHandler + optional warnings log.

    This replaces the ~15 lines of boilerplate repeated in every script:
        logger = logging.getLogger(...)
        logger.setLevel(...)
        if logger.hasHandlers(): clear
        formatter = SmartIndentFormatter(...)
        fh = FileHandler(...)
        ...

    Args:
        name (str): Logger name (usually __name__ or script filename without .py).
        log_dir (str): Directory where .log files are written.
        layout (str): Optional custom format string. Default preserves timestamps.
        warnings_log (bool): If True, also create a _warnings.log with a
            DuplicateInfoFilter that echoes select INFO lines for context.
        dupe_keywords (list[str]): Keywords for the DuplicateInfoFilter.
            Only used if warnings_log=True.

    Returns:
        logging.Logger: The configured logger, ready for use.
    """
    if layout is None:
        layout = "%(asctime)s [%(levelname)s] -> %(message)s"

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Defensive reset: survive reloads in long-running scheduler processes.
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = SmartIndentFormatter(fmt=layout)

    # Primary log file: everything (INFO and above).
    log_path = os.path.join(log_dir, f"{name}.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        encoding="utf-8",
        when="D",        # rotate by day
        interval=7,      # every 7 days
        backupCount=4,   # keep 4 weekly files
        utc=True         # use UTC so GitHub Actions and local runs agree
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Optional warnings-only secondary log.
    if warnings_log:
        warnings_path = os.path.join(log_dir, f"{name}_warnings.log")
        warnings_handler = logging.handlers.TimedRotatingFileHandler(
            warnings_path,
            encoding="utf-8",
            when="D",
            interval=7,
            backupCount=4,
            utc=True
        )
        warnings_handler.setFormatter(formatter)
        warnings_handler.setLevel(logging.WARNING)

        dup_filter = DuplicateInfoFilter(
            warnings_handler,
            keywords=dupe_keywords or ["launching", "processing", "=" * 10]
        )
        file_handler.addFilter(dup_filter)

    logger.addHandler(file_handler)
    return logger


# ==============================================================================
# SECTION 6: CONFIGURATION HELPERS
# ==============================================================================

def load_config(config_path=CONFIG_FILE):
    """
    Safely load config.yaml and return the parsed dict.

    Returns an empty dict (never raises) so callers can degrade gracefully
    with hardcoded defaults rather than crashing.

    Args:
        config_path (str): Path to the YAML config file.

    Returns:
        dict: Parsed YAML content, or {} on any failure (missing file,
              parse error, permission denied).
    """
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ==============================================================================
# SECTION 7: STATE FILE HELPERS
# ==============================================================================
# These ensure consistent dict structure and key ordering across all scripts
# that read/write local_version_state.json.

def get_spreadsheet_state_template():
    """
    Return a fresh spreadsheet state dict.

    This is a factory function rather than a module-level dict to avoid
    the mutable default dict anti-pattern. Every call returns a new dict
    with its own independent "workbooks" sub-dict.
    """
    return {
        "wishlist_update_required": False,
        "wishlist_split_required": False,
        "workbooks": {}
    }


MANIFEST_STATE_TEMPLATE = {
    "wishlist_update_required": False,
    "bungie_manifest_download_required": False,
    "bungie_manifest_compile_required": False,
    "local_saved_version": "",
    "last_check": ""
}


def ensure_spreadsheet_state(state, key):
    """
    Ensure state["spreadsheets"][key] exists with canonical key order.

    If the key is missing, creates it using the template:
        wishlist_update_required, wishlist_split_required, workbooks

    If the key exists but is missing any canonical keys, injects them while
    preserving existing values.

    Args:
        state (dict): The root state dict (mutated in place).
        key (str): The spreadsheet key (e.g., "aegis_boss-damage").

    Returns:
        dict: The spreadsheet state sub-dict for the given key.
    """
    if "spreadsheets" not in state:
        state["spreadsheets"] = {}

    if key not in state["spreadsheets"]:
        state["spreadsheets"][key] = get_spreadsheet_state_template()
        return state["spreadsheets"][key]

    existing = state["spreadsheets"][key]
    # Rebuild with canonical order, preserving existing values.
    merged = {}
    for k in get_spreadsheet_state_template():
        merged[k] = existing.get(k, get_spreadsheet_state_template()[k])
    # Preserve any non-canonical keys that may have been added by future scripts.
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v
    state["spreadsheets"][key] = merged
    return merged


def ensure_manifest_state(state):
    """
    Ensure state["bungie_manifest"] exists with canonical key order.

    Same pattern as ensure_spreadsheet_state but for the manifest section.

    Args:
        state (dict): The root state dict (mutated in place).

    Returns:
        dict: The manifest state sub-dict.
    """
    if "bungie_manifest" not in state:
        state["bungie_manifest"] = dict(MANIFEST_STATE_TEMPLATE)
        return state["bungie_manifest"]

    existing = state["bungie_manifest"]
    merged = {}
    for k in MANIFEST_STATE_TEMPLATE:
        merged[k] = existing.get(k, MANIFEST_STATE_TEMPLATE[k])
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v
    state["bungie_manifest"] = merged
    return merged
