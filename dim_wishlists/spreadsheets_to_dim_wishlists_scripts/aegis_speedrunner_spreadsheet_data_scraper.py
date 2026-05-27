# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
import os
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper


class AegisSpeedrunnerScraper(BaseSpreadsheetScraper):
    """
    Concrete scraper for the Aegis Speedrunner spreadsheet.

    This sheet catalogs weapons by their speedrun viability — how well they
    perform in time-attack scenarios, low-man raids, and optimized strike
    clears. It is structurally simpler than the endgame sheet: typically a
    single workbook with columns for weapon name, perks, purpose, usage notes,
    source, and a numeric rank (1-3 plus N/A).

    Inheritance from BaseSpreadsheetScraper provides:
        - YAML config loading and path resolution
        - Per-scraper logging
        - State-file integration (is_update_required / reset_scraper_flag)
        - Cell sanitization helpers
        - CSV/XLSX I/O abstractions
    """

    def __init__(self):
        """
        Initialize with the logical key "aegis_speedrunner".

        The output filename is hardcoded because this scraper always produces a
        single unified artifact regardless of how many workbooks the config lists.
        """
        super().__init__(spreadsheet_key="aegis_speedrunner")
        self.output_filename = os.path.join(
            self.output_dir, "aegis_speedrunner_spreadsheet_data_scraped.json"
        )

    def get_workbook_online_date(self, workbook_name, fallback="Unknown Date"):
        """
        Read the cached online modification date for a workbook.

        The downloader writes a companion .date file next to each downloaded
        workbook (e.g., aegis_speedrunner_weapons.csv -> aegis_speedrunner_weapons.date).
        This file contains the last-modified timestamp fetched from the Google
        Drive API, giving us provenance without re-querying the API during scrape.

        Args:
            workbook_name: the logical workbook name from config.yaml.
            fallback: string returned if the .date file is missing or unreadable.

        Returns:
            The stripped date string, or fallback on any I/O failure.
        """
        source_file = self.get_workbook_file_path(workbook_name)
        # Replace the extension with .date to locate the sidecar metadata file.
        date_file = os.path.splitext(source_file)[0] + ".date"
        if os.path.exists(date_file):
            try:
                with open(date_file, "r", encoding="utf-8") as df:
                    return df.read().strip()
            except Exception as e:
                # Log but do not crash; a missing date is non-fatal. The scraper
                # can still produce a valid payload with "Unknown Date".
                self.logger.error(f"⚠️ Failed to read date validation metadata cache file: {e}")
        return fallback

    def _execute_processing(self):
        """
        Main orchestration: check state, parse the single target workbook,
        translate ranks, and write the unified JSON artifact.

        Structural assumptions:
            - The speedrunner sheet contains exactly one content workbook.
            - Columns: Name, # (rank), Column 1, Column 2, Perk 1, Perk 2,
              Purpose, Usage, Source.
            - Divider rows starting with "==" are skipped.
            - Blank rows are skipped.

        Rank translation:
            The sheet stores raw numbers 1-3 and "N/A". We map these to
            human-readable labels so the wishlist converter does not need a
            separate lookup table.
        """
        # Pull the workbook list from config.yaml.
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        # Early-exit: if no workbook needs re-scraping, skip all I/O.
        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 Speedrunner data profiles match registry definitions. Scraping skipped.")
            return

        # The speedrunner scraper assumes a single workbook. We take the first
        # entry from the config list. If the list is empty, we log and abort.
        target_wb_name = workbooks_in_config[0] if workbooks_in_config else None
        if not target_wb_name:
            self.logger.error("❌ No workbooks found in config for speedrunner spreadsheet.")
            return

        # Resolve the on-disk path using the base-class convention.
        file_path = self.get_workbook_file_path(target_wb_name)
        if not os.path.exists(file_path):
            # The downloader may have failed or the file may have been deleted.
            # We warn and skip rather than crash, because the scheduler will
            # retry on the next 8-hour cycle.
            self.logger.warning(f"⚠️ Source file asset missing, mapping skipped: {file_path}")
            return

        self.logger.info(f"⚙️ Extracting speedrunner rows from source asset: {file_path}")

        # ------------------------------------------------------------------
        # Parse the workbook into a dict keyed by weapon display name.
        # ------------------------------------------------------------------
        extracted_weapons = {}
        rows = self._read_file_as_dicts(file_path, skip_rows=0)

        # Human-readable translations for the speedrunner rank taxonomy.
        rank_translations = {
            "1": "Best in Role, Must-Have",
            "2": "Alternate Inferior Pick, Niche",
            "3": "Situational, Unnecessary",
            "N/A": "Not Relevant"
        }

        for row in rows:
            name = row.get("Name")
            # Skip blank rows and visual divider rows.
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            # parse_name_and_version splits multi-line names into (name, version).
            clean_name, version_string = self.parse_name_and_version(name)

            # Seed a fresh canonical record so every entry has identical keys.
            record = self.initialize_unified_record()
            record["version"] = version_string

            # Perk columns: barrel/magazine (Column 1 / Column 2) and traits.
            record["perks"]["column1"] = self.sanitize_perk_cell(row.get("Column 1"))
            record["perks"]["column2"] = self.sanitize_perk_cell(row.get("Column 2"))
            record["perks"]["perk1"] = self.sanitize_perk_cell(row.get("Perk 1"))
            record["perks"]["perk2"] = self.sanitize_perk_cell(row.get("Perk 2"))

            # Rank translation: "#" column holds the raw numeric rank.
            raw_rank = str(row.get("#") or "").strip()
            translated_rank = rank_translations.get(raw_rank, raw_rank)

            # Populate info metadata. sanitize_info_cell collapses noise tokens.
            record["info"]["rank"] = self.sanitize_info_cell("rank", translated_rank)
            record["info"]["purpose"] = self.sanitize_info_cell("purpose", row.get("Purpose"))
            record["info"]["usage"] = self.sanitize_info_cell("usage", row.get("Usage"))
            record["info"]["source"] = self.sanitize_info_cell("source", row.get("Source"))

            # One entry per weapon name. Duplicate names overwrite, which is
            # acceptable because the speedrunner sheet is opinionated and does
            # not list the same weapon twice.
            extracted_weapons[clean_name] = record

        # ------------------------------------------------------------------
        # Assemble payload with provenance metadata
        # ------------------------------------------------------------------
        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {
                    # Pull the cached modification date from the downloader's
                    # sidecar .date file. This avoids an extra API call.
                    "date": self.get_workbook_online_date(target_wb_name),
                    "patch": ""  # Reserved for future Bungie patch correlation.
                },
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": {
                target_wb_name: extracted_weapons
            }
        }

        # _write_output_payload aborts if the payload is empty (zero weapons),
        # protecting against missing openpyxl, locked files, or empty sheets.
        write_ok = self._write_output_payload(
            output_payload, output_payload["workbooks"], label="speedrunner"
        )
        if write_ok:
            # Only clear state flags after a successful, non-empty write.
            # If write_ok is False, flags remain True so the next cycle retries.
            self.reset_scraper_flag(workbooks_in_config)


if __name__ == "__main__":
    AegisSpeedrunnerScraper().run()
