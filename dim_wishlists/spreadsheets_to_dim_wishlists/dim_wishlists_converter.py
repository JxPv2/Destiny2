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
    CONFIG_FILE,
)

# =============================================================================
# SECTION 1: BOOTSTRAP PATHS
# =============================================================================
SYSTEM_PATHS = bootstrap_system_paths()
STATE_FILE = SYSTEM_PATHS["state_file"]
LOG_DIR = SYSTEM_PATHS["log_dir"]
MANIFEST_DIR = SYSTEM_PATHS["manifest_dir"]
SCRAPED_DIR = SYSTEM_PATHS["scraped_dir"]
WISHLIST_DIR = SYSTEM_PATHS["wishlist_dir"]

PROCESSED_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "bungie_manifest_processed.json")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(WISHLIST_DIR, exist_ok=True)

# =============================================================================
# SECTION 2: LOGGING
# =============================================================================
LOG_LAYOUT = "%(asctime)s [%(levelname)s] -> %(message)s"
custom_formatter = PipelineIndentedFormatter(fmt=LOG_LAYOUT)

# =============================================================================
# FILTER: Duplicate specific INFO records to warnings log for context
# =============================================================================
class DuplicateInfoFilter(logging.Filter):
    """
    Duplicates specific INFO records to a secondary handler.
    Passes through WARNING+ records normally.
    """
    def __init__(self, target_handler, keywords=None):
        super().__init__()
        self.target_handler = target_handler
        self.keywords = keywords or []

    def filter(self, record):
        # Always pass through to primary handler (return True)
        # But also emit to target if it's an INFO with matching keywords
        if record.levelno == logging.INFO:
            msg_lower = record.getMessage().lower()
            if any(kw in msg_lower for kw in self.keywords):
                self.target_handler.handle(record)
        return True  # Don't block primary handler

log_name = os.path.splitext(os.path.basename(__file__))[0] + ".log"
file_handler = logging.FileHandler(os.path.join(LOG_DIR, log_name), encoding="utf-8")
file_handler.setFormatter(custom_formatter)
file_handler.setLevel(logging.INFO)

warnings_log_name = os.path.splitext(os.path.basename(__file__))[0] + "_warnings.log"
warnings_handler = logging.FileHandler(os.path.join(LOG_DIR, warnings_log_name), encoding="utf-8")
warnings_handler.setFormatter(custom_formatter)
warnings_handler.setLevel(logging.WARNING)

# Add filter to duplicate context INFO lines to warnings log
dup_filter = DuplicateInfoFilter(
    warnings_handler,
    keywords=["launching", "processing", "parsing nested", "================================================================================"]
)
file_handler.addFilter(dup_filter)

logger = logging.getLogger("WishlistGenerator")
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

logger.addHandler(file_handler)
logger.addHandler(warnings_handler)

# LoggerAdapters for hierarchical indentation
class IndentAdapter(logging.LoggerAdapter):
    def __init__(self, logger, indent_level):
        super().__init__(logger, {})
        self.indent_level = indent_level

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["indent"] = self.indent_level
        return msg, kwargs

workbook_logger = IndentAdapter(logger, 2)   # 2 spaces
details_logger = IndentAdapter(logger, 3)    # 4 spaces
warning_logger = IndentAdapter(logger, 4)    # 6 spaces

