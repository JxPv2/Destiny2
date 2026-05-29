# =============================================================================
# D2-Stuff — Auto-generated DIM Wishlist Splitter
# Copyright (C) 2026 JxPv2
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# =============================================================================
# OVERVIEW
# =============================================================================
#
# This script reads a compiled DIM wishlist .txt file (produced by the
# dim_wishlists_converter stage) alongside its scraped JSON metadata, then
# produces filtered ("split") wishlists based on declarative rules defined in
# a YAML configuration file.
#
# Each "output" block in the config becomes one .txt file in the
# splitted_wishlist_dir directory. Rules are combined with AND logic:
# a weapon must pass EVERY rule to be included in an output file.
#
# STATE-DRIVEN EXECUTION:
#   The converter sets wishlist_split_required=True in the shared state file
#   for every source it successfully updates. The splitter reads that same
#   state file and only processes sources flagged True. After processing, it
#   clears the flag. This ensures the splitter never does redundant work on
#   sources that haven't changed.
#
#   Standalone mode: If no flags are set (e.g., running splitter manually),
#   the script falls back to processing ALL sources defined in the config.
#
#   Force-all mode: --force-all bypasses state flags and processes everything.
#
# WORKFLOW:
#   1. Parse the master wishlist .txt into weapon blocks (header + rolls).
#   2. Build an index from the scraped JSON: weapon_name -> workbook + record.
#   3. For each output definition, evaluate every weapon block against its rules.
#   4. Write passing blocks to a new .txt with optional title/description overrides.
#
# =============================================================================

import os
import re
import json
import yaml
import logging
import argparse
from pathlib import Path

# pipeline_utils is a shared module across the D2-Stuff pipeline.
# It provides:
#   - bootstrap_system_paths(): Returns dict of canonical dir paths (logs, scraped, etc.)
#   - PipelineIndentedFormatter(): Custom formatter that indents continuation lines
#   - setup_root_console_logging(): Attaches a StreamHandler to the root logger
#     so all propagated module logs also appear on stdout/stderr.
from pipeline_utils import (
    bootstrap_system_paths,
    PipelineIndentedFormatter,
    setup_root_console_logging,
    save_json_file,
    setup_module_logger,
    load_config,
    IndentAdapter,
)

# =============================================================================
# BOOTSTRAP PATHS
# =============================================================================
# Resolve canonical directories via the shared utility. This ensures every
# script in the pipeline reads/writes to the same locations regardless of
# where the script is invoked from.
SYSTEM_PATHS = bootstrap_system_paths()
STATE_FILE = SYSTEM_PATHS["state_file"]
LOG_DIR = SYSTEM_PATHS["log_dir"]
SCRAPED_DIR = SYSTEM_PATHS["scraped_dir"]
WISHLIST_DIR = SYSTEM_PATHS["wishlist_dir"]
SPLIT_DIR = SYSTEM_PATHS["splitted_wishlist_dir"]

# Ensure output directories exist before any I/O happens.
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(SPLIT_DIR, exist_ok=True)

# =============================================================================
# LOGGING SETUP
# =============================================================================
# We use a named logger ("WishlistSplitter") rather than root logger so we
# can cleanly attach a dedicated FileHandler for this script. Console output
# is handled by the root logger's StreamHandler (see setup_root_console_logging
# in __main__) via propagation.
#
# Architecture: propagate=True means module logs bubble up to root, which
# prints to console. The FileHandler below captures the same logs to a file.
# This avoids duplicate handlers on the same logger object.
# =============================================================================

logger = setup_module_logger("dim_wishlists_splitter", LOG_DIR)

# Enable propagation so root console handler (set up in __main__) also emits
# these log records. This matches the pipeline-wide logging architecture.
logger.propagate = True

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
# STATE HELPERS
# =============================================================================
# The splitter shares the same state file as the converter. It only touches
# the wishlist_split_required flags under state["spreadsheets"][<<source_key>].
# =============================================================================

def _load_state():
    """
    Load the shared pipeline state JSON.

    Returns an empty dict if the file is missing or unreadable. An empty dict
    triggers fallback behavior (process all sources).
    """
    if not os.path.exists(STATE_FILE):
        logger.warning(f"State file not found at '{STATE_FILE}'. Will process ALL sources.")
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load state file '{STATE_FILE}': {e}. Processing ALL sources.")
        return {}


