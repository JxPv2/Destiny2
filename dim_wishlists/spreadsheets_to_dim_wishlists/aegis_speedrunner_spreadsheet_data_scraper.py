import os
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper

class AegisSpeedrunnerScraper(BaseSpreadsheetScraper):
    """
    Scraper engine for processing the Aegis Speedrunner spreadsheet.
    Inherits data sanitization and logging mechanics from the base scraper class.
    """
    def __init__(self):
        super().__init__(spreadsheet_key="aegis_speedrunner")
        self.output_filename = os.path.join(self.output_dir, "aegis_speedrunner_spreadsheet_data_scraped.json")

    def get_workbook_online_date(self, workbook_name, fallback="Unknown Date"):
        """Extracts the cached online modification date metadata for targeted workbooks."""
        source_file = self.get_workbook_file_path(workbook_name)
        date_file = os.path.splitext(source_file)[0] + ".date"
        if os.path.exists(date_file):
            try:
                with open(date_file, "r", encoding="utf-8") as df:
                    return df.read().strip()
            except Exception as e:
                self.logger.error(f"⚠️ Failed to read date validation metadata cache file: {e}")
        return fallback

    def _execute_processing(self):
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 Speedrunner data profiles match registry definitions. Scraping skipped.")
            return

        target_wb_name = workbooks_in_config[0] if workbooks_in_config else None
        if not target_wb_name:
            self.logger.error("❌ No workbooks found in config for speedrunner spreadsheet.")
            return

        file_path = self.get_workbook_file_path(target_wb_name)
        if not os.path.exists(file_path):
            self.logger.warning(f"⚠️ Source file asset missing, mapping skipped: {file_path}")
            return

        self.logger.info(f"⚙️ Extracting speedrunner rows from source asset: {file_path}")
        extracted_weapons = {}
        rows = self._read_file_as_dicts(file_path, skip_rows=0)

        rank_translations = {
            "1": "Best in Role, Must-Have",
            "2": "Alternate Inferior Pick, Niche",
            "3": "Situational, Unnecessary",
            "N/A": "Not Relevant"
        }

        for row in rows:
            name = row.get("Name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)

            record = self.initialize_unified_record()
            record["version"] = version_string

            record["perks"]["column1"] = self.sanitize_perk_cell(row.get("Column 1"))
            record["perks"]["column2"] = self.sanitize_perk_cell(row.get("Column 2"))
            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Perk 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Perk 2"))

            raw_rank = str(row.get("#") or "").strip()
            translated_rank = rank_translations.get(raw_rank, raw_rank)

            record["info"]["rank"] = self.sanitize_info_cell("rank", translated_rank)
            record["info"]["purpose"] = self.sanitize_info_cell("purpose", row.get("Purpose"))
            record["info"]["usage"] = self.sanitize_info_cell("usage", row.get("Usage"))
            record["info"]["source"] = self.sanitize_info_cell("source", row.get("Source"))

            extracted_weapons[clean_name] = record

        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {
                    "date": self.get_workbook_online_date(target_wb_name),
                    "patch": ""
                },
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": {
                target_wb_name: extracted_weapons
            }
        }

        write_ok = self._write_output_payload(output_payload, output_payload["workbooks"], label="speedrunner")
        if write_ok:
            self.reset_scraper_flag(workbooks_in_config)

if __name__ == "__main__":
    AegisSpeedrunnerScraper().run()