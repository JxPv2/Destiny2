import sqlite3
import zipfile
import io
import json
import os
import requests

# Live API endpoint metadata paths hosted directly by Bungie
MANIFEST_URL = "https://www.bungie.net/Platform/Destiny2/Manifest/"
HEADERS = {"X-API-Key": "Destiny2API"}  # A header key structure must exist to interact with the API

def download_unfiltered_manifest():
    """
    Pings Bungie endpoints to locate, download, and extract the latest 
    live version of the English Mobile World Content SQLite database.
    """
    print("1. Querying Bungie live manifest endpoints...")
    r = requests.get(MANIFEST_URL, headers=HEADERS).json()
    
    # Extract the internal network file path for the English manifest version
    sqlite_path = r['Response']['mobileWorldContentPaths']['en']
    db_url = f"https://www.bungie.net{sqlite_path}"
    
    print("2. Downloading the massive compressed database file...")
    db_resp = requests.get(db_url, headers=HEADERS)
    
    print("3. Unzipping the database...")
    # Read the compressed binary content from memory and extract the database file locally
    with zipfile.ZipFile(io.BytesIO(db_resp.content)) as z:
        db_name = z.namelist()[0]
        z.extract(db_name, path=".")
    return db_name

def extract_weapon_variant(name):
    """
    Checks if a weapon name contains a special variant suffix.
    Returns a tuple of (clean_base_name, variant_tag_or_None).
    """
    suffixes = ["(Adept)", "(Timelost)", "(Harrowed)"]
    for suffix in suffixes:
        if name.endswith(suffix):
            # Strip the suffix and any trailing/leading spaces left over
            base_name = name[:-len(suffix)].strip()
            # Extract just the word (e.g., "Harrowed")
            variant_tag = suffix.strip("() ")
            return base_name, variant_tag
    return name, None