def _save_state(state):
    """
    Atomically(ish) write the state dict back to disk via pipeline_utils.
    """
    try:
        save_json_file(STATE_FILE, state)
        logger.info(f"State synchronized. Splitter flags committed to '{STATE_FILE}'")
    except Exception as e:
        logger.error(f"Failed to write state file '{STATE_FILE}': {e}")


# =============================================================================
# WISHLIST BLOCK MODEL
# =============================================================================
# DIM wishlists are text files where each weapon is a contiguous block:
#
#   // Austringer
#   //notes:God roll for PvP | tags:pvp,handcannon
#   dimwishlist:item=123&perks=456,789
#   dimwishlist:item=123&perks=456,790
#
# The WishlistBlock class captures one such block so we can filter, move,
# and rewrite them without touching the raw text parsing repeatedly.
# =============================================================================

class WishlistBlock:
    """
    Represents one weapon entry (one or more dimwishlist lines) in a DIM wishlist.

    Attributes:
        name (str):        Weapon name parsed from the '// WeaponName' header.
        notes (str):       Full text after '//notes:' (may contain '| tags:...').
        tags (str):        Extracted substring after '| tags:' if present.
        lines (list[str]): All 'dimwishlist:' lines belonging to this weapon.
        raw_header (str):  The original '// WeaponName' line (preserved for debugging).
    """

    def __init__(self):
        self.name = ""
        self.notes = ""
        self.tags = ""
        self.lines = []
        self.raw_header = ""

    def is_empty(self):
        """Return True if this block has no dimwishlist lines."""
        return not self.lines


def parse_wishlist_file(file_path):
    """
    Parse a DIM wishlist .txt into header lines and WishlistBlock objects.

    The DIM format is line-oriented:
      - title:... and description:... are header metadata.
      - '// Generated:' lines are also treated as headers.
      - Blank lines separate weapon blocks.
      - '// Weapon Name' starts a new block.
      - '//notes:...' attaches to the current block.
      - 'dimwishlist:...' lines are the actual roll data.

    Args:
        file_path (str): Absolute or relative path to the .txt wishlist.

    Returns:
        tuple(list[str], list[WishlistBlock]):
            - header_lines: All title/description/generated lines from the top.
            - blocks:       Parsed weapon blocks in file order.

    Note:
        Any '// ' line that is NOT '//notes:' and NOT '// Generated:' is
        treated as a weapon name header. Non-standard comments may therefore
        be misinterpreted as weapon names.
    """
    blocks = []
    header_lines = []
    current_block = None

    if not os.path.exists(file_path):
        logger.error(f"Wishlist file not found: {file_path}")
        return [], []

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            # Strip only the newline; preserve other whitespace so notes
            # and names keep their original spacing.
            line = line.rstrip("\n")

            # --- Header metadata lines (only valid at top of file) ---
            if line.startswith("title:") or line.startswith("description:") or line.startswith("// Generated:"):
                header_lines.append(line)
                continue

            # --- Blank line: commit current block if it has data ---
            if line.strip() == "":
                if current_block and not current_block.is_empty():
                    blocks.append(current_block)
                current_block = None
                continue

            # --- Weapon name header: starts a new block ---
            # Must start with '// ' but must NOT be a notes line.
            if line.startswith("// ") and not line.startswith("//notes:"):
                if current_block and not current_block.is_empty():
                    blocks.append(current_block)
                current_block = WishlistBlock()
                current_block.name = line[3:].strip()
                current_block.raw_header = line
                continue

            # --- Notes line: attaches to the current active block ---
            if line.startswith("//notes:"):
                if current_block:
                    current_block.notes = line[8:].strip()
                    # Extract tags if the notes contain a '| tags:' segment.
                    tag_match = re.search(r'\| tags:\s*(.*?)$', current_block.notes)
                    if tag_match:
                        current_block.tags = tag_match.group(1).strip()
                continue

            # --- Actual wishlist roll line ---
            if line.startswith("dimwishlist:"):
                if current_block:
                    current_block.lines.append(line)
                continue

    # EOF: commit the last block if it has data.
    if current_block and not current_block.is_empty():
        blocks.append(current_block)

    return header_lines, blocks


