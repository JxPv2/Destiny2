# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
import os
import re
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper


class AegisBossDamageScraper(BaseSpreadsheetScraper):
    """
    Concrete scraper for the Aegis boss-damage spreadsheet.

    This sheet catalogs weapons by their DPS viability against raid and dungeon
    bosses. It typically contains two workbooks:
        1. A changelog/history workbook with version dates in the first column.
        2. An equipment workbook listing weapons, their recommended perks, and a
           numeric rank (1-4) translating to meta tiers.

    Inheritance from BaseSpreadsheetScraper provides:
        - YAML config loading and path resolution
        - Per-scraper logging
        - State-file integration (is_update_required / reset_scraper_flag)
        - Cell sanitization helpers
        - CSV/XLSX I/O abstractions
    """

    def __init__(self):
        """
        Initialize with the logical key "aegis_boss-damage".

        The output filename is hardcoded here rather than synthesized from the
        workbook name because this scraper always produces a single unified
        artifact covering all equipment workbooks.
        """
        super().__init__(spreadsheet_key="aegis_boss-damage")
        self.output_filename = os.path.join(
            self.output_dir, "aegis_boss-damage_spreadsheet_data_scraped.json"
        )

    def extract_latest_changelog_date(self, file_path):
        """
        Scan the changelog workbook to find the most recent documentation date.

        Community-maintained changelogs usually list dates in the first column,
        most recent at the top. We scan row-by-row, cell 0, and return the first
        string that matches a loose date pattern.

        Regex breakdown:
            \b                -> word boundary (prevents matching inside GUIDs).
            \d{1,4}           -> year or day (handles both YYYY-MM-DD and DD/MM/YY).
            [-/.]             -> common date separators.
            \d{1,2}           -> month or day.
            [-/.]             -> second separator.
            \d{1,4}           -> remaining component.
            \b                -> trailing word boundary.

        Returns "Unknown Date" if no cell matches, so the downstream payload
        always has a string rather than None.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Changelog asset spreadsheet missing: {file_path}")

        date_pattern = re.compile(r'\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b')
        raw_rows = self._read_file_raw_rows(file_path)

        for row in raw_rows:
            if row and row[0]:
                cell_value = str(row[0]).strip()
                if date_pattern.search(cell_value):
                    self.logger.info(f"🔎 Isolated sheet historical modification date stamp: {cell_value}")
                    return cell_value
        return "Unknown Date"

    def parse_equipment_sheet(self, file_path):
        """
        Parse the weapon-damage workbook into a normalized dict keyed by weapon name.

        The source sheet columns are expected to contain:
            - Name        -> weapon display name (may include version on line 2).
            - # / Rank    -> numeric tier (1-4). Header varies across sheet versions.
            - Role        -> damage archetype label (e.g., "Burst DPS", "Total DPS").
            - Column 1    -> barrel / magazine perks.
            - Column 2    -> battery / magazine / blade perks.
            - Perk 1      -> first trait column.
            - Perk 2      -> second trait column.
            - Notes       -> freeform commentary.

        Rank translation:
            The sheet stores raw numbers 1-4. We map these to human-readable
            labels so the wishlist converter and DIM display strings are
            immediately meaningful without extra lookup tables.

        Row filtering:
            - Empty names are skipped.
            - Names starting with "==" are treated as section headers or
              dividers inserted by the sheet author for visual grouping;
              they do not represent actual weapons.
        """
        equipment_map = {}
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Equipment source file asset missing: {file_path}")

        rows = self._read_file_as_dicts(file_path, skip_rows=0)

        # Human-readable label transformations for the spreadsheet rating values.
        # These tiers are specific to the Aegis community's boss-damage taxonomy.
        rank_translations = {
            "1": "Meta-Defining",
            "2": "Situational",
            "3": "Extremely Niche",
            "4": "Not Meta, Worse Alternative"
        }

        self.logger.info(f"⚙️ Compiling records loop matrix from target asset: {file_path}")
        for row in rows:
            name = row.get("Name")
            # Skip blank rows and visual divider rows that start with "==".
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            # parse_name_and_version splits "Palindrome\nVersion 3.2.1" into
            # ("Palindrome", "3.2.1"). For boss-damage sheets the version is
            # often absent, in which case version_string becomes "".
            clean_name, version_string = self.parse_name_and_version(name)

            # Seed a fresh record with the canonical schema so every weapon
            # entry has identical keys, even if the sheet leaves columns blank.
            record = self.initialize_unified_record()
            record["version"] = version_string

            # Extract perk structures cleanly using fallbacks for structural safety.
            # Column 1 / Column 2 map to the first two socket columns (barrels,
            # magazines, batteries, etc.). Perk 1 / Perk 2 map to the trait columns.
            # Origin trait is left empty because boss-damage sheets typically do
            # not call out origin perks as part of the DPS calculation.
            record["perks"]["column1"] = self.sanitize_perk_cell(row.get("Column 1"))
            record["perks"]["column2"] = self.sanitize_perk_cell(row.get("Column 2"))
            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Perk 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Perk 2"))

            # Handle cross-version header name variations elegantly.
            # Older revisions of the sheet used "#" as the rank column; newer
            # revisions renamed it to "Rank". We accept either so the scraper
            # does not break when the sheet owner restructures headers.
            rank_id = str(row.get("#") or row.get("Rank") or "").strip()
            translated_rank = rank_translations.get(rank_id, rank_id)

            # Populate info metadata. sanitize_info_cell collapses "N/A", "-",
            # and "NONE" into empty strings (except for rank, which is handled
            # above and is always meaningful here).
            record["info"]["rank"] = self.sanitize_info_cell("rank", translated_rank)
            record["info"]["role"] = self.sanitize_info_cell("role", row.get("Role"))
            record["info"]["notes"] = self.sanitize_info_cell("notes", row.get("Notes"))

            # The boss-damage sheet is opinionated: one entry per weapon name.
            # If a name appears twice (rare, usually a copy-paste error), the
            # second occurrence overwrites the first. This matches the sheet's
            # intent of having a single canonical DPS ranking per weapon.
            equipment_map[clean_name] = record

        return equipment_map

    def _execute_processing(self):
        """
        Main orchestration: decide whether work is needed, locate the changelog
        and equipment workbooks by heuristic name matching, parse them, and
        write the unified JSON artifact.

        Workbook identification heuristic:
            - Any workbook whose name contains "changelog" (case-insensitive)
              is treated as the changelog.
            - The first non-changelog workbook is assumed to be the equipment
              data sheet. This supports the common two-workbook layout without
              requiring rigid naming conventions in config.yaml.

        Early-exit optimization:
            If none of the configured workbooks flag workbook_scrape_update_required,
            we skip all I/O. This keeps the 8-hour pipeline cycle fast when the
            Google Sheet has not changed.
        """
        # Pull the workbook list from config.yaml so we know what files to look
        # for in the source_dir.
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        # Guard: if every workbook is up-to-date, bail out immediately.
        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 Associated boss damage data elements match local registers perfectly. Compilation skipped.")
            return

        # ------------------------------------------------------------------
        # Heuristic workbook pairing
        # ------------------------------------------------------------------
        # We do not hardcode workbook names; instead we classify by substring.
        # This lets the sheet owner rename workbooks (e.g., "v2 Changelog",
        # "Boss Damage Weapons") without breaking the scraper.
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

        # Resolve filesystem paths using the base-class convention.
        changelog_path = self.get_workbook_file_path(changelog_wb_name)
        equipment_path = self.get_workbook_file_path(equipment_wb_name)

        # ------------------------------------------------------------------
        # Parse and assemble payload
        # ------------------------------------------------------------------
        document_date = self.extract_latest_changelog_date(changelog_path)
        extracted_weapons = self.parse_equipment_sheet(equipment_path)

        # The output schema wraps weapon data inside metadata about the source
        # sheet. This provenance block is consumed by the wishlist converter to
        # inject sheet attribution comments into the final DIM wishlist file.
        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {
                    "date": document_date,
                    "patch": ""  # Reserved for future Bungie patch correlation.
                },
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": {
                equipment_wb_name: extracted_weapons
            }
        }

        # _write_output_payload aborts if the payload is empty (zero weapons),
        # protecting against openpyxl import failures or empty downloads.
        write_ok = self._write_output_payload(output_payload, output_payload["workbooks"], label="boss damage")
        if write_ok:
            # Only clear the state flags after a successful, non-empty write.
            # If write_ok is False, the flags remain True so the next pipeline
            # cycle will retry.
            self.reset_scraper_flag(workbooks_in_config)


if __name__ == "__main__":
    AegisBossDamageScraper().run()
