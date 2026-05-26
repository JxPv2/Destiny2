import os
import re
import json
from datetime import datetime
from core_spreadsheet_data_scraper import BaseSpreadsheetScraper


class AegisEndgameScraper(BaseSpreadsheetScraper):
    """
    Concrete scraper for the Aegis endgame PvE spreadsheet.

    This is a multi-tab "mega-sheet" that covers the entire Destiny 2 weapon
    sandbox across many specialized workbooks:

        Metadata workbooks (excluded from weapon output):
            - Status      -> current season / validity date.
            - Changelog   -> patch version history.

        Content workbooks (parsed into the JSON payload):
            - Shopping List          -> curated priority list with role and source.
            - Exotic Weapons         -> exotic primaries/specials/heavies with
                                       tier symbols and analysis scores.
            - Exotic Armor (ignore)  -> exotic armor pieces (routed to the same
                                       exotic parser despite the name).
            - <Weapon Archetype>     -> one tab per archetype (Autos, HCs, SMGs,
                                       BGLs, Rockets, Swords, etc.). Each tab
                                       contains legendary weapons with barrel,
                                       magazine, perk, and origin-trait columns.

    The scraper uses heuristic name matching to route each workbook to the
    correct parser, so new archetype tabs can be added to the sheet without
    requiring code changes.
    """

    def __init__(self):
        """
        Initialize with the logical key "aegis_endgame".

        weapon_archetype_tabs is a hardcoded set of every archetype tab name
        that the Aegis sheet is known to use. When _execute_processing()
        encounters a workbook name inside this set, it routes it to the generic
        parse_weapon_archetype() handler. If the sheet authors add a new tab
        (e.g., "Breach GLs"), simply adding the string to this set is enough
        for the scraper to ingest it on the next run.
        """
        super().__init__(spreadsheet_key="aegis_endgame")
        self.output_filename = os.path.join(
            self.output_dir, "aegis_endgame_spreadsheet_data_scraped.json"
        )

        self.weapon_archetype_tabs = {
            "Autos", "Bows", "HCs", "Pulses", "Scouts", "Sidearms", "SMGs", "BGLs", "Fusions",
            "Glaives", "Shotguns", "Snipers", "Rocket Sidearms", "Traces", "HGLs",
            "LFRs", "LMGs", "Rockets", "Swords", "Other"
        }

    def extract_metadata_date(self, file_path):
        """
        Scan the Status workbook to find the current operational date.

        The Status tab places the validity date in the second column (index 1)
        rather than the first. We scan every row, check cell index 1, and
        return the first value that matches a loose date pattern.

        Returns "Unknown Date" if the file is missing or no date is found.
        """
        if not os.path.exists(file_path):
            return "Unknown Date"

        date_pattern = re.compile(r'\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b')
        raw_rows = self._read_file_raw_rows(file_path)

        for row in raw_rows:
            # Guard against short rows; some status sheets have blank padding.
            if len(row) > 1 and row[1]:
                cell_value = str(row[1]).strip()
                if date_pattern.search(cell_value):
                    return cell_value
        return "Unknown Date"

    def extract_metadata_patch(self, file_path):
        """
        Scan the Changelog workbook to find the most recent patch string.

        The Aegis sheet stores the current patch in the very first cell of the
        changelog tab (row 0, column 0). This is typically a Bungie patch
        name like "Episode: Heresy" or "Update 8.0.5".

        Returns "Unknown Patch" if the file is missing or empty.
        """
        if not os.path.exists(file_path):
            return "Unknown Patch"

        raw_rows = self._read_file_raw_rows(file_path)
        if raw_rows and raw_rows[0] and raw_rows[0][0]:
            return str(raw_rows[0][0]).strip()
        return "Unknown Patch"

    def parse_shopping_list(self, file_path):
        """
        Parse the Shopping List workbook into a normalized weapon dict.

        The Shopping List is a curated "priority buy" list. Its columns are
        sparser than the full archetype tabs:

            - Name         -> weapon display name.
            - Column 1     -> first perk column (traits only, no barrel/mag).
            - Column 2     -> second perk column.
            - Role         -> endgame role label (e.g., "Add Clear", "Boss DPS").
            - Source       -> acquisition source (e.g., "Trials", "Raid").
            - #            -> numeric priority rank.
            - Priority     -> human-readable priority string.
            - Alternatives -> substitute weapons if this one is unavailable.

        Divider rows starting with "==" are skipped, as are blank rows.
        """
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

            # Shopping List only specifies trait perks, not barrel or magazine.
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
        """
        Parse a generic weapon-archetype workbook (e.g., "HCs", "Rockets").

        These tabs share a rigid column layout but may contain a banner row
        above the headers, so we skip_rows=1 to align DictReader with the
        true header row.

        Expected columns:
            - Name         -> weapon display name.
            - Barrel       -> column 1 perks (barrels, strings, batteries, etc.).
            - Mag          -> column 2 perks (magazines, fletching, etc.).
            - Perk 1       -> first trait column.
            - Perk 2       -> second trait column.
            - Origin Trait -> origin perk pool.
            - Rank         -> numeric or letter rank.
            - Tier         -> tier label.
            - Notes        -> freeform commentary.
        """
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

            # Full perk coverage: barrel/mag plus traits and origin.
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
        """
        Parse an exotic workbook (weapons or armor) with symbolic scoring.

        Exotic tabs use single-character symbols rather than prose tiers:
            Tier symbols:
                ✔  -> Optimal   (best-in-slot for the current meta).
                ▲  -> Viable    (usable but outclassed).
                !  -> Situational (niche, map-specific, or build-dependent).
                ✖  -> Wasted    (do not invest materials; outclassed or bugged).

            Type symbols:
                N -> Neutral (general-use exotic).
                S -> Swap      (swap-to-fire, then stow).
                H -> Hybrid    (mix of neutral and swap).
                M -> Movement  (mobility / utility focused).

        Analysis columns (scored per symbol above):
            - Roam   -> general ad-clear / neutral-game score.
            - DPS    -> sustained damage score.
            - Day 1  -> day-one raid viability.
            - Chall  -> challenge-mode / contest score.
            - Speed  -> speedrun / time-attack score.

        Header normalization:
            Exotic sheets are notorious for invisible BOM characters and mixed
            casing in headers (e.g., "﻿name", "Name", "TIER"). We lowercase
            every key and strip BOMs so downstream lookups are case-insensitive
            and BOM-resilient.
        """
        items_map = {}
        if not os.path.exists(file_path):
            return items_map

        rows = self._read_file_as_dicts(file_path, skip_rows=skip_rows)

        # Translation tables for the symbolic shorthand used by sheet authors.
        tier_translations = {
            "✔": "Optimal", "▲": "Viable", "!": "Situational", "✖": "Wasted"
        }
        type_translations = {
            "N": "Neutral", "S": "Swap", "H": "Hybrid", "M": "Movement"
        }

        for row in rows:
            # Enforce clean lowercase key maps to normalize BOM inconsistencies.
            # The replace("﻿", "") strips the zero-width no-break space that
            # Excel and Google Sheets inject at the start of exported headers.
            clean_row = {
                str(k).lower().strip().replace("\ufeff", ""): v
                for k, v in row.items() if k is not None
            }

            name = clean_row.get("name")
            if not name or str(name).strip() == "" or str(name).startswith("=="):
                continue

            clean_name, version_string = self.parse_name_and_version(name)
            record = self.initialize_unified_record()
            record["version"] = version_string

            # Translate the one-letter type symbol into a human-readable label.
            raw_type = str(clean_row.get("type") or "").strip()
            record["info"]["type"] = self.sanitize_info_cell(
                "type", type_translations.get(raw_type, raw_type)
            )

            # Translate the tier symbol.
            raw_tier = str(clean_row.get("tier") or "").strip()
            record["info"]["tier"] = self.sanitize_info_cell(
                "tier", tier_translations.get(raw_tier, raw_tier)
            )

            record["info"]["tags"] = self.sanitize_info_cell("tags", clean_row.get("tags"))
            record["info"]["description"] = self.sanitize_info_cell(
                "description", clean_row.get("description")
            )

            # Map the five analysis dimensions. "Day 1" contains a space, so we
            # special-case its JSON key to "day1" while keeping the lookup key
            # lowercased for the clean_row dict.
            for key in ["Roam", "DPS", "Day 1", "Chall", "Speed"]:
                lookup_key = key.lower()
                raw_sym = str(clean_row.get(lookup_key) or "").strip()
                trans_sym = tier_translations.get(raw_sym, raw_sym)
                json_key = "day1" if key == "Day 1" else key.lower()
                record["info"]["analysis"][json_key] = self.sanitize_info_cell(
                    json_key, trans_sym
                )

            items_map[clean_name] = record
        return items_map

    def _execute_processing(self):
        """
        Main orchestration: route each workbook to the correct parser, assemble
        the unified payload, and write the JSON artifact.

        Routing logic:
            1. Skip "Status" and "Changelog" — they are metadata, not weapon data.
            2. "Shopping List" -> parse_shopping_list().
            3. "Exotic Weapons" -> parse_exotic_sheet(skip_rows=1).
               (skip_rows=1 because the exotic weapons tab has a banner row.)
            4. "Exotic Armor (ignore)" -> parse_exotic_sheet(skip_rows=0).
               (No banner row; headers start immediately.)
            5. Any name in weapon_archetype_tabs -> parse_weapon_archetype().

        Early-exit:
            If no workbook flags are set, we skip all I/O to keep the pipeline
            cycle fast.
        """
        ss_config = self.config.get("spreadsheets", {}).get(self.spreadsheet_key, {})
        workbooks_in_config = [wb.get("name") for wb in ss_config.get("workbooks", [])]

        # Guard: if every workbook is up-to-date, bail out immediately.
        if not any(self.is_update_required(wb) for wb in workbooks_in_config):
            self.logger.info("🟩 All endgame workbook profiles match local registry maps. Scraping skipped.")
            return

        # ------------------------------------------------------------------
        # Resolve metadata workbooks
        # ------------------------------------------------------------------
        # We assume the sheet always contains "Status" and "Changelog" by those
        # exact names. If they are missing, the extractors degrade gracefully
        # to "Unknown Date" / "Unknown Patch".
        status_path = self.get_workbook_file_path("Status")
        changelog_path = self.get_workbook_file_path("Changelog")
        sheet_date = self.extract_metadata_date(status_path)
        sheet_patch = self.extract_metadata_patch(changelog_path)

        # ------------------------------------------------------------------
        # Route content workbooks to their parsers
        # ------------------------------------------------------------------
        compiled_workbooks = {}

        for wb_name in workbooks_in_config:
            # Metadata workbooks do not produce weapon entries.
            if wb_name in ["Status", "Changelog"]:
                continue

            file_path = self.get_workbook_file_path(wb_name)
            self.logger.info(f"⚙️ Mapping sheet layout structure: '{wb_name}' from {file_path}")

            # Heuristic routing based on workbook name.
            if wb_name == "Shopping List":
                compiled_workbooks[wb_name] = self.parse_shopping_list(file_path)
            elif wb_name == "Exotic Weapons":
                # Exotic Weapons has a visual banner above the headers.
                compiled_workbooks[wb_name] = self.parse_exotic_sheet(file_path, skip_rows=1)
            elif wb_name == "Exotic Armor (ignore)":
                # Despite the "(ignore)" suffix, we still scrape it so the
                # wishlist converter can decide whether to include or exclude
                # exotic armor recommendations. skip_rows=0 because headers
                # are on the first row.
                compiled_workbooks[wb_name] = self.parse_exotic_sheet(file_path, skip_rows=0)
            elif wb_name in self.weapon_archetype_tabs:
                # Generic archetype handler covers the bulk of the sheet.
                compiled_workbooks[wb_name] = self.parse_weapon_archetype(file_path)

        # ------------------------------------------------------------------
        # Assemble and write payload
        # ------------------------------------------------------------------
        output_payload = {
            "spreadsheet": {
                "name": ss_config.get("name", self.spreadsheet_key),
                "changelog": {"date": sheet_date, "patch": sheet_patch},
                "link": f"https://docs.google.com/spreadsheets/d/{ss_config.get('id', '')}",
                "scrape_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "workbooks": compiled_workbooks
        }

        # _write_output_payload aborts if the aggregate payload is empty.
        write_ok = self._write_output_payload(
            output_payload, output_payload["workbooks"], label="endgame"
        )
        if write_ok:
            # Clear flags only after a successful, non-empty write.
            self.reset_scraper_flag(workbooks_in_config)


if __name__ == "__main__":
    AegisEndgameScraper().run()