# =============================================================================
# SCRAPED DATA INDEXER
# =============================================================================
# The scraped JSON is produced by an earlier pipeline stage that reads Google
# Sheets workbooks. Its structure is:
#
#   {
#     "workbooks": {
#       "Workbook Name": {
#         "Weapon Name": { ... scraped fields ... },
#         ...
#       }
#     }
#   }
#
# We flatten this into a single lookup: weapon_name -> {workbook, record}
# so the rule engine can quickly access metadata without re-parsing JSON.
# =============================================================================

def build_scraped_index(scraped_path):
    """
    Build a lookup dict: bare_weapon_name -> list of {workbook_name, raw_record}.

    Because the scraper deduplicates same-workbook duplicates by suffixing
    _2, _3, etc. to the dict key, we index by the bare weapon_name field
    inside the record. When a weapon appears multiple times with different
    ranks, all entries are stored under the same bare name so the rule
    engine can match the correct one by rank.

    Args:
        scraped_path (str): Path to the scraped JSON file.

    Returns:
        dict[str, list[dict]]: Index mapping bare weapon name to a list of
                               {workbook, record} entries. Empty dict if the
                               file is missing or malformed.
    """
    index = {}

    if not os.path.exists(scraped_path):
        logger.warning(f"Scraped data not found: {scraped_path}")
        return index

    with open(scraped_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    workbooks = data.get("workbooks", {})

    for wb_name, weapons in workbooks.items():
        for weapon_name, weapon_data in weapons.items():
            # Use the bare weapon_name from the record, not the suffixed dict key.
            bare_name = weapon_data.get("weapon_name", weapon_name)
            entry = {"workbook": wb_name, "record": weapon_data}
            index.setdefault(bare_name, []).append(entry)

    return index


def get_field_value(record, field_path):
    """
    Navigate a nested dictionary using dot notation.

    Args:
        record (dict):    The nested dictionary to traverse.
        field_path (str): Dot-separated path, e.g. "info.rank" or "perks.perk2".

    Returns:
        Any: The value at the path, or None if any segment is missing.
             The caller must distinguish between "field is None" and "field missing".
    """
    if not field_path:
        return None

    parts = field_path.split(".")
    current = record

    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    return current


# =============================================================================
# RULE ENGINE
# =============================================================================
# Rules are defined in the YAML config and evaluated per weapon block.
# There are two rule types:
#
#   1. WORKBOOK rule — include/exclude by source workbook (Google Sheet tab).
#   2. FILTER rule   — match a value inside the scraped JSON record.
#
# Rules are AND-combined: ALL rules must pass for a weapon to be included.
# If a rule has workbook targeting (apply_to_workbook*, etc.) and the weapon
# does NOT belong to that workbook, the rule is SKIPPED (returns True) rather
# than failing. This lets you chain per-workbook filters without accidentally
# excluding weapons from other workbooks.
# =============================================================================

class RuleEngine:
    """
    Evaluates filter rules against a weapon block + its scraped record.

    Attributes:
        rules (list[dict]):        Raw rule definitions from YAML.
        scraped_index (dict):      weapon_name -> {workbook, record} lookup.
        workbook_groups (dict):    Maps group_name -> list of workbook names.
                                   Used by apply_to_workbook_group rules.
    """

    def __init__(self, rules, scraped_index, workbook_groups=None, source_key=None):
        self.rules = rules
        self.scraped_index = scraped_index
        # workbook_groups maps group_name -> list of workbook names.
        # Passed from the top-level config so rules can reference them.
        self.workbook_groups = workbook_groups or {}
        self.source_key = source_key

    @staticmethod
    def _extract_rank_from_notes(notes):
        """Extract the [Rank] value from a notes string, or None."""
        if not notes:
            return None
        match = re.search(r'\[Rank\]:\s*([^/\[]+)', notes)
        if match:
            return match.group(1).strip()
        return None

    def _resolve_workbook_target(self, rule):
        """
        Resolve the target workbook(s) for a filter rule.

        Priority (first match wins):
          1. apply_to_workbook_group -> lookup in workbook_groups dict
          2. apply_to_workbook       -> string or list of workbook names
          3. apply_to_workbook_pattern -> regex pattern string
          4. None                    -> apply to all workbooks

        Warns if multiple targeting keys are present (ambiguous config).

        Args:
            rule (dict): A single rule definition from YAML.

        Returns:
            tuple(str, Any): (mode, target)
                mode is one of "all", "list", "pattern".
                target depends on mode:
                    "all"    -> None
                    "list"   -> list of workbook name strings
                    "pattern"-> compiled regex pattern string
        """
        group_name = rule.get("apply_to_workbook_group")
        workbook_target = rule.get("apply_to_workbook")
        pattern = rule.get("apply_to_workbook_pattern")

        # Detect ambiguous config: multiple targeting keys present.
        present_keys = []
        if group_name is not None:
            present_keys.append("apply_to_workbook_group")
        if workbook_target is not None:
            present_keys.append("apply_to_workbook")
        if pattern is not None:
            present_keys.append("apply_to_workbook_pattern")

        if len(present_keys) > 1:
            logger.warning(
                f"Ambiguous rule targeting: multiple keys present ({', '.join(present_keys)}). "
                f"Using priority order: group > workbook > pattern."
            )

        # Priority 1: workbook group
        if group_name:
            if group_name in self.workbook_groups:
                return "list", self.workbook_groups[group_name]
            else:
                logger.warning(f"Workbook group '{group_name}' not defined in config")
                return "list", []  # Empty list = matches nothing

        # Priority 2: explicit workbook(s)
        if workbook_target is not None:
            if workbook_target == "*":
                return "all", None
            if isinstance(workbook_target, str):
                return "list", [workbook_target]
            return "list", workbook_target

        # Priority 3: regex pattern
        if pattern:
            return "pattern", pattern

        # Default: apply to all workbooks
        return "all", None

    def _workbook_matches(self, workbook_name, mode, target):
        """
        Check if a weapon's source workbook matches the resolved target.

        Args:
            workbook_name (str): The workbook this weapon came from.
            mode (str):          "all", "list", or "pattern".
            target (Any):        Depends on mode (None, list[str], or regex str).

        Returns:
            bool: True if the workbook matches the target scope.
        """
        if mode == "all":
            return True
        if mode == "list":
            return workbook_name in target
        if mode == "pattern":
            return re.search(target, workbook_name) is not None
        return False

    def evaluate(self, block):
        """
        Evaluate ALL rules against a weapon block (AND logic).

        A weapon is included only if every rule returns True. If there are no
        rules, the block passes by default (vacuous truth).

        When a weapon appears multiple times in the same workbook (e.g.,
        "Gunnora's Axe" and "Gunnora's Axe_2"), the scraped index stores
        a list of entries under the bare name. We match the block to the
        correct record by comparing the rank extracted from the block's
        notes with the rank in each scraped record.

        Args:
            block (WishlistBlock): The weapon block to test.

        Returns:
            bool: True if the block passes all rules, False otherwise.
        """
        if not self.rules:
            return True

        entries = self.scraped_index.get(block.name, [])
        if not entries:
            # No scraped data for this weapon — fail all filter rules.
            return False

        # If there's only one entry, use it directly.
        if len(entries) == 1:
            record = entries[0]["record"]
            workbook_name = entries[0]["workbook"]
            for rule in self.rules:
                if not self._evaluate_single(rule, block, record, workbook_name):
                    return False
            return True

        # Multiple entries: find the one whose rank matches the block's notes.
        block_rank = self._extract_rank_from_notes(block.notes)
        if block_rank:
            for entry in entries:
                record = entry["record"]
                wb_rank = record.get("info", {}).get("rank", "")
                if str(wb_rank).strip() == block_rank:
                    workbook_name = entry["workbook"]
                    for rule in self.rules:
                        if not self._evaluate_single(rule, block, record, workbook_name):
                            return False
                    return True

        # Fallback: if no rank match, try every entry. Return True if ANY passes.
        for entry in entries:
            record = entry["record"]
            workbook_name = entry["workbook"]
            all_pass = True
            for rule in self.rules:
                if not self._evaluate_single(rule, block, record, workbook_name):
                    all_pass = False
                    break
            if all_pass:
                return True

        return False

    def _evaluate_single(self, rule, block, record, workbook_name):
        """
        Evaluate one rule against a single weapon.

        Args:
            rule (dict):           The rule definition.
            block (WishlistBlock): The weapon block.
            record (dict):         The scraped JSON record for this weapon.
            workbook_name (str):   The source workbook name.

        Returns:
            bool: True if the rule passes or is skipped, False if it rejects.
        """
        rule_type = rule.get("type")

        # ------------------------------------------------------------------
        # WORKBOOK RULE (include/exclude by workbook name)
        # ------------------------------------------------------------------
        # This rule type operates ONLY on the workbook name. It does NOT use
        # apply_to_workbook / apply_to_workbook_group / apply_to_workbook_pattern;
        # instead it uses direct 'include' and 'exclude' lists.
        #
        # IMPORTANT: include/exclude can be a single string or a list of strings.
        # We normalize to list so membership testing works correctly.
        # ------------------------------------------------------------------
        if rule_type == "workbook":
            include = rule.get("include", [])
            exclude = rule.get("exclude", [])

            # Normalize strings to single-element lists for consistent 'in' checks.
            # Without this, a string like "Exotic Weapons" would be treated as
            # a sequence of characters, causing substring false-positives.
            if isinstance(include, str):
                include = [include]
            if isinstance(exclude, str):
                exclude = [exclude]

            # Exclusion is checked first: if the weapon's workbook is in the
            # exclude list, reject immediately.
            if exclude and workbook_name in exclude:
                return False

            # If an include list is provided, the workbook MUST be in it.
            if include and workbook_name not in include:
                return False

            return True

        # ------------------------------------------------------------------
        # FILTER RULE (match scraped JSON field value)
        # ------------------------------------------------------------------
        # This rule type checks a value inside the scraped record. It supports
        # workbook targeting so the same filter can behave differently (or be
        # skipped) depending on which workbook the weapon came from.
        #
        # If the rule targets specific workbooks and this weapon does not match,
        # the rule returns True (skipped) rather than False. This is critical
        # for chaining per-workbook rules: a rule meant only for "Shopping List"
        # must not accidentally reject weapons from "Autos".
        # ------------------------------------------------------------------
        if rule_type == "filter":
            mode, target = self._resolve_workbook_target(rule)

            # If the rule targets specific workbooks and this weapon is not
            # among them, skip the rule (don't reject — other rules apply).
            if not self._workbook_matches(workbook_name, mode, target):
                return True

            field_path = rule.get("scraped_field")
            allowed_values = rule.get("values", [])

            # ------------------------------------------------------------------
            # RANK TRANSLATION: If this is a rank filter and we know the source
            # spreadsheet, translate numeric values using config.yaml rank_mappings.
            # This lets the splitter config use raw numbers (e.g., [1, 2]) instead
            # of translated strings (e.g., ["Meta-Defining", "Situational"]).
            # ------------------------------------------------------------------
            if field_path == "info.rank" and self.source_key:
                config = load_config()
                translations = config.get("rank_mappings", {}).get(self.source_key, {})
                translated = set()
                for v in allowed_values:
                    v_str = str(v)
                    translated.add(translations.get(v_str, v_str))
                allowed_values = list(translated)

            # Malformed rule: missing field path or no allowed values.
            # We treat this as "do not reject" so a typo doesn't silently
            # empty the output. Consider upgrading to a hard error in future.
            if not field_path or not allowed_values:
                logger.debug(f"Skipping malformed filter rule (missing field or values): {rule}")
                return True

            actual_value = get_field_value(record, field_path)

            # If the field value is a list (e.g. multiple perks), ANY element
            # matching an allowed value counts as a pass.
            if isinstance(actual_value, list):
                return any(
                    str(v).strip() in allowed_values
                    for v in actual_value
                    if v is not None
                )

            # Single value: convert to string for comparison since YAML values
            # are strings and JSON may contain ints/bools.
            if actual_value is not None:
                return str(actual_value).strip() in allowed_values

            # Field is missing from the record (None returned). Rule fails.
            # NOTE: If you want to match "field is missing", use a dedicated
            # sentinel or handle it at the config level; here we treat missing
            # as a rejection.
            return False

        # Unknown rule types are ignored (pass through) so the script doesn't
        # crash on experimental or future rule types.
        logger.warning(f"Unknown rule type '{rule_type}' — treating as pass")
        return True


# =============================================================================
# OUTPUT WRITER
# =============================================================================
# Writes filtered blocks back to a DIM-compatible .txt file, preserving the
# original header structure but allowing title/description overrides.
#
# Header override behavior:
#   - title / description:               Hardcoded replacement.
#   - title_suffix / description_suffix:   Injected into existing text at a
#                                          canonical position (before updated
#                                          date for title, before autogen tag
#                                          for description). If the expected
#                                          pattern is absent, the suffix is
#                                          appended instead.
# =============================================================================

def write_output(header_lines, blocks, output_path,
                 title="", title_suffix="",
                 description="", description_suffix=""):
    """
    Write filtered blocks to a DIM wishlist file with optional header overrides.

    Args:
        header_lines (list[str]):     Original title/description/generated lines.
        blocks (list[WishlistBlock]): Filtered weapon blocks to include.
        output_path (str):            Destination file path.
        title (str):                  If non-empty, replaces the original title.
        title_suffix (str):           If non-empty, injected into the original title.
        description (str):            If non-empty, replaces the original description.
        description_suffix (str):     If non-empty, injected into the original description.
    """
    lines = []
    original_title = ""
    original_description = ""

    # Extract the original title and description text so we can modify them.
    for hl in header_lines:
        if hl.startswith("title:"):
            original_title = hl[6:]  # preserve spacing after colon
        elif hl.startswith("description:"):
            original_description = hl[12:]

    # --- Title line ---
    if title:
        # Hardcoded override: use exactly what the config specified.
        lines.append(f"title:{title}")
    elif title_suffix and original_title:
        # Inject suffix BEFORE the '(updated YYYY-MM-DD)' pattern if present.
        # If the pattern is absent, append the suffix to the end.
        updated_pattern = r'(\s*\(updated\s+[^)]+\))'
        match = re.search(updated_pattern, original_title)
        if match:
            # Insert ' | {suffix}' right before the (updated ...) text.
            insert_pos = match.start()
            new_title = (
                original_title[:insert_pos]
                + f" | {title_suffix}"
                + original_title[insert_pos:]
            )
        else:
            new_title = f"{original_title} | {title_suffix}"
        lines.append(f"title:{new_title}")
    elif original_title:
        lines.append(f"title:{original_title}")
    else:
        # Fallback if the source file had no title line at all.
        lines.append(f"title:Filtered Wishlist")

    # --- Description line ---
    if description:
        lines.append(f"description:{description}")
    elif description_suffix and original_description:
        # Inject suffix BEFORE the '| Autogenerated by JxP' pattern if present.
        # If absent, append the suffix.
        auto_pattern = r'(\s*\|\s*Autogenerated by JxP\s*)$'
        match = re.search(auto_pattern, original_description)
        if match:
            insert_pos = match.start()
            new_desc = (
                original_description[:insert_pos]
                + f" | {description_suffix}"
                + original_description[insert_pos:]
            )
        else:
            new_desc = f"{original_description} | {description_suffix}"
        lines.append(f"description:{new_desc}")
    elif original_description:
        lines.append(f"description:{original_description}")
    else:
        lines.append(f"description:Filtered wishlist")

    # Preserve any '// Generated:' header lines (timestamp, version, etc.).
    for hl in header_lines:
        if hl.startswith("// Generated:"):
            lines.append(hl)

    # Blank line before first weapon block (DIM convention).
    lines.append("")

    # --- Weapon blocks ---
    for block in blocks:
        lines.append(f"// {block.name}")
        if block.notes:
            lines.append(f"//notes:{block.notes}")
        lines.extend(block.lines)
        lines.append("")  # blank line after each block

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    workbook_logger.info(f"Wrote {len(blocks)} blocks to {output_path}")


# =============================================================================
# MAIN ORCHESTRATION
# =============================================================================
# run_splitter() is the entry point that:
#   1. Loads the YAML config.
#   2. Groups output definitions by their source spreadsheet.
#   3. For each source, parses the wishlist + scraped JSON once, then runs
#      every output definition against the same parsed data.
#   4. Writes any non-empty filtered outputs to the splitted/ directory.
#
# STATE-DRIVEN SELECTIVE EXECUTION:
#   The splitter reads the shared pipeline state file to check which sources
#   have wishlist_split_required=True. Only those sources are processed.
#   After processing, the flag is cleared.
#
#   If no flags are set (e.g., standalone run without prior converter), the
#   splitter falls back to processing ALL sources defined in the config.
#   This ensures the script is usable both in the pipeline and standalone.
#
#   --force-all bypasses state checks entirely and processes everything.
# =============================================================================

def run_splitter(config_path, force_all=False, pipeline_mode=False):
    """
    Execute the full split workflow driven by a YAML config file.

    Args:
        config_path (str):  Path to the splitter configuration YAML.
        force_all (bool):   If True, process all sources regardless of state
                            flags. Useful for manual re-runs or recovery.
        pipeline_mode (bool): Reserved for future use. Currently the skip logic
                            behaves the same in both modes: a source is skipped
                            only when no state flag is set AND all expected
                            output files already exist. The flag is accepted
                            so the pipeline_launcher can pass it without error,
                            but it does not change behavior at this time.
                            To force a full re-split, use --force-all instead.
    """
    logger.info("=" * 80)
    logger.info("🚀 Starting Wishlist Splitter...")
    logger.info("=" * 80)

    # ------------------------------------------------------------------
    # Load pipeline state for selective execution
    # ------------------------------------------------------------------
    state = _load_state()
    spreadsheets_state = state.get("spreadsheets", {})
    state_modified = False

    # Determine execution mode:
    #   - If force_all is True, process everything (manual override).
    #   - Otherwise, we enter the source loop and decide per-source whether
    #     to skip or process based on state flags + missing output files.
    if force_all:
        logger.info("🔧 Force-all mode: processing ALL sources")
        selective_mode = False
    else:
        selective_mode = any(
            isinstance(s, dict) and s.get("wishlist_split_required", False)
            for s in spreadsheets_state.values()
        )
        if selective_mode:
            logger.info("🔍 Selective mode active: processing sources with wishlist_split_required=True")
        else:
            logger.info("📋 No split flags found in state. Checking for missing output files...")

    # ------------------------------------------------------------------
    # Load splitter config
    # ------------------------------------------------------------------
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    outputs = config.get("outputs", [])
    workbook_groups = config.get("workbook_groups", {})

    if not outputs:
        logger.warning("No outputs defined in config. Nothing to do.")
        return

    # Group outputs by source spreadsheet so we parse each source only once.
    # The key is the config_source_spreadsheet value; outputs with the same
    # key share one wishlist + scraped JSON pair.
    outputs_by_source = {}
    for out in outputs:
        source = out.get("config_source_spreadsheet", "default")
        outputs_by_source.setdefault(source, []).append(out)

    processed_any_source = False

    for source_key, source_outputs in outputs_by_source.items():
        # "default" means the output definition omitted config_source_spreadsheet.
        # We skip these because we cannot determine which source files to read.
        if source_key == "default":
            logger.warning(
                "Output missing config_source_spreadsheet, skipping. "
                "Add config_source_spreadsheet to the output definition."
            )
            continue

        # Derive filenames from the source_key prefix.
        # Example: source_key "aegis_boss-damage" maps to:
        #   - aegis_boss-damage_spreadsheet_dim_wishlist.txt
        #   - aegis_boss-damage_spreadsheet_data_scraped.json
        wishlist_file = os.path.join(WISHLIST_DIR, f"{source_key}_spreadsheet_dim_wishlist.txt")
        scraped_file = os.path.join(SCRAPED_DIR, f"{source_key}_spreadsheet_data_scraped.json")
        base_name = f"{source_key}_spreadsheet_dim_wishlist"

        # ------------------------------------------------------------------
        # Check state flag and missing output files
        # ------------------------------------------------------------------
        split_required = spreadsheets_state.get(source_key, {}).get("wishlist_split_required", False)

        # Determine if any expected output files are missing (deleted, first run,
        # or config added new outputs). If so, force processing regardless
        # of the state flag.
        any_output_missing = False
        missing_files = []
        for out_def in source_outputs:
            hardcoded_file = out_def.get("output_filename")
            file_suffix = out_def.get("output_filename_suffix", "")
            if hardcoded_file:
                out_file = hardcoded_file
            elif file_suffix:
                out_file = f"{base_name}_{file_suffix}.txt"
            else:
                out_file = f"{out_def.get('id', 'unnamed')}.txt"
            out_path = os.path.join(SPLIT_DIR, out_file)
            if not os.path.exists(out_path):
                any_output_missing = True
                missing_files.append(out_file)

        # ------------------------------------------------------------------
        # DECISION: Skip or Process
        # ------------------------------------------------------------------
        # In both selective_mode and pipeline_mode, we skip a source ONLY when:
        #   - wishlist_split_required is False (or missing)
        #   - AND all expected output files already exist
        #
        # If the converter updated the source (split_required=True), we process.
        # If output files are missing, we process (and log which ones).
        # ------------------------------------------------------------------
        if not force_all and not split_required and not any_output_missing:
            logger.info(f"  ⏭️  Skipping {source_key} (no changes and all outputs exist)")
            continue

        if any_output_missing and not split_required:
            logger.info(f"  🔄 Forcing split for {source_key} — missing output files: {', '.join(missing_files)}")

        if not os.path.exists(wishlist_file):
            logger.warning(f"Source wishlist not found: {wishlist_file}")
            continue

        logger.info(f"Processing source: {source_key}")
        workbook_logger.info(f"  Wishlist: {wishlist_file}")
        workbook_logger.info(f"  Scraped:  {scraped_file}")

        # Parse once, filter many times.
        header_lines, blocks = parse_wishlist_file(wishlist_file)
        scraped_index = build_scraped_index(scraped_file)

        workbook_logger.info(f"  Parsed {len(blocks)} weapon blocks")

        for out_def in source_outputs:
            out_id = out_def.get("id", "unnamed")

            hardcoded_file = out_def.get("output_filename")
            file_suffix = out_def.get("output_filename_suffix", "")

            # Filename resolution priority:
            #   1. output_filename (hardcoded full name)
            #   2. output_filename_suffix (append to base name)
            #   3. fallback to {id}.txt
            if hardcoded_file:
                out_file = hardcoded_file
            elif file_suffix:
                out_file = f"{base_name}_{file_suffix}.txt"
            else:
                out_file = f"{out_id}.txt"

            out_path = os.path.join(SPLIT_DIR, out_file)

            # Header override options (independent for title and description).
            title = out_def.get("wishlist_title", "")
            title_suffix = out_def.get("wishlist_title_suffix", "")
            description = out_def.get("wishlist_description", "")
            description_suffix = out_def.get("wishlist_description_suffix", "")

            # Build rule engine and evaluate every block.
            rules = out_def.get("rules", [])
            engine = RuleEngine(rules, scraped_index, workbook_groups, source_key=source_key)
            filtered = [b for b in blocks if engine.evaluate(b)]

            if filtered:
                write_output(
                    header_lines, filtered, out_path,
                    title=title,
                    title_suffix=title_suffix,
                    description=description,
                    description_suffix=description_suffix
                )
            else:
                logger.warning(f"  Output '{out_id}' produced 0 blocks — no file written")

        # ------------------------------------------------------------------
        # CLEAR SPLITTER FLAG: This source has been fully processed.
        # ------------------------------------------------------------------
        if not force_all and source_key in spreadsheets_state:
            spreadsheets_state[source_key]["wishlist_split_required"] = False
            state_modified = True
            logger.info(f"  ✅ Cleared wishlist_split_required for '{source_key}'")

        processed_any_source = True

    # ------------------------------------------------------------------
    # Commit state changes
    # ------------------------------------------------------------------
    if state_modified:
        _save_state(state)
    else:
        logger.info("No state flags were modified.")

    logger.info("=" * 80)
    if processed_any_source:
        logger.info("Wishlist Splitter complete.")
    else:
        logger.info("Wishlist Splitter complete (no-op — nothing to process).")
    logger.info("=" * 80)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================
# When executed directly, we bootstrap root console logging so progress is
# visible on stdout/stderr, then parse CLI args and invoke run_splitter().
# =============================================================================

if __name__ == "__main__":
    # Attach a StreamHandler to the root logger. All module loggers that have
    # propagate=True will emit to both this console handler AND their own
    # FileHandlers (if any). This gives real-time console feedback plus
    # persistent per-script log files.
    setup_root_console_logging()

    parser = argparse.ArgumentParser(
        description="Split DIM wishlists by configurable rules"
    )
    parser.add_argument(
        "--config",
        default="dim_wishlists_splitter_config.yaml",
        help="Path to config YAML (default: dim_wishlists_splitter_config.yaml)"
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="Process all sources regardless of state flags. "
             "Useful for manual re-runs or when the state file is stale."
    )
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Accepted for compatibility with pipeline_launcher. "
             "Currently does not change behavior; skip logic is the same "
             "with or without this flag. Use --force-all to force a full re-split."
    )
    args = parser.parse_args()

    run_splitter(args.config, force_all=args.force_all, pipeline_mode=args.pipeline)