# =============================================================================
# SECTION 3: CORE COMPILATION AND COMBINATORICS ENGINE
# =============================================================================
class DIMWishlistGenerator:
    def __init__(self):
        self.perk_map = {}
        self.item_map = {}
        self.ignore_manifest_perk_pool = self._load_ignore_manifest_perk_pool()

    def _load_ignore_manifest_perk_pool(self):
        """Loads the list of items that bypass manifest perk validation."""
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            items = config.get("ignore_manifest_perk_pool", [])
            # Normalize to lowercase for case-insensitive matching
            return {item.strip().lower() for item in items if isinstance(item, str)}
        except Exception as e:
            logger.warning(f"Could not load ignore_manifest_perk_pool from config: {e}")
            return set()

    def _clean(self, text):
        if not text:
            return ""
        return text.strip().lower().replace(" (adept)", "").replace("(adept)", "")

    def _fold_diacritics(self, text):
        if not text:
            return ""
        # NFD splits "ä" into "a" + combining diaeresis; we drop the combining marks
        decomposed = unicodedata.normalize('NFD', text)
        return ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn')

    def _load_manifest_lookups(self):
        if not os.path.exists(PROCESSED_MANIFEST_PATH):
            logger.critical(f"Critical execution block: Manifest mapping not found at '{PROCESSED_MANIFEST_PATH}'")
            return

        logger.info("Loading processed Bungie manifest database maps into memory...")
        with open(PROCESSED_MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        self.perk_map = manifest_data.get("perks", {})
        # Build a lowercase lookup for case-insensitive perk matching
        self.perk_map_lower = {k.lower(): k for k in self.perk_map if isinstance(k, str)}
        # Build a diacritic-folded lookup for accent-insensitive perk matching
        self.perk_map_folded = {}
        for k in self.perk_map:
            if isinstance(k, str):
                folded = self._fold_diacritics(k.lower())
                self.perk_map_folded[folded] = k
        # Build reverse hash->name lookup for variant matching inside weapon pools
        self.perk_map_by_hash = {}
        for name, hashes in self.perk_map.items():
            for h in hashes:
                self.perk_map_by_hash[h] = name

        weapons = manifest_data.get("weapons", {})
        exotic_armor = manifest_data.get("exotic_armor", {}) or {}

        if not weapons and not exotic_armor:
            for k, v in manifest_data.items():
                if k != "perks" and isinstance(v, list):
                    cleaned = self._clean(k)
                    if cleaned in self.item_map:
                        self.item_map[cleaned].extend(v)
                    else:
                        self.item_map[cleaned] = v
        else:
            for name, item_instances in itertools.chain(weapons.items(), exotic_armor.items()):
                cleaned = self._clean(name)
                if cleaned in self.item_map:
                    self.item_map[cleaned].extend(item_instances)
                else:
                    self.item_map[cleaned] = item_instances

    def load_pipeline_state(self):
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
        try:
            save_json_file(STATE_FILE, state)
            logger.info(f"State tracker synchronized. Flags committed to file: '{STATE_FILE}'")
        except Exception as e:
            logger.error(f"Failed to synchronize state machine progress flags: {e}")

    def find_file_state_block(self, state, filename_key):
        possible_keys = [
            filename_key,
            filename_key.replace("_spreadsheet_data_scraped", ""),
            filename_key.replace("_data_scraped", "")
        ]

        for pk in possible_keys:
            if pk in state and isinstance(state[pk], dict):
                return state[pk], pk, None

        if "spreadsheets" in state and isinstance(state["spreadsheets"], dict):
            for pk in possible_keys:
                if pk in state["spreadsheets"] and isinstance(state["spreadsheets"][pk], dict):
                    return state["spreadsheets"][pk], pk, "spreadsheets"

        return None, filename_key, None

    def resolve_tags(self, spreadsheet_key, workbook_name=None):
        """
        Reads tags from config.yaml.
        Spreadsheet-level tags are default.
        Workbook-level tags override if non-empty.
        """
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            return ""

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
        Maps flat text selections to explicit database identifiers.
        Returns a dict with 'lines' and 'metadata' for the new output format.
        """
        cleaned_item_name = self._clean(item_name)

        if cleaned_item_name not in self.item_map:
            details_logger.warning(f"FINAL RESULT: Item '{item_name}' yielded 0 lines.")
            details_logger.warning(f"  Reason: Item not found in manifest (cleaned name: '{cleaned_item_name}')")
            return None

        item_instances = self.item_map[cleaned_item_name]

        info_block = roll_data.get("info", {})
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

        # Add analysis fields (roam, dps, day1, chall, speed, effect, flex, power)
        analysis = info_block.get("analysis", {})
        if analysis:
            for key in ["roam", "dps", "day1", "chall", "speed", "effect", "flex", "power"]:
                val = analysis.get(key, "").strip()
                if val:
                    label = key.upper() if key in ("dps", "day1") else key.capitalize()
                    info_parts.append(f"[{label}]: {val}")

        info_string = " / ".join(info_parts)

        perks_dict = roll_data.get("perks", {})
        # , "origin_trait"
        slots_to_process = ["column1", "column2", "perk1", "perk2"]

        # Diagnostic tracking for 0-line cases
        missing_perks = []      # [(slot, perk_name)]
        pool_rejected = []      # [(slot, perk_name, hash)]
        rejected_combos = 0

        perk_name_buckets = []
        for slot in slots_to_process:
            perk_selections = perks_dict.get(slot, [])
            names = []
            for perk_name in perk_selections:
                cleaned_perk = perk_name.strip()
                if cleaned_perk in self.perk_map:
                    names.append(cleaned_perk)
                elif cleaned_perk.lower() in self.perk_map_lower:
                    canonical_name = self.perk_map_lower[cleaned_perk.lower()]
                    names.append(canonical_name)
                else:
                    folded = self._fold_diacritics(cleaned_perk.lower())
                    if folded in self.perk_map_folded:
                        canonical_name = self.perk_map_folded[folded]
                        names.append(canonical_name)
                    else:
                        missing_perks.append((slot, perk_name))
                        details_logger.warning(f"PERK MISSING: '{perk_name}' on '{item_name}'")

            if names:
                perk_name_buckets.append((slot, names))

        generated_lines = []
        rejected_lines = 0

        # Check if this item should bypass manifest perk validation
        pool_bypass = cleaned_item_name in self.ignore_manifest_perk_pool
        if pool_bypass:
            details_logger.info(f"POOL BYPASS: '{item_name}' using global manifest hashes, ignoring weapon perk pool.")

        # Pre-check: which perks exist globally but not in ANY instance's pool
        all_valid_perks = set()
        for inst in item_instances:
            all_valid_perks.update(inst.get("valid_perks", []))

        for slot, name_bucket in perk_name_buckets:
            for perk_name in name_bucket:
                possible_hashes = self.perk_map.get(perk_name, [])
                if possible_hashes and not any(h in all_valid_perks for h in possible_hashes):
                    # Perk exists in manifest but not in any pool for this weapon
                    for h in possible_hashes:
                        pool_rejected.append((slot, perk_name, h))

        for instance in item_instances:
            item_id = instance["item_id"]
            valid_manifest_perks = set(instance.get("valid_perks", []))

            perk_hash_buckets = []
            for slot, name_bucket in perk_name_buckets:
                hash_bucket = []
                for perk_name in name_bucket:
                    possible_hashes = self.perk_map.get(perk_name, [])
                    valid_hashes = [h for h in possible_hashes if h in valid_manifest_perks]
                    if valid_hashes:
                        hash_bucket.extend(valid_hashes)
                    else:
                        # Fallback: search the weapon's valid perk pool for variant names
                        fallback_hashes = []
                        perk_name_lower = perk_name.lower()
                        for vp_hash in valid_manifest_perks:
                            vp_name = self.perk_map_by_hash.get(vp_hash)
                            if vp_name:
                                vp_name_lower = vp_name.lower()
                                if (vp_name_lower == perk_name_lower or 
                                    (vp_name_lower.startswith(perk_name_lower) and 
                                     len(vp_name_lower) > len(perk_name_lower) and 
                                     vp_name_lower[len(perk_name_lower)] in " (")):
                                    fallback_hashes.append(vp_hash)
                        if fallback_hashes:
                            hash_bucket.extend(fallback_hashes)
                            matched_names = [self.perk_map_by_hash.get(h) for h in fallback_hashes]
                            details_logger.info(f"VARIANT MATCH: '{perk_name}' -> {matched_names} on '{item_name}'")
                        elif possible_hashes:
                            hash_bucket.extend(possible_hashes)

                if hash_bucket:
                    perk_hash_buckets.append(hash_bucket)

            combinations = list(itertools.product(*perk_hash_buckets)) if perk_hash_buckets else [()]

            for combo in combinations:
                if not pool_bypass and valid_manifest_perks and combo and not all(h in valid_manifest_perks for h in combo):
                    details_logger.info(f"PERK INVALIDATION: Item '{item_name}' (ID: {item_id}) rejected for perk combo: {combo}")
                    rejected_lines += 1
                    rejected_combos += 1
                    continue

                perks_str = ",".join(map(str, combo)) if combo else ""
                line = f"dimwishlist:item={item_id}&perks={perks_str}"
                generated_lines.append(line)

        if not generated_lines:
            # Build diagnostic report with all item IDs for this weapon
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

        item_ids = [str(inst["item_id"]) for inst in item_instances]
        id_str = ", ".join(item_ids)
        details_logger.info(f"FINAL RESULT: Item '{item_name}' (ID: {id_str}) wrote {len(generated_lines)} lines, rejected {rejected_lines} lines.")

        return {
            "item_name": item_name,
            "info": info_string,
            "tags": tags,
            "lines": generated_lines
        }

    def execute_pipeline(self):
        logger.info("================================================================================")
        logger.info("🚀 Launching Isolated DIM Wishlist Text Generation Pipeline...")
        logger.info("================================================================================")

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

        manifest_state = state.get("bungie_manifest", {})
        manifest_wishlist_required = manifest_state.get("wishlist_update_required", False)
        if manifest_wishlist_required:
            logger.info("🔄 Global manifest update detected. Forcing wishlist rebuild for all spreadsheets.")

        for json_file in json_files:
            filename_key = json_file.replace(".json", "")

            file_state_block, matched_key, parent_node = self.find_file_state_block(state, filename_key)

            clean_short_name = filename_key.replace("_spreadsheet_data_scraped", "").replace("_data_scraped", "")
            output_txt_name = f"{clean_short_name}_spreadsheet_dim_wishlist.txt"
            output_path = os.path.join(WISHLIST_DIR, output_txt_name)

            file_exists = os.path.exists(output_path)

            update_required = False
            if file_state_block is not None:
                update_required = file_state_block.get("wishlist_update_required", False)
            else:
                logger.warning(f"Could not locate matching state block entry for '{filename_key}'. Defaulting compilation rules.")
                update_required = True

            if manifest_wishlist_required:
                update_required = True

            if not update_required and file_exists:
                logger.info(f"Skip Condition Met: File '{json_file}' has no update required and wishlist output file already exists. Skipping.")
                continue

            if update_required:
                logger.info(f"Processing '{json_file}' (Reason: wishlist_update_required is set to true)...")
            elif not file_exists:
                logger.info(f"Processing '{json_file}' (Reason: Missing destination output file '{output_txt_name}')...")

            input_path = os.path.join(SCRAPED_DIR, json_file)

            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    scraped_content = json.load(f)
            except Exception as e:
                logger.error(f"Parsing Failure: File '{json_file}' could not be loaded into standard JSON data: {e}")
                continue

            spreadsheet_meta = scraped_content.get("spreadsheet", {})
            ss_name = spreadsheet_meta.get("name", matched_key.replace("_", " ").title())
            ss_date = spreadsheet_meta.get("changelog", {}).get("date", "Unknown Date")
            ss_link = spreadsheet_meta.get("link", "")

            items_pool = scraped_content.get("workbooks", scraped_content)

            all_compiled_lines = [
                f"title:{ss_name} (updated {ss_date})",
                f"description:Based on {ss_name} Spreadsheet (updated {ss_date}). -> {ss_link} <- | Autogenerated by JxP",
                f"// Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ""
            ]

            lines_written_for_file = 0

            if "workbooks" in scraped_content:
                for workbook_name, weapons_dict in items_pool.items():
                    workbook_logger.info(f"Parsing nested ledger tab: '{workbook_name}'")
                    tags = self.resolve_tags(matched_key, workbook_name)
                    for weapon_name, weapon_details in weapons_dict.items():
                        result = self.generate_item_wishlist(weapon_name, weapon_details, tags=tags)
                        if result:
                            all_compiled_lines.append(f"// {result['item_name']}")
                            tag_suffix = f" | tags: {result['tags']}" if result.get('tags') else " | tags: "
                            all_compiled_lines.append(f"//notes:{result['info']}{tag_suffix}")
                            all_compiled_lines.extend(result["lines"])
                            all_compiled_lines.append("")
                            lines_written_for_file += len(result["lines"])
            else:
                tags = self.resolve_tags(matched_key)
                for weapon_name, weapon_details in items_pool.items():
                    if weapon_name == "spreadsheet":
                        continue
                    result = self.generate_item_wishlist(weapon_name, weapon_details, tags=tags)
                    if result:
                        all_compiled_lines.append(f"// {result['item_name']}")
                        tag_suffix = f" | tags: {result['tags']}" if result.get('tags') else " | tags: "
                        all_compiled_lines.append(f"//notes:{result['info']}{tag_suffix}")
                        all_compiled_lines.extend(result["lines"])
                        all_compiled_lines.append("")
                        lines_written_for_file += len(result["lines"])

            if lines_written_for_file > 0:
                with open(output_path, "w", encoding="utf-8") as out_f:
                    out_f.write("\n".join(all_compiled_lines) + "\n")
                workbook_logger.info(f"🎉 Wishlist file isolated and successfully written: '{output_path}' ({lines_written_for_file} lines)")

                if parent_node == "spreadsheets":
                    state["spreadsheets"][matched_key]["wishlist_update_required"] = False
                elif matched_key in state and isinstance(state[matched_key], dict):
                    state[matched_key]["wishlist_update_required"] = False
                else:
                    if "spreadsheets" not in state:
                        state["spreadsheets"] = {}
                    if matched_key not in state["spreadsheets"]:
                        state["spreadsheets"][matched_key] = {}
                    state["spreadsheets"][matched_key]["wishlist_update_required"] = False

                state_modified = True
            else:
                logger.warning(f"No valid lines could be computed or verified for file setup template: '{json_file}'")

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