def dump_everything_complete(db_file):
    """
    Parses the SQLite database table to sort items into structured groups, tracks
    weapon socket exclusivity connections, applies label criteria, and outputs 
    the final pipe-delimited text list.
    """
    print("4. Indexing database items for structural relationship mapping...")
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    # Select the JSON strings housing item definitions out of the core manifest table
    cursor.execute("SELECT json FROM DestinyInventoryItemDefinition")
    rows = cursor.fetchall()
    
    item_lookup = {}
    weapons_and_exotics = []
    plugs_to_process = []
    trait_to_weapons_map = {}  # Format: { trait_hash: set(weapon_names) }

    # Pass 1: Parse string queries into RAM-accessible dictionaries
    for row in rows:
        item = json.loads(row[0])
        h = item.get('hash')
        if h:
            item_lookup[h] = item
            
    # Pass 2: Iterate over live elements to discover item categories and socket links
    for h, item in item_lookup.items():
        item_type = item.get('itemType', 0)
        inventory_data = item.get('inventory', {})
        tier_type = inventory_data.get('tierType', 0)
        
        # Category criteria tracking (ItemType 3 = Weapons, ItemType 2 with TierType 6 = Exotic Armor)
        is_weapon = (item_type == 3)
        is_exotic_armor = (item_type == 2 and tier_type == 6)
        
        if is_weapon or is_exotic_armor:
            weapons_and_exotics.append(item)
            
            # Helper to strip internal line breaks specifically for weapon socket name mapping
            w_raw_name = item.get('displayProperties', {}).get('name', '')
            w_name = w_raw_name.replace('\r', '').replace('\n', ' ').replace('  ', ' ').strip()
            
            if w_name:
                socket_entries = item.get('sockets', {}).get('socketEntries', [])
                for entry in socket_entries:
                    # Capture baseline curated static items inside the weapon column slot
                    init_hash = entry.get('singleInitialItemHash')
                    if init_hash:
                        if init_hash not in trait_to_weapons_map:
                            trait_to_weapons_map[init_hash] = set()
                        trait_to_weapons_map[init_hash].add(w_name)
                    
                    # Capture reusable alternative items assigned within the same column slot
                    for reusable in entry.get('reusablePlugItems', []):
                        r_hash = reusable.get('plugItemHash')
                        if r_hash:
                            if r_hash not in trait_to_weapons_map:
                                trait_to_weapons_map[r_hash] = set()
                            trait_to_weapons_map[r_hash].add(w_name)
        
        # ItemType 19 indicates inventory modifications (Perks, Sockets, or Mods)
        elif item_type == 19:
            plug_data = item.get('plug', {})
            if plug_data:
                plug_id = plug_data.get('plugCategoryIdentifier', '').lower()
                invalid_keywords = [
                    "emote", "stasis.fragment", "strand.fragment", "transmog", 
                    "holo_framer", "bounty", "shader", "projection", "memento",
                    "skins", "ornament"
                ]
                if not any(kw in plug_id for kw in invalid_keywords):
                    plugs_to_process.append(item)

    print("5. Formatting final flat layout lines...")
    all_items = []
    
    # Tracking index for Gear entries to handle merging variants
    # Structure: { (clean_item_name, item_type_label): { "hashes": [id1, id2...], "variants": set() } }
    gear_groups = {}
    
    # Pass 3A: Accumulate and group Weapons and Exotic Armor hashes, collapsing special variants
    for item in weapons_and_exotics:
        raw_name = item.get('displayProperties', {}).get('name', '')
        name = raw_name.replace('\r', '').replace('\n', ' ').replace('  ', ' ').strip()
        
        if not name:
            continue
            
        item_hash = item.get('hash')
        item_type = item.get('itemType', 0)
        type_label = "Weapon" if item_type == 3 else "Exotic Armor"
        
        # Check and clean variant suffixes if this is a weapon
        clean_name, variant = extract_weapon_variant(name) if item_type == 3 else (name, None)
        
        group_key = (clean_name, type_label)
        if group_key not in gear_groups:
            gear_groups[group_key] = {"hashes": [], "variants": set()}
            
        gear_groups[group_key]["hashes"].append(str(item_hash))
        if variant:
            gear_groups[group_key]["variants"].add(variant)
        
    # Pass 3B: Process consolidated gear entries into the final line layouts with variant suffixes
    for (name, type_label), data in gear_groups.items():
        joined_hashes = ", ".join(data["hashes"])
        
        # Build the final string. Append special version suffixes if any exist
        line = f"NAME|{name}|ID|{joined_hashes}|TYPE|{type_label}"
        if data["variants"]:
            # Sort the variants alphabetically for consistent ordering (e.g., "+Adept, Timelost")
            joined_variants = ", ".join(sorted(list(data["variants"])))
            line += f"|+{joined_variants}"
            
        all_items.append(line)
        
    # Index to group ALL Mods together by Name + their specific Type context
    mod_groups = {}
    
    # Custom Trait Grouping Storage Index
    trait_groups = {}
    
    # Process Sockets, Perks, Traits, and Slottable Mods
    for item in plugs_to_process:
        raw_name = item.get('displayProperties', {}).get('name', '')
        name = raw_name.replace('\r', '').replace('\n', ' ').replace('  ', ' ').strip()
        if not name:
            continue
            
        item_hash = item.get('hash')
        type_display = item.get('itemTypeDisplayName', '')
        plug_id = item.get('plug', {}).get('plugCategoryIdentifier', '').lower()
        
        is_slottable_mod = "mod" in type_display.lower() or "v400.mods" in plug_id or "masterwork" in plug_id
        
        if is_slottable_mod:
            mod_context = "General Mod"
            if "weapon" in plug_id or "deprecated" not in plug_id and "components" in plug_id:
                mod_context = "Weapon Mod"
            elif "armor" in plug_id:
                mod_context = "Armor Mod"
            
            if "enhanced" in type_display.lower() or "enhanced" in name.lower():
                mod_context += "|Enhanced"
            else:
                mod_context += "|Normal"
                
            mod_key = (name, mod_context)
            if mod_key not in mod_groups:
                mod_groups[mod_key] = []
            mod_groups[mod_key].append(str(item_hash))
            
        else:
            # Trait specific parsing block
            # Step 1: Detect if this trait entry is explicitly an Enhanced tier variant
            tooltips = item.get('tooltipNotifications', [])
            has_enhanced_tooltip = any(t.get('displayStyle') == 'ui_display_style_enhanced_perk' for t in tooltips)
            is_enhanced_display = "enhanced" in type_display.lower()
            
            is_enhanced = has_enhanced_tooltip or is_enhanced_display
            tier_kind = "Enhanced" if is_enhanced else "Normal"
            
            # Step 2: Determine its clean base name without checking prefix text mutations
            clean_trait_name = name
            if is_enhanced and name.lower().startswith("enhanced ") and not is_enhanced_display:
                # Only strip the "Enhanced " prefix text if the base trait name didn't inherently include it
                # (e.g., "Enhanced Hand Cannon Loader" -> "Hand Cannon Loader")
                clean_trait_name = name[9:].strip()
                
            # Collect linked parent weapons for this trait hash
            linked_weapons = trait_to_weapons_map.get(item_hash, set())
            notes_label = ""
            if 0 < len(linked_weapons) <= 15:
                notes_label = ", ".join(sorted(list(linked_weapons)))
            elif "exotic" in plug_id or "intrinsic" in plug_id:
                notes_label = ", ".join(sorted(list(linked_weapons))) if len(linked_weapons) > 0 else ""

            if clean_trait_name not in trait_groups:
                trait_groups[clean_trait_name] = {"Normal": [], "Enhanced": [], "WeaponNotes": ""}
                
            trait_groups[clean_trait_name][tier_kind].append(str(item_hash))
            if notes_label and not trait_groups[clean_trait_name]["WeaponNotes"]:
                trait_groups[clean_trait_name]["WeaponNotes"] = notes_label

    # Pass 4A: Unpack consolidated Standard Mods into output lines
    for (name, mod_context), hashes in mod_groups.items():
        joined_hashes = ", ".join(hashes)
        all_items.append(f"NAME|{name}|ID|{joined_hashes}|TYPE|{mod_context}")

    # Pass 4B: Unpack consolidated Traits applying the updated notes placement rules
    for name, trait_data in trait_groups.items():
        normal_hashes = trait_data["Normal"]
        enhanced_hashes = trait_data["Enhanced"]
        weapon_notes = trait_data["WeaponNotes"]
        
        if normal_hashes:
            joined_normal_hashes = ", ".join(normal_hashes)
            line = f"NAME|{name}|ID|{joined_normal_hashes}|TYPE|Trait"
            
            if enhanced_hashes:
                joined_enhanced_hashes = ", ".join(enhanced_hashes)
                line += f"|{joined_enhanced_hashes} Enhanced"
            elif weapon_notes:
                line += f"|{weapon_notes}"
                
            all_items.append(line)
            
        elif enhanced_hashes:
            joined_enhanced_hashes = ", ".join(enhanced_hashes)
            line = f"NAME|{name}|ID|{joined_enhanced_hashes}|TYPE|Trait|Enhanced"
            if weapon_notes:
                line += f"|{weapon_notes}"
            all_items.append(line)

    conn.close()
    
    try:
        os.remove(db_file)
    except Exception:
        pass
        
    print("6. Sorting items alphabetically by name...")
    all_items.sort(key=lambda x: x.split("NAME|")[1].split("|ID|")[0].lower())
    
    # Write the entire clean list out to the final text file
    output_filename = "raw_manifest_dump.txt"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(all_items))
        
    print(f"\nExecution complete. Output successfully exported to: '{output_filename}'")

if __name__ == "__main__":
    try:
        db_name = download_unfiltered_manifest()
        dump_everything_complete(db_name)
    except Exception as e:
        print(f"\nAn error occurred: {e}")
