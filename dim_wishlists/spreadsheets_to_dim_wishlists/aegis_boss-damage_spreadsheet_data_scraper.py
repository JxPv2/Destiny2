import os
import re
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper

class AegisBossDamageScraper(BaseSpreadsheetScraper):
    """
    Scraper engine tailored to isolate tactical boss-damage equipment rankings.
    Utilizes common parent sanitization processes.
    """
    def __init__(self):
        super().__init__(spreadsheet_key="aegis_boss-damage")
        self.output_filename = os.path.join(self.output_dir, "aegis_boss-damage_spreadsheet_data_scraped.json")

    def extract_latest_changelog_date(self, file_path):
        """Scans the changelog workbook component grid to isolate documentation updates."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Changelog asset spreadsheet missing: {file_path}")

        date_pattern = re.compile(r'\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}')
        raw_rows = self._read_file_raw_rows(file_path)

        for row in raw_rows:
            if row and row[0]:
                cell_value = str(row[0]).strip()
                if date_pattern.search(cell_value):
                    self.logger.info(f"🔎 Isolated sheet historical modification date stamp: {cell_value}")
                    return cell_value
        return "Unknown Date"

    def parse_equipment_sheet(self, file_path):
        """Parses weapon damage tables and normalizes structural variation rules."""
        equipment_map = {}
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Equipment source file asset missing: {file_path}")

        rows = self._read_file_as_dicts(file_path, skip_rows=0)

        # Human-readable label transformations for the spreadsheet rating values
        rank_translations = {
            "1": "Meta-Defining",
            "2": "Situational",
            "3": "Extremely Niche",
            "4": "Not Meta, Worse Alternative"
        }

        self.logger.info(f"⚙️ Compiling records loop matrix from target asset: {file_path}")
        for row in rows:
            name = row.get("Name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)

            # Instantiates uniform data matrices safely matching schema parameters
            record = self.initialize_unified_record()
            record["version"] = version_string

            # Extract perk structures cleanly using fallbacks for structural safety
            record["perks"]["column1"] = self.sanitize_perk_cell(row.get("Column 1"))
            record["perks"]["column2"] = self.sanitize_perk_cell(row.get("Column 2"))
            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Perk 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Perk 2"))

            # Handle cross-version header name variations elegantly
            rank_id = str(row.get("#") or row.get("Rank") or "").strip()
            translated_rank = rank_translations.get(rank_id, rank_id)

            record["info"]["rank"] = self.sanitize_info_cell("rank", translated_rank)
            record["info"]["role"] = self.sanitize_info_cell("role", row.get("Role"))
            record["info"]["notes"] = self.sanitize_info_cell("notes", row.get("Notes"))

            equipment_map[clean_name] = record

        return equipment_map

    def _execute_processing(self):
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 Associated boss damage data elements match local registers perfectly. Compilation skipped.")
            return

        changelog_wb_name = None
        equipment_wb_name = None
        for wb_name in workbooks_in_config:
            if "changelog" in wb_name.lower():
                changelog_wb_name = wb_name
            else:
                equipment_wb_name = wb_name

        if not changelog_wb_name or not equipment_wb_name:
            self.logger.error("❌ Could not identify Changelog and Equipment workbooks from config.")
            return

        changelog_path = self.get_workbook_file_path(changelog_wb_name)
        equipment_path = self.get_workbook_file_path(equipment_wb_name)

        document_date = self.extract_latest_changelog_date(changelog_path)
        extracted_weapons = self.parse_equipment_sheet(equipment_path)

        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {
                    "date": document_date,
                    "patch": ""  
                },
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": {
                equipment_wb_name: extracted_weapons
            }
        }

        write_ok = self._write_output_payload(output_payload, output_payload["workbooks"], label="boss damage")
        if write_ok:
            self.reset_scraper_flag(workbooks_in_config)

if __name__ == "__main__":
    AegisBossDamageScraper().run()