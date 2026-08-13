# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
import os
import sys
import subprocess
import logging
import logging.handlers
from datetime import datetime
from pipeline_utils import setup_root_console_logging, SmartIndentFormatter, is_dry_run

# Initialize root console logging immediately so that any errors during module
# load (e.g., missing imports) are visible when running manually.
setup_root_console_logging()

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS AND LOGGING
# ==============================================================================
# SCRIPT_DIR anchors all relative paths to the directory containing this launcher,
# regardless of the user's current working directory. This prevents "file not
# found" errors when the pipeline is invoked from cron, GitHub Actions, or
# a different shell cwd.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

# Ensure the log directory exists before attaching the FileHandler.
os.makedirs(LOG_DIR, exist_ok=True)

LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
formatter = SmartIndentFormatter(fmt=LOG_LAYOUT)

# Module-level logger for the launcher. It captures high-level stage
# transitions and the final execution summary. Individual stage detail is
# written to their own log files (e.g., bungie_manifest_downloader.log).
logger = logging.getLogger("PipelineLauncher")
logger.setLevel(logging.INFO)

# Defensive reset: survive reloads in long-running scheduler processes.
if logger.hasHandlers():
    logger.handlers.clear()

# Derive log filename from script name: pipeline_launcher.log.
log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(LOG_DIR, log_name),
    encoding="utf-8",
    when="D",
    interval=7,
    backupCount=4,
    utc=True
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ==============================================================================
# SECTION 2: CONFIGURATION
# ==============================================================================
# Per-stage timeout. If a script hangs (e.g., Bungie API outage, infinite loop
# in a scraper), the launcher kills it after 10 minutes and marks the stage
# failed. This prevents the 8-hour GitHub Actions job from hanging indefinitely.
STAGE_TIMEOUT_SECONDS = 600  # 10 minutes per stage

# ==============================================================================
# SECTION 3: STAGE DEFINITIONS
# ==============================================================================
# Ordered list of pipeline stages. Each tuple is (display_name, argv).
# The launcher runs these sequentially; a failure in one stage does not
# abort subsequent stages (they still run), but the final exit code will be
# non-zero so the scheduler (GitHub Actions / cron) knows to alert the operator.
#
# Stage order rationale:
#   1. Version State Check     -> Detects which Google Sheets / Bungie manifest
#                                 versions changed since the last run.
#   2. Bungie Manifest Download-> Fetches new Destiny 2 definitions if needed.
#   3. Boss Damage Scraper     -> Scrapes the Aegis boss-damage sheet.
#   4. Speedrunner Scraper     -> Scrapes the Aegis speedrunner sheet.
#   5. Endgame Scraper         -> Scrapes the Aegis endgame mega-sheet.
#   6. DIM Wishlist Converter  -> Generates .txt wishlists from all scraped
#                                 JSON artifacts. Sets wishlist_split_required
#                                 flags in the shared state for sources that
#                                 were actually updated.
#   7. DIM Wishlist Splitter   -> Reads the state flags set by the converter
#                                 and only re-splits sources that changed.
#                                 Clears flags after processing.
PIPELINE_STAGES = [
    ("Version State Check", [sys.executable, "version_state_checker.py"]),
    ("Bungie Manifest Download", [sys.executable, "bungie_manifest_downloader.py"]),
    ("Boss Damage Scraper", [sys.executable, "aegis_boss-damage_spreadsheet_data_scraper.py"]),
    ("Speedrunner Scraper", [sys.executable, "aegis_speedrunner_spreadsheet_data_scraper.py"]),
    ("Endgame Scraper", [sys.executable, "aegis_endgame_spreadsheet_data_scraper.py"]),
    ("DIM Wishlist Converter", [sys.executable, "dim_wishlists_converter.py"]),
    ("DIM Wishlist Splitter", [sys.executable, "dim_wishlists_splitter.py", "--pipeline"]),
]

