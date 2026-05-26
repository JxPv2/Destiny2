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
        "wishlist_dir": "dim_wishlists"
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
                "wishlist_dir": paths_block.get("wishlist_dir", defaults["wishlist_dir"])
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