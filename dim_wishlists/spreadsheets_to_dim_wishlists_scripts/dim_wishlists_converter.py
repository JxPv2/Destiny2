# D2-Stuff — Auto-generated DIM wishlists from community spreadsheets
# Copyright (C) 2026 JxPv2
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
import os
import json
import yaml
import logging
import itertools
import unicodedata
from datetime import datetime

from pipeline_utils import (
    bootstrap_system_paths,
    PipelineIndentedFormatter,
    save_json_file,
    setup_root_console_logging,
    IndentAdapter,
    DuplicateInfoFilter,
    setup_module_logger,
    load_config,
    ensure_spreadsheet_state,
    CONFIG_FILE,
)

# =============================================================================
# SECTION 1: BOOTSTRAP PATHS
# =============================================================================
# Resolve canonical folders from the pipeline's path registry so this script
# can be run from any working directory.
SYSTEM_PATHS = bootstrap_system_paths()
STATE_FILE = SYSTEM_PATHS["state_file"]
LOG_DIR = SYSTEM_PATHS["log_dir"]
MANIFEST_DIR = SYSTEM_PATHS["manifest_dir"]
SCRAPED_DIR = SYSTEM_PATHS["scraped_dir"]
WISHLIST_DIR = SYSTEM_PATHS["wishlist_dir"]

# The processed manifest produced by bungie_manifest_compiler.py.
PROCESSED_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "bungie_manifest_processed.json")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(WISHLIST_DIR, exist_ok=True)

# =============================================================================
# SECTION 2: LOGGING
# =============================================================================
# Base layout used by PipelineIndentedFormatter. It preserves timestamps and
# levels while visually indenting continuation lines.
LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
custom_formatter = PipelineIndentedFormatter(fmt=LOG_LAYOUT)

# =============================================================================
# FILTER: Duplicate specific INFO records to warnings log for context
# =============================================================================
# Module logger: isolated from root noise, dual-file output.
logger = setup_module_logger(
    "dim_wishlists_converter",
    LOG_DIR,
    warnings_log=True,
    dupe_keywords=["launching", "processing", "parsing nested", "=" * 80]
)

# =============================================================================
# Attach warnings handler (setup_module_logger creates it but forgets to add it)
# =============================================================================
# The warnings log captures WARNING+ records for easy tailing. setup_module_logger
# builds the handler internally for DuplicateInfoFilter but never adds it to the
# logger's handler list, so WARNING records never reach it. We attach it here.
_script_stem = os.path.splitext(os.path.basename(__file__))[0]
warnings_handler = logging.FileHandler(
    os.path.join(LOG_DIR, f"{_script_stem}_warnings.log"),
    encoding="utf-8"
)
warnings_handler.setFormatter(custom_formatter)
warnings_handler.setLevel(logging.WARNING)
logger.addHandler(warnings_handler)

# =============================================================================
# LoggerAdapters for hierarchical indentation
# =============================================================================
# IndentAdapter injects an "indent" key into the LogRecord's extra dict.
# PipelineIndentedFormatter reads this key and prepends spaces so nested
# output (workbook -> weapon -> perk) is visually scannable.
# Three indentation tiers for the three nesting levels in execute_pipeline():
#   workbook_logger  -> workbook name banner (2 spaces)
#   details_logger   -> per-weapon results    (3 spaces)
#   warning_logger   -> diagnostic reasons    (4 spaces)
workbook_logger = IndentAdapter(logger, 2)
details_logger = IndentAdapter(logger, 3)
warning_logger = IndentAdapter(logger, 4)

