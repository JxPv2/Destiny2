import os
import json
import logging
import sys
import io
import tempfile
import yaml
import argparse
from datetime import datetime, timezone

def is_dry_run():
    """Check if --dry-run was passed on the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Write outputs to dry_run/ folder instead of production paths")
    args, _ = parser.parse_known_args()
    return args.dry_run


def setup_root_console_logging():
    """Adds a StreamHandler to the root logger so all propagated logs hit the shell."""
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
CONFIG_FILE = "config.yaml"

def bootstrap_system_paths(config_path=CONFIG_FILE):
    """
    Reads the yaml configuration file early to map pipeline folder parameters.
    Provides robust defaults if the file cannot be accessed or loaded.
    Prefixes all paths with 'dry_run/' when --dry-run flag is active.
    """
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

    if not os.path.exists(config_path):
        return {k: prefix + v for k, v in defaults.items()}

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
            if dry_run:
                result = {k: prefix + v for k, v in result.items()}
            return result
    except Exception:
        return {k: prefix + v for k, v in defaults.items()}


# ==============================================================================
# SECTION 2: LOGGING FORMATTERS
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
        self._call_counts = {}

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S,") + f"{int(record.msecs):03d}"

    def format(self, record):
        formatted_line = super().format(record)
        # Check for explicit indent override from LoggerAdapter
        explicit_indent = getattr(record, "indent", None)
        if explicit_indent is not None:
            return "  " * explicit_indent + formatted_line

        logger_name = record.name
        key = f"{logger_name}:{record.thread}"

        # Only increment the counter the first time we see this record.
        # We mark the record object itself to avoid double-counting when
        # multiple handlers (file + stream) share one formatter instance.
        # Using hasattr is safer than id() because CPython can reuse object
        # memory addresses after garbage collection.
        if not getattr(record, "_smart_indent_seen", False):
            record._smart_indent_seen = True
            count = self._call_counts.get(key, 0)
            self._call_counts[key] = count + 1

        count = self._call_counts.get(key, 0)
        if count <= 3:
            return formatted_line
        return "  " + formatted_line


# Backward-compatible aliases
PipelineIndentedFormatter = SmartIndentFormatter
IndentedFormatter = SmartIndentFormatter


# ==============================================================================
# SECTION 3: ATOMIC FILE OPERATIONS
# ==============================================================================
def load_json_file(filepath, default_factory=None):
    """
    Safely reads structural data matrices from local disk.
    Returns a fallback template if the file is missing or broken.
    """
    if not os.path.exists(filepath):
        if default_factory:
            return default_factory()
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        if default_factory:
            return default_factory()
        return {}


def save_json_file(filepath, data):
    """
    Saves JSON data atomically to prevent corruption from partial writes.
    """
    dir_name = os.path.dirname(filepath) or "."
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dir_name, suffix=".tmp", delete=False) as f:
        json.dump(data, f, indent=2, sort_keys=False)
        temp_path = f.name
    os.replace(temp_path, filepath)


# ==============================================================================
# SECTION 4: TIMESTAMP HELPERS
# ==============================================================================
def get_current_timestamp():
    """ Generates UTC timestamps in strict ISO-8601 format. """
    return datetime.now(timezone.utc).isoformat()