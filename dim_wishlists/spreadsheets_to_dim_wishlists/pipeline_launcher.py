import os
import sys
import subprocess
import logging
from datetime import datetime
from pipeline_utils import setup_root_console_logging, SmartIndentFormatter, is_dry_run

setup_root_console_logging()

# ==============================================================================
# SECTION 1: BOOTSTRAP PATHS AND LOGGING
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
formatter = SmartIndentFormatter(fmt=LOG_LAYOUT)

logger = logging.getLogger("PipelineLauncher")
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ==============================================================================
# SECTION 2: CONFIGURATION
# ==============================================================================
STAGE_TIMEOUT_SECONDS = 600  # 10 minutes per stage

# ==============================================================================
# SECTION 3: STAGE DEFINITIONS
# ==============================================================================
PIPELINE_STAGES = [
    ("Version State Check", [sys.executable, "version_state_checker.py"]),
    ("Bungie Manifest Download", [sys.executable, "bungie_manifest_downloader.py"]),
    ("Boss Damage Scraper", [sys.executable, "aegis_boss-damage_spreadsheet_data_scraper.py"]),
    ("Speedrunner Scraper", [sys.executable, "aegis_speedrunner_spreadsheet_data_scraper.py"]),
    ("Endgame Scraper", [sys.executable, "aegis_endgame_spreadsheet_data_scraper.py"]),
    ("DIM Wishlist Converter", [sys.executable, "dim_wishlists_converter.py"]),
]

# ==============================================================================
# SECTION 4: ORCHESTRATION ENGINE
# ==============================================================================
def run_stage_streaming(stage_name, command):
    """Runs a stage and streams its stdout/stderr to the launcher shell in real-time."""
    logger.info(f"▶️  Starting stage: {stage_name}")

    # Force UTF-8 for child Python processes so emojis render correctly
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Pass --dry-run to child if launcher received it
    if is_dry_run():
        command = command + ["--dry-run"]
        logger.info(f"🧪 DRY-RUN mode active for stage: {stage_name}")

    process = subprocess.Popen(
        command,
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        env=env,
    )

    try:
        # Stream every line from the child to the launcher shell immediately
        for line in process.stdout:
            print(line, end="")
            sys.stdout.flush()

        process.wait(timeout=STAGE_TIMEOUT_SECONDS)

    except subprocess.TimeoutExpired:
        logger.error(f"⏱️ Stage '{stage_name}' timed out after {STAGE_TIMEOUT_SECONDS}s. Killing process...")
        process.kill()
        process.wait()
        raise subprocess.CalledProcessError(-1, command)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)

    logger.info(f"✅ Stage completed successfully: {stage_name}")


def execute_pipeline():
    """Runs all pipeline stages sequentially with isolated failure tracking."""
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
            logger.error(f"❌ Stage failed with exit code {e.returncode}: {stage_name}")
            failed_stages.append(stage_name)
        except FileNotFoundError as e:
            logger.critical(f"❌ Stage executable or script not found: {stage_name} -> {e}")
            failed_stages.append(stage_name)
        except Exception as e:
            logger.critical(f"❌ Stage crashed unexpectedly: {stage_name} -> {e}")
            failed_stages.append(stage_name)

    # ==============================================================================
    # SECTION 5: FINAL REPORT
    # ==============================================================================
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