# D2-Stuff Pipeline Changelog

---

## 2026-05-29 — Log Naming & State Ordering

### Fixed
- **`version_state_checker.py`**
  - **Workbook ordering in `local_version_state.json`**: Results from parallel `ThreadPoolExecutor` were being committed in completion-order (fastest download first) instead of config-order. Now buffers results into a temporary dict, then rebuilds `ss_state["workbooks"]` by iterating over the original `config.yaml` `workbooks` list, preserving insertion order in the JSON output.
  - **Log filename**: Changed logger name from `VersionChecker` → `version_state_checker` so the log file matches the script filename.

- **`bungie_manifest_downloader.py`**
  - **Log filename**: Changed logger name from `ManifestDownloader` → `bungie_manifest_downloader`.

- **`bungie_manifest_compiler.py`**
  - **Log filename**: Changed logger name from `ManifestCompiler` → `bungie_manifest_compiler`.

- **`dim_wishlists_converter.py`**
  - **Log filename**: Changed logger name from `WishlistGenerator` → `dim_wishlists_converter`. The auto-generated `_warnings.log` now also follows the script name.

- **`dim_wishlists_splitter.py`**
  - **Log filename**: Changed logger name from `WishlistSplitter` → `dim_wishlists_splitter`.

---

## 2026-05-29 — Splitter Indentation Refinement

### Changed
- **`dim_wishlists_splitter.py`**
  - **Logging indentation**: Refined `IndentAdapter` usage for workbook-level and detail-level output. `workbook_logger` (indent=2) now handles source-level file paths and block counts, while `details_logger` (indent=3) handles per-output write confirmations. This makes the splitter log output visually consistent with the converter's three-tier indentation hierarchy.

---

## 2026-05-29 — Pipeline Review (13 Issues Identified)

### Fixed
- **`pipeline_utils.py`**
  - **Critical mutable default dict bug**: `get_spreadsheet_state_template()` was previously a module-level dict, causing all spreadsheets to share the same `workbooks` sub-dict. Converted to a factory function so every call returns an independent dict.
  - **`local_version_state.json` key ordering**: Spreadsheet-level flags (`wishlist_update_required`, `wishlist_split_required`) now appear **before** the `workbooks` dict in the JSON output, matching the template structure.

- **`dim_wishlists_splitter.py`**
  - **Rank translation feature**: Added support in `RuleEngine` to translate raw numeric ranks (e.g., `"1"`, `"2"`) from `config.yaml`'s `rank_mappings` tables into human-readable strings at evaluation time. Splitter config can now use raw numbers instead of translated strings.

---

## 2026-05-28 — Deduplication & Data Integrity

### Fixed
- **`aegis_speedrunner_spreadsheet_data_scraper.py`**
  - **Duplicate weapon suffixing**: When the same weapon appears multiple times in one workbook, subsequent entries are now suffixed with `_2`, `_3`, etc. instead of overwriting the first occurrence.

- **`dim_wishlists_splitter.py`**
  - **Same-weapon rank matching**: `build_scraped_index()` now indexes by bare `weapon_name` (not the suffixed dict key), storing a list of entries per name. `RuleEngine.evaluate()` matches the correct record by extracting `[Rank]` from `block.notes` and comparing with `info.rank` in each scraped record. Prevents same-weapon duplicates with different ranks from being evaluated against the wrong record.

---

## 2026-05-26 — Converter Robustness & Logging Architecture

### Fixed
- **`dim_wishlists_converter.py`**
  - **Case-insensitive perk lookup**: Added `perk_map_lower` for lowercase perk name matching.
  - **Diacritic folding**: Added `perk_map_folded` to handle accented characters (e.g., `Häkke` → `Hakke`).
  - **Variant perk matching**: Added `perk_map_by_hash` reverse lookup so perks like `Outlaw` can match `Outlaw Refit` when the spreadsheet uses a shortened name.
  - **Adept/normal weapon collision**: `item_map` now `extend`s lists instead of `overwrite`, so `Palindrome` and `Palindrome (Adept)` (both cleaning to `palindrome`) coexist as separate instances.
  - **Rejected lines counter**: Added `rejected_lines` to final result logs for visibility into how many combos were filtered out by pool validation.
  - **Logging architecture**: Switched to `propagate=True` with root `StreamHandler` + per-script `FileHandler` only, eliminating duplicate handler stacks.

- **`pipeline_utils.py`**
  - **Smart indent double-counting**: `SmartIndentFormatter` now uses `getattr(record, "_smart_indent_seen", False)` instead of fragile `id(record)` tracking, preventing double-counting when `FileHandler` + `StreamHandler` share one formatter instance.

---

## 2026-05-25 — Smart Indent Formatter

### Fixed
- **`pipeline_utils.py`**
  - **Indentation fix**: `SmartIndentFormatter` corrected to prevent double-counting log records across shared handlers. All logs now match desired output perfectly.

---

## Planned / In Progress
- **GitHub Actions CI**: Pipeline scheduled to run every 8 hours. Output wishlists to `dim_wishlists/` at repo root and `dim_wishlists/spreadsheets_to_dim_wishlists/dim_wishlists/` locally.
- **Repo naming**: `D2-Stuff`, open source.
- **Comment pass**: Adding clear inline comments to all scripts before pushing to GitHub.

---

*Generated: 2026-05-29 03:07 UTC*
