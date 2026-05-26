import os
import re
import json
import yaml
import logging
from datetime import datetime

from pipeline_utils import (
    bootstrap_system_paths,
    PipelineIndentedFormatter,
    save_json_file,
    setup_root_console_logging,
    CONFIG_FILE,
)

# =============================================================================
# SECTION 1: BASE SCRAPER CLASS
# =============================================================================
class BaseSpreadsheetScraper:
    def __init__(self, spreadsheet_key):
        setup_root_console_logging()
        self.spreadsheet_key = spreadsheet_key

        self.config = self._load_pipeline_configuration()

        paths_config = self.config.get("pipeline_paths", {})

        self.source_dir = paths_config.get("download_dir", "workbooks_downloaded")
        self.logs_dir = paths_config.get("log_dir", "logs")
        self.state_file = paths_config.get("state_file", "local_version_state.json")
        self.output_dir = paths_config.get("scraped_dir", "workbooks_scraped_data")

        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)

        self._initialize_logging_system()

        self._report_configuration_status()

    def _initialize_logging_system(self):
        log_file_path = os.path.join(self.logs_dir, f"{self.spreadsheet_key}_scraper.log")

        self.logger = logging.getLogger(self.spreadsheet_key)
        self.logger.setLevel(logging.INFO)

        if self.logger.hasHandlers():
            self.logger.handlers.clear()

        LOG_LAYOUT = "%(asctime)s [%(levelname)s] [%(name)s] -> %(message)s"
        custom_formatter = PipelineIndentedFormatter(fmt=LOG_LAYOUT)

        file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
        file_handler.setFormatter(custom_formatter)
        self.logger.addHandler(file_handler)

        self.logger.info(f"================================================================================")
        self.logger.info(f"🚀 Initializing Data Extraction Pipeline Engine: [{self.spreadsheet_key}]")
        self.logger.info(f"================================================================================")

    def _load_pipeline_configuration(self):
        config_path = CONFIG_FILE
        self._config_error = None
        self._config_missing = False

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                self._config_error = e
                return {}

        self._config_missing = True
        return {}

    def _report_configuration_status(self):
        if self._config_error:
            self.logger.error(f"❌ Failed to parse config.yaml: {self._config_error}. Falling back to default paths.")
        elif self._config_missing:
            self.logger.warning(f"⚠️ Master configuration data asset missing at config.yaml. Falling back to default paths.")

    # =========================================================================
    # DATA SANITIZATION
    # =========================================================================
    def parse_name_and_version(self, raw_name):
        if not raw_name:
            return "", ""

        name_lines = [line.strip() for line in str(raw_name).split('\n') if line.strip()]
        if not name_lines:
            return "", ""

        clean_name = name_lines[0].replace("\ufeff", "").strip()
        version_string = ""

        if len(name_lines) > 1:
            version_string = re.sub(r'(?i)\s*version\s*', '', name_lines[1]).strip()

        return clean_name, version_string

    def sanitize_perk_cell(self, cell_value):
        if cell_value is None:
            return []

        lines = [line.strip() for line in str(cell_value).split('\n') if line.strip()]
        return [l for l in lines if l.upper() not in ["/", "N/A", "FIXED", "NONE", "-"]]

    def sanitize_info_cell(self, key, cell_value):
        if cell_value is None:
            return ""

        val = str(cell_value).strip()
        if str(key).lower() != "rank" and val.upper() in ["NONE", "N/A", "-"]:
            return ""

        lines = [line.strip() for line in val.split('\n') if line.strip()]
        return ", ".join(lines)

    # =========================================================================
    # STATE REGISTRY
    # =========================================================================
    def is_update_required(self, workbook_name):
        if not os.path.exists(self.get_workbook_file_path(workbook_name)):
            self.logger.warning(f"⚠️ Expected workbook storage asset not found: {workbook_name}")
            return False

        expected_output_path = getattr(self, 'output_filename', None)
        if not expected_output_path:
            clean_wb_name = str(workbook_name).lower().replace(" ", "_")
            expected_output_path = os.path.join(self.output_dir, f"{self.spreadsheet_key}_{clean_wb_name}.json")

        if expected_output_path and not os.path.exists(expected_output_path):
            self.logger.info(f"🔄 Output data asset missing at '{expected_output_path}'. Forcing scrape routine override.")
            return True

        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)

                sheet_entry = state.get("spreadsheets", {}).get(self.spreadsheet_key, {})
                workbook_entry = sheet_entry.get("workbooks", {}).get(workbook_name, {})

                return workbook_entry.get("workbook_scrape_update_required", False)
            except Exception as e:
                self.logger.error(f"❌ State file corrupt or unreadable: {e}. Defaulting to full compile safety block.")
                return True

        return True

    def reset_scraper_flag(self, workbooks_list):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)

                sheet_entry = state.get("spreadsheets", {}).get(self.spreadsheet_key, {})
                workbooks_entry = sheet_entry.get("workbooks", {})

                for wb in workbooks_list:
                    if wb in workbooks_entry:
                        workbooks_entry[wb]["workbook_scrape_update_required"] = False

                save_json_file(self.state_file, state)

                self.logger.info(f"✅ Registry synchronization update flags reset to False for elements: {workbooks_list}")
            except Exception as e:
                self.logger.critical(f"❌ Failed to reset local state update flags: {e}")

    # =========================================================================
    # FILE IO UTILITIES
    # =========================================================================
    def initialize_unified_record(self):
        return {
            "version": "",
            "perks": {
                "column1": [], "column2": [], "perk1": [], "perk2": [], "origin_trait": []
            },
            "info": {
                "rank": "", "tier": "", "priority": "", "role": "", "purpose": "", "tags": "", "type": "",
                "usage": "", "source": "", "notes": "", "description": "", "alternatives": "",
                "analysis": {
                    "roam": "", "dps": "", "day1": "", "chall": "", "speed": "", "effect": "", "flex": "", "power": ""
                }
            }
        }

    def _write_output_payload(self, payload, workbooks_dict, label="payload"):
        total_items = sum(len(v) for v in workbooks_dict.values() if isinstance(v, dict))
        if total_items == 0:
            self.logger.critical("❌ All workbooks produced zero items. Aborting write to prevent data loss.")
            self.logger.critical("   Possible causes: missing 'openpyxl', locked file, corrupted download, or empty source sheet.")
            return False

        with open(self.output_filename, "w", encoding="utf-8") as f_out:
            json.dump(payload, f_out, indent=2, ensure_ascii=False)
        self.logger.info(f"🎉 Successfully compiled {label} down to cache: {self.output_filename}")
        return True

    def _get_workbook_config(self, workbook_name):
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        for wb in ss_config.get("workbooks", []):
            if wb.get("name") == workbook_name:
                return wb
        return {}

    def get_workbook_file_path(self, workbook_name):
        clean_wb_name = str(workbook_name).lower().replace(" ", "_")
        wb_config = self._get_workbook_config(workbook_name)

        ext = "xlsx" if wb_config.get("use_xlsx") else "csv"
        return os.path.join(self.source_dir, f"{self.spreadsheet_key}_{clean_wb_name}.{ext}")

    def _read_file_as_dicts(self, file_path, skip_rows=0):
        if not os.path.exists(file_path):
            self.logger.error(f"❌ Cannot ingest dictionary records. Path missing: {file_path}")
            return []

        if file_path.endswith(".xlsx"):
            try:
                import openpyxl
            except ImportError:
                self.logger.critical("❌ Required execution library module dependency 'openpyxl' is missing! Run pip install openpyxl.")
                return []

            wb = openpyxl.load_workbook(file_path, data_only=True)
            sheet = wb.active

            raw_rows = list(sheet.iter_rows(values_only=True))
            if len(raw_rows) <= skip_rows:
                return []

            raw_rows = raw_rows[skip_rows:]
            header_row = [str(cell).strip() if cell is not None else None for cell in raw_rows[0]]

            dict_rows = []
            for row in raw_rows[1:]:
                row_dict = {}
                for idx, col_name in enumerate(header_row):
                    if col_name is not None:
                        val = row[idx] if idx < len(row) else None
                        row_dict[col_name] = val
                dict_rows.append(row_dict)
            return dict_rows
        else:
            import csv
            with open(file_path, mode="r", encoding="utf-8-sig") as f:
                for _ in range(skip_rows):
                    next(f, None)
                reader = csv.DictReader(f)
                return [{k.strip() if k else "": v for k, v in row.items()} for row in reader]

    def _read_file_raw_rows(self, file_path):
        if not os.path.exists(file_path):
            return []

        if file_path.endswith(".xlsx"):
            try:
                import openpyxl
            except ImportError:
                self.logger.critical("❌ Required execution library module dependency 'openpyxl' is missing!")
                return []
            wb = openpyxl.load_workbook(file_path, data_only=True)
            sheet = wb.active
            return [[str(cell).strip() if cell is not None else "" for cell in row] for row in sheet.iter_rows(values_only=True)]
        else:
            import csv
            with open(file_path, mode="r", encoding="utf-8-sig") as f:
                return list(csv.reader(f))

    def run(self):
        try:
            self._execute_processing()
        except Exception as err:
            self.logger.error(f"❌ Pipeline processing failed during runtime: {str(err)}", exc_info=True)
            raise err