# ==============================================================================
# SECTION 4: ORCHESTRATION ENGINE
# ==============================================================================
def run_stage_streaming(stage_name, command):
    """
    Execute a single pipeline stage as a subprocess and stream its output.

    Why streaming instead of capture-then-print:
        When running in GitHub Actions, the job log is live. If we captured all
        stdout and only printed at the end, a 10-minute manifest download would
        show no output, causing the runner to assume the job is hung and kill it.
        Streaming ensures the runner sees periodic output and keeps the job alive.

    UTF-8 enforcement:
        Windows and some Linux containers default to cp1252 or ASCII for
        subprocess pipes. We force PYTHONIOENCODING=utf-8 so emojis in child
        logs render correctly and do not become mojibake.

    Dry-run propagation:
        If the launcher was invoked with --dry-run, we append the flag to every
        child script so they all write to the dry_run/ sandbox folder instead of
        production paths.

    Args:
        stage_name: human-readable name for logging.
        command:    list of strings passed to subprocess.Popen (e.g., ["python", "foo.py"]).

    Raises:
        subprocess.CalledProcessError: if the child exits non-zero or times out.
    """
    logger.info(f"▶️  Starting stage: {stage_name}")

    # Force UTF-8 for child Python processes so emojis render correctly.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Pass --dry-run to child if launcher received it.
    if is_dry_run():
        command = command + ["--dry-run"]
        logger.info(f"🧪 DRY-RUN mode active for stage: {stage_name}")

    # Popen with stdout=PIPE and stderr=STDOUT merges both streams so we only
    # have to poll one pipe. text=True + encoding='utf-8' gives us str objects
    # instead of bytes. bufsize=1 enables line-buffering for real-time prints.
    process = subprocess.Popen(
        command,
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',  # Replace undecodable bytes rather than crashing.
        bufsize=1,
        env=env,
    )

    try:
        # Stream every line from the child to the launcher shell immediately.
        # The 'for line in process.stdout' iterator yields whenever the child
        # flushes a line (or when the pipe buffer fills).
        for line in process.stdout:
            print(line, end="")
            sys.stdout.flush()

        # Wait for the child to finish, but impose the timeout. If the child
        # exits before the timeout, process.wait() returns immediately.
        process.wait(timeout=STAGE_TIMEOUT_SECONDS)

    except subprocess.TimeoutExpired:
        # The child exceeded STAGE_TIMEOUT_SECONDS. Kill it and translate the
        # timeout into a CalledProcessError so the caller treats it as a failure.
        logger.error(f"⏱️ Stage '{stage_name}' timed out after {STAGE_TIMEOUT_SECONDS}s. Killing process...")
        process.kill()
        process.wait()
        raise subprocess.CalledProcessError(-1, command)

    # After wait(), returncode is populated. Non-zero means the stage failed.
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)

    logger.info(f"✅ Stage completed successfully: {stage_name}")


def execute_pipeline():
    """
    Run all pipeline stages sequentially with isolated failure tracking.

    Design philosophy:
        - Each stage runs independently. A scraper failure does not prevent the
          wishlist converter from running on previously-scraped data.
        - The final summary lists both completed and failed stages so the
          operator can see at a glance what needs attention.
        - Exit code 0 = everything succeeded. Exit code 1 = one or more stages
          failed. This lets GitHub Actions mark the workflow run as failed.

    Dry-run mode:
        When active, all stages write to a sandbox folder (dry_run/) instead of
        overwriting production wishlists. The launcher logs this prominently.
    """
    if is_dry_run():
        logger.info("=" * 80)
        logger.info("🧪 DRY-RUN MODE: All outputs written to dry_run/ folder")
        logger.info("=" * 80)

    logger.info("=" * 80)
    logger.info("🚀 Launching Full Pipeline Execution...")
    logger.info("=" * 80)

    failed_stages = []
    completed_stages = []

    for stage_name, command in PIPELINE_STAGES:
        try:
            run_stage_streaming(stage_name, command)
            completed_stages.append(stage_name)
        except subprocess.CalledProcessError as e:
            # Child exited non-zero (or was killed after timeout).
            logger.error(f"❌ Stage failed with exit code {e.returncode}: {stage_name}")
            failed_stages.append(stage_name)
        except FileNotFoundError as e:
            # The script file itself is missing (e.g., not pushed to the runner).
            logger.critical(f"❌ Stage executable or script not found: {stage_name} -> {e}")
            failed_stages.append(stage_name)
        except Exception as e:
            # Catch-all for unexpected launcher-level bugs (permission errors,
            # OOM killer, etc.). We log and continue to the next stage.
            logger.critical(f"❌ Stage crashed unexpectedly: {stage_name} -> {e}")
            failed_stages.append(stage_name)

    # ==============================================================================
    # SECTION 5: FINAL REPORT
    # ==============================================================================
    # Emit a concise summary block. In GitHub Actions this appears at the bottom
    # of the job log so the operator does not have to scroll through thousands
    # of lines to find the failure.
    logger.info("=" * 80)
    logger.info("📊 Pipeline Execution Summary")
    logger.info("=" * 80)
    logger.info(f"  Completed ({len(completed_stages)}): {', '.join(completed_stages) if completed_stages else 'None'}")

    if failed_stages:
        logger.error(f"  Failed ({len(failed_stages)}): {', '.join(failed_stages)}")
        logger.info("=" * 80)
        logger.info("🛑 Pipeline finished with ERRORS. Check individual logs above.")
        sys.exit(1)
    else:
        logger.info("  Failed: None")
        logger.info("=" * 80)
        if is_dry_run():
            logger.info("🧪 DRY-RUN complete. Inspect outputs in dry_run/ folder.")
        else:
            logger.info("🎉 All pipeline stages completed successfully.")
        sys.exit(0)

if __name__ == "__main__":
    execute_pipeline()
