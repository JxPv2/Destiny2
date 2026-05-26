import os
import re
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper

class AegisEndgameScraper(BaseSpreadsheetScraper):
    """
    Scraper engine built to extract information from multiple complex worksheet tabs.
    Runs all parsed assets through the parent class data filters.
    """
    def __init__(self):
        super().__init__(spreadsheet_key="aegis_endgame")
        self.output_filename = os.path.join(self.output_dir, "aegis_endgame_spreadsheet_data_scraped.json")

        self.weapon_archetype_tabs = {
            "Autos", "Bows", "HCs", "Pulses", "Scouts", "Sidearms", "SMGs", "BGLs", "Fusions", 
            "Glaives", "Shotguns", "Snipers", "Rocket Sidearms", "Traces", "HGLs", 
            "LFRs", "LMGs", "Rockets", "Swords", "Other"
        }

    def extract_metadata_date(self, file_path):
        """Extracts systemic operational date limits from the validation spreadsheet layout."""
        if not os.path.exists(file_path):
            return "Unknown Date"
        date_pattern = re.compile(r'\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}')
        raw_rows = self._read_file_raw_rows(file_path)
        for row in raw_rows:
            if len(row) > 1 and row[1]:
                cell_value = str(row[1]).strip()
                if date_pattern.search(cell_value):
                    return cell_value
        return "Unknown Date"

    def extract_metadata_patch(self, file_path):
        """Extracts active application sandbox build indices from system change records."""
        if not os.path.exists(file_path):
            return "Unknown Patch"
        raw_rows = self._read_file_raw_rows(file_path)
        if raw_rows and raw_rows[0] and raw_rows[0][0]:
            return str(raw_rows[0][0]).strip()
        return "Unknown Patch"

    def parse_shopping_list(self, file_path):
        """Compiles structural priority metadata tables cleanly."""
        items_map = {}
        if not os.path.exists(file_path):
            return items_map

        rows = self._read_file_as_dicts(file_path, skip_rows=0)
        for row in rows:
            name = row.get("Name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)
            record = self.initialize_unified_record()
            record["version"] = version_string

            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Column 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Column 2"))

            record["info"]["role"] = self.sanitize_info_cell("role", row.get("Role"))
            record["info"]["source"] = self.sanitize_info_cell("source", row.get("Source"))
            record["info"]["rank"] = self.sanitize_info_cell("rank", row.get("#"))
            record["info"]["priority"] = self.sanitize_info_cell("priority", row.get("Priority"))
            record["info"]["alternatives"] = self.sanitize_info_cell("alternatives", row.get("Alternatives"))

            items_map[clean_name] = record
        return items_map

    def parse_weapon_archetype(self, file_path):
        """Ingests standardized frame records from general archetype categories."""
        items_map = {}
        if not os.path.exists(file_path):
            return items_map

        rows = self._read_file_as_dicts(file_path, skip_rows=1)
        for row in rows:
            name = row.get("Name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)
            record = self.initialize_unified_record()
            record["version"] = version_string

            record["perks"]["column1"] = self.sanitize_perk_cell(row.get("Barrel"))
            record["perks"]["column2"] = self.sanitize_perk_cell(row.get("Mag"))
            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Perk 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Perk 2"))
            record["perks"]["origin_trait"] = self.sanitize_perk_cell(row.get("Origin Trait"))

            record["info"]["rank"] = self.sanitize_info_cell("rank", row.get("Rank"))
            record["info"]["tier"] = self.sanitize_info_cell("tier", row.get("Tier"))
            record["info"]["notes"] = self.sanitize_info_cell("notes", row.get("Notes"))

            items_map[clean_name] = record
        return items_map

    def parse_exotic_sheet(self, file_path, skip_rows=0):
        """Extracts tactical performance metrics from complex exotic tabs."""
        items_map = {}
        if not os.path.exists(file_path):
            return items_map

        rows = self._read_file_as_dicts(file_path, skip_rows=skip_rows)

        tier_translations = {
            "✔": "Optimal", "▲": "Viable", "!": "Situational", "✖": "Wasted"
        }
        type_translations = {
            "N": "Neutral", "S": "Swap", "H": "Hybrid", "M": "Movement"
        }

        for row in rows:
            # Enforce clean lowercase key maps to normalize BOM inconsistencies
            clean_row = {str(k).lower().strip().replace("﻿", ""): v for k, v in row.items() if k is not None}

            name = clean_row.get("name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)
            record = self.initialize_unified_record()
            record["version"] = version_string

            raw_type = str(clean_row.get("type") or "").strip()
            record["info"]["type"] = self.sanitize_info_cell("type", type_translations.get(raw_type, raw_type))

            raw_tier = str(clean_row.get("tier") or "").strip()
            record["info"]["tier"] = self.sanitize_info_cell("tier", tier_translations.get(raw_tier, raw_tier))

            record["info"]["tags"] = self.sanitize_info_cell("tags", clean_row.get("tags"))
            record["info"]["description"] = self.sanitize_info_cell("description", clean_row.get("description"))

            for key in ["Roam", "DPS", "Day 1", "Chall", "Speed"]:
                lookup_key = key.lower()
                raw_sym = str(clean_row.get(lookup_key) or "").strip()
                trans_sym = tier_translations.get(raw_sym, raw_sym)
                json_key = "day1" if key == "Day 1" else key.lower()
                record["info"]["analysis"][json_key] = self.sanitize_info_cell(json_key, trans_sym)

            items_map[clean_name] = record
        return items_map

    def _execute_processing(self):
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 All endgame workbook profiles match local registry maps. Scraping skipped.")
            return

        status_path = self.get_workbook_file_path("Status")
        changelog_path = self.get_workbook_file_path("Changelog")
        sheet_date = self.extract_metadata_date(status_path)
        sheet_patch = self.extract_metadata_patch(changelog_path)

        compiled_workbooks = {}

        for wb_name in workbooks_in_config:
            if wb_name in ["Status", "Changelog"]:
                continue

            file_path = self.get_workbook_file_path(wb_name)
            self.logger.info(f"⚙️ Mapping sheet layout structure: '{wb_name}' from {file_path}")

            if wb_name == "Shopping List":
                compiled_workbooks[wb_name] = self.parse_shopping_list(file_path)
            elif wb_name == "Exotic Weapons":
                compiled_workbooks[wb_name] = self.parse_exotic_sheet(file_path, skip_rows=1)
            elif wb_name == "Exotic Armor (ignore)":
                compiled_workbooks[wb_name] = self.parse_exotic_sheet(file_path, skip_rows=0)
            elif wb_name in self.weapon_archetype_tabs:
                compiled_workbooks[wb_name] = self.parse_weapon_archetype(file_path)

        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {"date": sheet_date, "patch": sheet_patch},
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": compiled_workbooks
        }

        write_ok = self._write_output_payload(output_payload, output_payload["workbooks"], label="endgame")
        if write_ok:
            self.reset_scraper_flag(workbooks_in_config)

if __name__ == "__main__":
    AegisEndgameScraper().run()