# =============================================================================
# SECTION 3: CORE COMPILATION AND COMBINATORICS ENGINE
# =============================================================================
class DIMWishlistGenerator:
    def __init__(self):
        # Populated by _load_manifest_lookups() from the processed manifest.
        self.perk_map = {}          # perk_name -> list of hashes
        self.item_map = {}          # cleaned_item_name -> list of instances

        # Set of item names that bypass manifest perk-pool validation.
        # Configured in config.yaml under ignore_manifest_perk_pool.
        self.ignore_manifest_perk_pool = self._load_ignore_manifest_perk_pool()

    def _load_ignore_manifest_perk_pool(self):
        """
        Load the list of items that bypass manifest perk validation.

        Some weapons (e.g., exotics with unique perk pools, or items whose
        Bungie API entries are incomplete) should not be rejected just because
        a perk hash is absent from the weapon's valid_perks list. This set
        contains lowercase cleaned names; any match in generate_item_wishlist()
        skips the pool-membership check.
        """
        config = load_config()
        items = config.get("ignore_manifest_perk_pool", [])
        # Normalize to lowercase for case-insensitive matching.
        return {item.strip().lower() for item in items if isinstance(item, str)}

    def _clean(self, text):
        """
        Normalize an item name for dictionary lookup.

        Steps:
          1. Strip leading/trailing whitespace.
          2. Lowercase.
          3. Remove " (adept)" and "(adept)" suffixes so that "Palindrome (Adept)"
             and "Palindrome" share the same lookup key.
        """
        if not text:
            return ""
        return text.strip().lower().replace(" (adept)", "").replace("(adept)", "")

    def _fold_diacritics(self, text):
        """
        Remove diacritical marks from text for accent-insensitive matching.

        Example: "Häkke" -> "Hakke". This is needed because some community
        spreadsheets use plain ASCII while Bungie's API uses decorated names.
        """
        if not text:
            return ""
        # NFD splits "ä" into "a" + combining diaeresis; we drop the combining marks.
        decomposed = unicodedata.normalize('NFD', text)
        return ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn')

    def _tokenize(self, text):
        """
        Split a perk name into lowercase tokens for subset matching.

        Tokens are whitespace-separated words. Punctuation attached to words
        is stripped so "Frame," and "Frame" share the same token.
        """
        if not text:
            return set()
        # Split on whitespace, strip common punctuation from each token
        raw_tokens = text.lower().split()
        cleaned = set()
        for t in raw_tokens:
            # Strip leading/trailing punctuation
            stripped = t.strip(".,;:!?()[]{}\"'")
            if stripped:
                cleaned.add(stripped)
        return cleaned

    def _load_manifest_lookups(self):
        """
        Hydrate self.perk_map and self.item_map from the processed manifest.

        perk_map:           perk display name -> list of integer hashes.
        perk_map_lower:     lowercase name -> original key (case-insensitive).
        perk_map_folded:    diacritic-stripped lowercase -> original key.
        perk_map_by_hash:   hash integer -> canonical perk name (reverse lookup).

        item_map:           cleaned weapon/armor name -> list of instances.
                            Each instance is {"item_id": int, "valid_perks": [int, ...]}.

        The manifest may be in the old flat format (top-level keys are item
        names with list values) or the new structured format ({"weapons": ...,
        "exotic_armor": ..., "perks": ...}). We handle both.
        """
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            logger.critical(f"Critical execution block: Manifest mapping not found at '{PROCESSED_MANIFEST_PATH}'")
            return

        logger.info("Loading processed Bungie manifest database maps into memory...")
        with open(PROCESSED_MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        self.perk_map = manifest_data.get("perks", {})

        # Build a lowercase lookup for case-insensitive perk matching.
        self.perk_map_lower = {k.lower(): k for k in self.perk_map if isinstance(k, str)}

        # Build a diacritic-folded lookup for accent-insensitive perk matching.
        self.perk_map_folded = {}
        for k in self.perk_map:
            if isinstance(k, str):
                folded = self._fold_diacritics(k.lower())
                self.perk_map_folded[folded] = k

        # Build reverse hash->name lookup for variant matching inside weapon pools.
        # Example: spreadsheet says "Outlaw", weapon pool contains "Outlaw Refit"
        # (hash 12345). perk_map_by_hash[12345] == "Outlaw Refit" lets us
        # detect that "Outlaw" is a prefix of a valid pool perk.
        self.perk_map_by_hash = {}
        for name, hashes in self.perk_map.items():
            for h in hashes:
                self.perk_map_by_hash[h] = name

        weapons = manifest_data.get("weapons", {})
        exotic_armor = manifest_data.get("exotic_armor", {}) or {}

        # Legacy fallback: if the manifest does not contain the new "weapons"
        # and "exotic_armor" keys, treat every top-level key (except "perks")
        # as an item name pointing to a list of instances.
        if not weapons and not exotic_armor:
            for k, v in manifest_data.items():
                if k != "perks" and isinstance(v, list):
                    cleaned = self._clean(k)
                    if cleaned in self.item_map:
                        self.item_map[cleaned].extend(v)
                    else:
                        self.item_map[cleaned] = v
        else:
            # New format: iterate both weapons and exotic_armor into the same
            # lookup table. Exotic armor is included because some wishlists
            # (e.g., Aegis endgame) contain exotic armor recommendations.
            for name, item_instances in itertools.chain(weapons.items(), exotic_armor.items()):
                cleaned = self._clean(name)
                if cleaned in self.item_map:
                    # Extend rather than overwrite. This fixes the Adept/normal
                    # collision bug where two different hashes share the same
                    # cleaned name (e.g., "Palindrome" and "Palindrome (Adept)"
                    # both clean to "palindrome").
                    self.item_map[cleaned].extend(item_instances)
                else:
                    self.item_map[cleaned] = item_instances

    def load_pipeline_state(self):
        """
        Load the shared JSON state file that tracks which scraped files need
        wishlist regeneration.

        Returns an empty dict if the file is missing or unreadable. An empty
        dict triggers a conservative "run everything" behavior.
        """
        if not os.path.exists(STATE_FILE):
            logger.warning(f"State tracking file missing at '{STATE_FILE}'. Will run safely without skip logic.")
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load pipeline state ledger: {e}")
            return {}

    def save_pipeline_state(self, state):
        """
        Atomically(ish) write the state dict back to disk via pipeline_utils.
        """
        try:
            save_json_file(STATE_FILE, state)
            logger.info(f"State tracker synchronized. Flags committed to file: '{STATE_FILE}'")
        except Exception as e:
            logger.error(f"Failed to synchronize state machine progress flags: {e}")

    def find_file_state_block(self, state, filename_key):
        """
        Locate the state sub-dict for a given scraped JSON filename.

        The state file uses nested keys that may not exactly match the filename.
        We try several key variants:
            1. Exact filename_key.
            2. filename_key with "_spreadsheet_data_scraped" stripped.
            3. filename_key with "_data_scraped" stripped.
            4. Any of the above inside state["spreadsheets"].

        Returns:
            (block_dict, matched_key, parent_node_name) or (None, filename_key, None)

        NOTE: matched_key is for READ-ONLY lookups (checking flags). For any
        state MUTATIONS (setting/clearing flags), always use clean_short_name
        derived from filename_key so that both wishlist_update_required and
        wishlist_split_required live on the same canonical spreadsheet key.
        """
        possible_keys = [
            filename_key,
            filename_key.replace("_spreadsheet_data_scraped", ""),
            filename_key.replace("_data_scraped", "")
        ]

        # Try top-level keys first.
        for pk in possible_keys:
            if pk in state and isinstance(state[pk], dict):
                return state[pk], pk, None

        # Try nested under state["spreadsheets"].
        if "spreadsheets" in state and isinstance(state["spreadsheets"], dict):
            for pk in possible_keys:
                if pk in state["spreadsheets"] and isinstance(state["spreadsheets"][pk], dict):
                    return state["spreadsheets"][pk], pk, "spreadsheets"

        return None, filename_key, None

    def resolve_tags(self, spreadsheet_key, workbook_name=None):
        """
        Read tags from config.yaml for attribution in the wishlist header.

        Hierarchy:
            - Spreadsheet-level tags are the default.
            - Workbook-level tags override if non-empty.

        Tags appear in the wishlist as "//notes: ... | tags: <tags>" so DIM
        users can see which community sheet recommended each roll.
        """
        config = load_config()
        ss_config = config.get("spreadsheets", {}).get(spreadsheet_key, {})
        ss_tags = ss_config.get("tags", "")

        if workbook_name:
            for wb in ss_config.get("workbooks", []):
                if wb.get("name") == workbook_name:
                    wb_tags = wb.get("tags", "")
                    return wb_tags if wb_tags else ss_tags

        return ss_tags

    def generate_item_wishlist(self, item_name, roll_data, tags=""):
        """
        Convert a single weapon's scraped roll data into DIM wishlist lines.

        Algorithm overview:
            1. Look up the weapon in item_map by cleaned name.
            2. Gather metadata (rank, tier, notes, analysis scores) into a
               human-readable info string.
            3. Resolve each perk name to a list of hashes via perk_map.
               Try exact match -> case-insensitive -> diacritic-folded.
            4. For every item instance (hash) of this weapon, intersect the
               perk hashes with the instance's valid_perks list.
            5. If no intersection, attempt variant matching (e.g., "Outlaw"
               prefix-matches "Outlaw Refit" in the weapon's pool).
            6. Generate the Cartesian product of all valid perk hash buckets.
            7. Filter combos where any hash is outside the instance's pool,
               unless the weapon is in ignore_manifest_perk_pool.
            8. Emit one dimwishlist: line per surviving combo.

        Returns:
            dict with keys {"item_name", "info", "tags", "lines"} on success,
            or None if zero lines were generated (with diagnostics logged).
        """
        cleaned_item_name = self._clean(item_name)

        # Guard: weapon not found in manifest at all.
        if cleaned_item_name not in self.item_map:
            details_logger.warning(f"FINAL RESULT: Item '{item_name}' yielded 0 lines.")
            details_logger.warning(f"  Reason: Item not found in manifest (cleaned name: '{cleaned_item_name}')")
            return None

        # A weapon may have multiple instances (different hashes for different
        # versions: normal, Adept, Timelost, crafted, etc.).
        item_instances = self.item_map[cleaned_item_name]

        # ------------------------------------------------------------------
        # Assemble metadata info string
        # ------------------------------------------------------------------
        info_block = roll_data.get("info", {})

        # Flatten multi-line cells into comma-separated prose so the DIM
        # notes field stays single-line and diff-friendly.
        rank = info_block.get("rank", "").replace("\n", ", ").strip()
        tier = info_block.get("tier", "").replace("\n", ", ").strip()
        priority = info_block.get("priority", "").replace("\n", ", ").strip()
        role = info_block.get("role", "").replace("\n", ", ").strip()
        purpose = info_block.get("purpose", "").replace("\n", ", ").strip()
        item_tags = info_block.get("tags", "").replace("\n", ", ").strip()
        item_type = info_block.get("type", "").replace("\n", ", ").strip()
        usage = info_block.get("usage", "").replace("\n", ", ").strip()
        source = info_block.get("source", "").replace("\n", ", ").strip()
        notes = info_block.get("notes", "").replace("\n", ", ").strip()
        description = info_block.get("description", "").replace("\n", ", ").strip()
        alternatives = info_block.get("alternatives", "").replace("\n", ", ").strip()

        info_parts = []
        if rank: info_parts.append(f"[Rank]: {rank}")
        if tier: info_parts.append(f"[Tier]: {tier}")
        if priority: info_parts.append(f"[Priority]: {priority}")
        if role: info_parts.append(f"[Role]: {role}")
        if purpose: info_parts.append(f"[Purpose]: {purpose}")
        if item_tags: info_parts.append(f"[Tags]: {item_tags}")
        if item_type: info_parts.append(f"[Type]: {item_type}")
        if usage: info_parts.append(f"[Usage]: {usage}")
        if source: info_parts.append(f"[Source]: {source}")
        if notes: info_parts.append(f"[Notes]: {notes}")
        if description: info_parts.append(f"[Description]: {description}")
        if alternatives: info_parts.append(f"[Alternatives]: {alternatives}")

        # Append analysis scores (roam, dps, day1, chall, speed, effect, flex, power).
        analysis = info_block.get("analysis", {})
        if analysis:
            for key in ["roam", "dps", "day1", "chall", "speed", "effect", "flex", "power"]:
                val = analysis.get(key, "").strip()
                if val:
                    label = key.upper() if key in ("dps", "day1") else key.capitalize()
                    info_parts.append(f"[{label}]: {val}")

        info_string = " / ".join(info_parts)

        # ------------------------------------------------------------------
        # Resolve perk names to canonical names
        # ------------------------------------------------------------------
        perks_dict = roll_data.get("perks", {})
        # DIM wishlist format uses four perk slots: column1, column2, perk1, perk2.
        # Origin trait is currently excluded from the wishlist line because DIM
        # does not support origin-trait filtering in the dimwishlist: format.
        slots_to_process = ["column1", "column2", "perk1", "perk2"]

        # Diagnostic accumulators for the 0-line failure report.
        missing_perks = []      # [(slot, perk_name)] — perk not found in manifest at all.
        pool_rejected = []      # [(slot, perk_name, hash)] — perk exists globally but not in any weapon pool.
        rejected_combos = 0     # Count of combos rejected by per-instance pool validation.

        perk_name_buckets = []
        for slot in slots_to_process:
            perk_selections = perks_dict.get(slot, [])
            names = []
            for perk_name in perk_selections:
                cleaned_perk = perk_name.strip()

                # Try exact match first.
                if cleaned_perk in self.perk_map:
                    names.append(cleaned_perk)
                # Fall back to case-insensitive match.
                elif cleaned_perk.lower() in self.perk_map_lower:
                    canonical_name = self.perk_map_lower[cleaned_perk.lower()]
                    names.append(canonical_name)
                else:
                    # Fall back to diacritic-insensitive match.
                    folded = self._fold_diacritics(cleaned_perk.lower())
                    if folded in self.perk_map_folded:
                        canonical_name = self.perk_map_folded[folded]
                        names.append(canonical_name)
                    else:
                        # Perk is completely unknown. Log and skip.
                        missing_perks.append((slot, perk_name))
                        details_logger.warning(f"PERK MISSING: '{perk_name}' on '{item_name}'")

            if names:
                perk_name_buckets.append((slot, names))

        generated_lines = []
        rejected_lines = 0

        # Check if this item should bypass manifest perk validation.
        pool_bypass = cleaned_item_name in self.ignore_manifest_perk_pool
        if pool_bypass:
            details_logger.info(f"POOL BYPASS: '{item_name}' using global manifest hashes, ignoring weapon perk pool.")

        # =================================================================
        # GLOBAL PERK POOL CHECK FOR VARIANT MATCHING ELIGIBILITY
        # =================================================================
        # Build the union of ALL valid perks across every instance of this weapon.
        # If a perk exists in at least one instance's pool, we use it as-is and
        # NEVER do variant matching. Variant matching is only for typos where
        # the perk is completely absent from ALL instances.
        all_valid_perks_union = set()
        for inst in item_instances:
            all_valid_perks_union.update(inst.get("valid_perks", []))

        # Pre-check: which perks exist globally but not in ANY instance's pool?
        # These are the ONLY candidates eligible for variant matching.
        globally_missing_perks = set()
        for slot, name_bucket in perk_name_buckets:
            for perk_name in name_bucket:
                possible_hashes = self.perk_map.get(perk_name, [])
                if possible_hashes and not any(h in all_valid_perks_union for h in possible_hashes):
                    globally_missing_perks.add(perk_name)
                    for h in possible_hashes:
                        pool_rejected.append((slot, perk_name, h))

        # ------------------------------------------------------------------
        # Per-instance combo generation
        # ------------------------------------------------------------------
        for instance in item_instances:
            item_id = instance["item_id"]
            valid_manifest_perks = set(instance.get("valid_perks", []))

            # Convert canonical perk names to hashes, filtered by this instance's pool.
            perk_hash_buckets = []
            for slot, name_bucket in perk_name_buckets:
                hash_bucket = []
                for perk_name in name_bucket:
                    possible_hashes = self.perk_map.get(perk_name, [])
                    if pool_bypass:
                        valid_hashes = possible_hashes
                    else:
                        valid_hashes = [h for h in possible_hashes if h in valid_manifest_perks]

                    if valid_hashes:
                        hash_bucket.extend(valid_hashes)
                    else:
                        # =================================================================
                        # VARIANT MATCHING WITH GLOBAL POOL GUARD
                        # =================================================================
                        # Only attempt variant matching if this perk is missing from
                        # ALL instances of this weapon. If it exists in even one
                        # instance, we skip variant matching because the spreadsheet
                        # intended that specific perk for a specific version.
                        fallback_hashes = []

                        if perk_name in globally_missing_perks:
                            search_tokens = self._tokenize(perk_name)

                            for vp_hash in valid_manifest_perks:
                                vp_name = self.perk_map_by_hash.get(vp_hash)
                                if not vp_name:
                                    continue

                                vp_tokens = self._tokenize(vp_name)

                                if search_tokens and search_tokens <= vp_tokens:
                                    fallback_hashes.append(vp_hash)

                            if fallback_hashes:
                                hash_bucket.extend(fallback_hashes)
                                matched_names = [self.perk_map_by_hash.get(h) for h in fallback_hashes]
                                details_logger.info(f"VARIANT MATCH: '{perk_name}' -> {matched_names} on '{item_name}'")
                        elif possible_hashes:
                            # Perk exists globally and in some instance, just not THIS one.
                            # Include the global hash; it will be filtered by pool validation.
                            hash_bucket.extend(possible_hashes)

                if hash_bucket:
                    perk_hash_buckets.append(hash_bucket)

            # Cartesian product: every combination of one hash from each slot.
            combinations = list(itertools.product(*perk_hash_buckets)) if perk_hash_buckets else [()]

            for combo in combinations:
                # Pool validation: every hash in the combo must belong to this
                # instance's valid_perks set, unless pool_bypass is enabled.
                if not pool_bypass and valid_manifest_perks and combo and not all(h in valid_manifest_perks for h in combo):
                    details_logger.info(
                        f"PERK INVALIDATION: Item '{item_name}' (ID: {item_id}) rejected for perk combo: {combo}"
                    )
                    rejected_lines += 1
                    rejected_combos += 1
                    continue

                # DIM wishlist format: dimwishlist:item=<item_id>&perks=<hash1>,<hash2>,...
                perks_str = ",".join(map(str, combo)) if combo else ""
                line = f"dimwishlist:item={item_id}&perks={perks_str}"
                generated_lines.append(line)

        # ------------------------------------------------------------------
        # 0-line diagnostic report
        # ------------------------------------------------------------------
        if not generated_lines:
            # Build a list of all item IDs for this weapon so the operator can
            # cross-reference with DIM or the Bungie API.
            item_ids = [str(inst["item_id"]) for inst in item_instances]
            id_str = ", ".join(item_ids)

            reasons = []
            if missing_perks:
                for slot, perk_name in missing_perks:
                    reasons.append(f"Perk '{perk_name}' (slot: {slot}): not found in manifest")
            if pool_rejected:
                for slot, perk_name, h in pool_rejected:
                    reasons.append(f"Perk '{perk_name}' (slot: {slot}): hash {h} not in weapon pool")
            if rejected_combos > 0:
                reasons.append(f"All {rejected_combos} combinations rejected by pool validation")
            if not reasons:
                reasons.append("No perks specified in scraped data")

            details_logger.warning(f"FINAL RESULT: Item '{item_name}' (ID: {id_str}) yielded 0 lines.")
            warning_logger.warning(f"  Reasons:")
            for reason in reasons:
                warning_logger.warning(f"    - {reason}")
            return None

        # Success: log the result and return the structured output.
        item_ids = [str(inst["item_id"]) for inst in item_instances]
        id_str = ", ".join(item_ids)
        details_logger.info(
            f"FINAL RESULT: Item '{item_name}' (ID: {id_str}) wrote {len(generated_lines)} lines, rejected {rejected_lines} lines."
        )

        return {
            "item_name": item_name,
            "info": info_string,
            "tags": tags,
            "lines": generated_lines
        }

    def execute_pipeline(self):
        """
        Main orchestrator: iterate every scraped JSON file, generate wishlist
        lines for every weapon, and write one .txt file per spreadsheet.

        State integration:
            - Reads wishlist_update_required flags from the state file.
            - If the manifest was updated (manifest_wishlist_required=True),
              forces a rebuild of every wishlist regardless of per-file flags.
            - Clears flags after successful writes.

        Output format (DIM wishlist):
            title:<Sheet Name> (updated <date>)
            description:Based on <Sheet Name> Spreadsheet (updated <date>). -> <link> <- | Autogenerated by JxP
            // Generated: <timestamp>
            //
            // <Weapon Name>
            //notes:<metadata> | tags: <tags>
            dimwishlist:item=<id>&perks=<h1>,<h2>,<h3>,<h4>
            //
            // <Next Weapon>
            ...
        """
        logger.info("=" * 80)
        logger.info("🚀 Launching Isolated DIM Wishlist Text Generation Pipeline...")
        logger.info("=" * 80)

        self._load_manifest_lookups()

        if not os.path.exists(SCRAPED_DIR):
            logger.error(f"Critical Termination: Target scraping directory layout '{SCRAPED_DIR}' does not exist.")
            return

        json_files = [f for f in os.listdir(SCRAPED_DIR) if f.endswith(".json")]
        if not json_files:
            logger.warning(f"No source profile targets found inside scraper folder '{SCRAPED_DIR}'.")
            return

        state = self.load_pipeline_state()
        state_modified = False

        # Check the global manifest wishlist flag. If the manifest was updated,
        # every wishlist is stale because perk hashes may have shifted.
        manifest_state = state.get("bungie_manifest", {})
        manifest_wishlist_required = manifest_state.get("wishlist_update_required", False)
        if manifest_wishlist_required:
            logger.info("🔄 Global manifest update detected. Forcing wishlist rebuild for all spreadsheets.")

        # ------------------------------------------------------------------
        # Iterate every scraped JSON file
        # ------------------------------------------------------------------
        for json_file in json_files:
            filename_key = json_file.replace(".json", "")

            # Locate the state block for this file.
            file_state_block, matched_key, parent_node = self.find_file_state_block(state, filename_key)

            # Derive the output filename: strip redundant suffixes and append
            # _spreadsheet_dim_wishlist.txt.
            clean_short_name = filename_key.replace("_spreadsheet_data_scraped", "").replace("_data_scraped", "")
            output_txt_name = f"{clean_short_name}_spreadsheet_dim_wishlist.txt"
            output_path = os.path.join(WISHLIST_DIR, output_txt_name)

            file_exists = os.path.exists(output_path)

            # Determine whether this file needs processing.
            update_required = False
            if file_state_block is not None:
                update_required = file_state_block.get("wishlist_update_required", False)
            else:
                logger.warning(f"Could not locate matching state block entry for '{filename_key}'. Defaulting compilation rules.")
                update_required = True

            # Global manifest update overrides per-file flags.
            if manifest_wishlist_required:
                update_required = True

            # Skip if no update is needed and the output already exists.
            if not update_required and file_exists:
                logger.info(f"Skip Condition Met: File '{json_file}' has no update required and wishlist output file already exists. Skipping.")
                continue

            # Log the reason for processing.
            if update_required:
                logger.info(f"Processing '{json_file}' (Reason: wishlist_update_required is set to true)...")
            elif not file_exists:
                logger.info(f"Processing '{json_file}' (Reason: Missing destination output file '{output_txt_name}')...")

            # ------------------------------------------------------------------
            # Load scraped data
            # ------------------------------------------------------------------
            input_path = os.path.join(SCRAPED_DIR, json_file)

            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    scraped_content = json.load(f)
            except Exception as e:
                logger.error(f"Parsing Failure: File '{json_file}' could not be loaded into standard JSON data: {e}")
                continue

            # Extract provenance metadata for the wishlist header.
            spreadsheet_meta = scraped_content.get("spreadsheet", {})
            ss_name = spreadsheet_meta.get("name", matched_key.replace("_", " ").title())
            ss_date = spreadsheet_meta.get("changelog", {}).get("date", "Unknown Date")
            ss_link = spreadsheet_meta.get("link", "")

            # The scraped content may be wrapped in a "workbooks" dict (multi-tab
            # sheets like Aegis endgame) or flat (single-tab sheets like boss-damage).
            items_pool = scraped_content.get("workbooks", scraped_content)

            # ------------------------------------------------------------------
            # Build wishlist lines
            # ------------------------------------------------------------------
            # Header block: DIM reads the title and description lines to show
            # the user which wishlist is active.
            all_compiled_lines = [
                f"title:{ss_name} (updated {ss_date})",
                f"description:Based on {ss_name} Spreadsheet (updated {ss_date}). -> {ss_link} <- | Autogenerated by JxP",
                f"// Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ""
            ]

            lines_written_for_file = 0

            # Multi-workbook layout (e.g., Aegis endgame with 20+ tabs).
            if "workbooks" in scraped_content:
                for workbook_name, weapons_dict in items_pool.items():
                    workbook_logger.info(f"Parsing nested ledger tab: '{workbook_name}'")
                    tags = self.resolve_tags(matched_key, workbook_name)
                    for weapon_name, weapon_details in weapons_dict.items():
                        real_name = weapon_details.get("weapon_name", weapon_name)
                        result = self.generate_item_wishlist(real_name, weapon_details, tags=tags)
                        if result:
                            # DIM comment block: weapon name, notes, then lines.
                            all_compiled_lines.append(f"// {result['item_name']}")
                            tag_suffix = f" | tags: {result['tags']}" if result.get('tags') else " | tags: "
                            all_compiled_lines.append(f"//notes:{result['info']}{tag_suffix}")
                            all_compiled_lines.extend(result["lines"])
                            all_compiled_lines.append("")
                            lines_written_for_file += len(result["lines"])

            # Flat layout (e.g., Aegis boss-damage with a single equipment tab).
            else:
                tags = self.resolve_tags(matched_key)
                for weapon_name, weapon_details in items_pool.items():
                    # Skip the "spreadsheet" metadata key if it leaked into the
                    # flat dict (defensive; should not happen with current scrapers).
                    if weapon_name == "spreadsheet":
                        continue
                    real_name = weapon_details.get("weapon_name", weapon_name)
                    result = self.generate_item_wishlist(real_name, weapon_details, tags=tags)
                    if result:
                        all_compiled_lines.append(f"// {result['item_name']}")
                        tag_suffix = f" | tags: {result['tags']}" if result.get('tags') else " | tags: "
                        all_compiled_lines.append(f"//notes:{result['info']}{tag_suffix}")
                        all_compiled_lines.extend(result["lines"])
                        all_compiled_lines.append("")
                        lines_written_for_file += len(result["lines"])

            # ------------------------------------------------------------------
            # Write output and update state
            # ------------------------------------------------------------------
            if lines_written_for_file > 0:
                with open(output_path, "w", encoding="utf-8") as out_f:
                    out_f.write("\n".join(all_compiled_lines) + "\n")
                workbook_logger.info(
                    f"🎉 Wishlist file isolated and successfully written: '{output_path}' ({lines_written_for_file} lines)"
                )

                # ------------------------------------------------------------------
                # SET SPLITTER FLAG: Signal the downstream splitter that this
                # source has fresh data and should be re-split.
                # ------------------------------------------------------------------
                # The splitter reads this flag from the same state file. If True,
                # it processes this source and clears the flag. If False/missing,
                # it skips the source entirely.
                # We use clean_short_name as the key because that matches the
                # config_source_spreadsheet values in the splitter's YAML config.
                from pipeline_utils import ensure_spreadsheet_state
                ensure_spreadsheet_state(state, clean_short_name)
                state["spreadsheets"][clean_short_name]["wishlist_split_required"] = True
                details_logger.info(f"  🏷️  Flagged '{clean_short_name}' for splitter (wishlist_split_required = True)")
                state_modified = True

                # Clear the wishlist_update_required flag for this file.
                # We use clean_short_name (not matched_key) to ensure the flag
                # is cleared at the same key path where the splitter will later
                # read wishlist_split_required.
                ensure_spreadsheet_state(state, clean_short_name)
                state["spreadsheets"][clean_short_name]["wishlist_update_required"] = False

                state_modified = True

                # Verify the file was actually written
                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    logger.error(f"File write verification failed for '{output_path}'")
                    continue
            else:
                logger.warning(f"No valid lines could be computed or verified for file setup template: '{json_file}'")

        # ------------------------------------------------------------------
        # Final state commit
        # ------------------------------------------------------------------
        if manifest_wishlist_required:
            manifest_state["wishlist_update_required"] = False
            state_modified = True
            logger.info("✅ Global manifest wishlist rebuild flag cleared.")

        if state_modified:
            self.save_pipeline_state(state)
        else:
            logger.info("Pipeline execution completed. No state changes needed updating.")

if __name__ == "__main__":
    setup_root_console_logging()
    generator = DIMWishlistGenerator()
    generator.execute_pipeline()
