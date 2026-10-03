import json
import os

from bs4 import BeautifulSoup
import requests


def get_tracker_urls(tracker_url, auth):
    # Always pass a timeout: without one a stalled connection hangs indefinitely (the default
    # connect timeout is None), tying up the worker thread. On failure this raises, which the
    # caller's loop catches, logs, and retries next cycle (last-good data is preserved).
    page = requests.get(url=tracker_url, auth=auth, timeout=15)
    soup = BeautifulSoup(page.content, "html.parser")

    urls = []
    slot_names = []
    slot_numbers = []
    game_names = []
    games_statuses = []
    checks_statuses = []

    table = soup.find("table", id="checks-table")
    if not table:
        return slot_numbers, urls, slot_names, game_names
    tbody = table.find("tbody")
    if not tbody:
        return slot_numbers, urls, slot_names, game_names
    rows = tbody.find_all("tr")

    for idx, row in enumerate(rows):
        tds = row.find_all("td")
        # Ensure there are at least three cells: link, slot name, and game name.
        if len(tds) < 3:
            continue

        # Use the row order (starting at 1) as the slot number.
        slot_number = str(idx + 1)
        slot_numbers.append(slot_number)

        # Extract URL from the first cell (assumes the link is the second element in the cell's contents)
        slot_link = tds[0].contents[1]
        link = slot_link['href']
        link = link.split("/tracker")[1]
        urls.append(link)

        # Get slot name from the second cell.
        slot_name = tds[1].get_text(strip=True)
        slot_names.append(slot_name)

        # Get game name from the third cell.
        game_name = tds[2].get_text(strip=True)
        game_names.append(game_name)

        # Get game status from the fourth cell.
        game_status = tds[3].get_text(strip=True)
        if game_status != "Goal Completed":
            game_status = "Goal Incomplete"
        games_statuses.append(game_status)

        # Get checks status from the fifth cell.
        checks_status = tds[4].get_text(strip=True)
        checks_statuses.append(checks_status)

    return slot_numbers, urls, slot_names, game_names, games_statuses, checks_statuses


def _checked_locations(soup):
    """Names of the slot's checked locations: the locations table marks each with a ✔."""
    table = soup.find("table", id="locations-table")
    tbody = table.find("tbody") if table else None
    if not tbody:
        return []
    checked = []
    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) >= 2 and "✔" in tds[1].get_text():
            checked.append(tds[0].get_text(strip=True))
    return checked


def track_items_from_slot(tracker_url, url, auth):
    """(received items, checked location names) from a slot's page. Items is None when the page
    has no received table (the game is completed)."""
    tracker_slot_url = tracker_url.split("/tracker")[0] + "/generic_tracker" + url
    page = requests.get(tracker_slot_url, auth=auth, timeout=15)
    soup = BeautifulSoup(page.content, "html.parser")
    checked = _checked_locations(soup)

    items = []
    table = soup.find("table", id="received-table")
    if table is None:
        return None, checked
    tbody = table.find("tbody")
    if not tbody:
        return None, checked
    rows = tbody.find_all("tr")

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 2:
            continue
        item_name = tds[0].get_text(strip=True)
        item_amount_text = tds[1].get_text(strip=True)
        try:
            amount = int(item_amount_text)
        except ValueError:
            amount = item_amount_text

        items.append({
            "item_name": item_name,
            "amount": amount
        })

    # Reverse the list to ensure items are ordered in the sequence they were received.
    items.reverse()
    return items, checked


# --- reading the tracker's JSON API -----------------------------------------
# One /api/tracker request carries every slot's received items and checked locations, instead
# of one page per slot. It leaves out three things, filled in here: slot names (the main
# tracker page), item/location names (/api/datapackage/<checksum>, cached on disk) and start
# inventory (worked out once per slot from its page, then cached, since it never changes).
# That first look at each page also checks the datapackage's names against the page: the
# WebHost can hand back a different package for a checksum than the room was generated with
# (seen with Cookie Clicker, whose apworld has no manifest), and those slots stay on the page.

DATAPACKAGE_DIR = os.path.join("data", "datapackage_cache")
START_INVENTORY_JSON = os.path.join("data", "start_inventory.json")
_names_memo = {}       # checksum -> (item id -> name, location id -> name)
_static_memo = {}      # tracker id -> /api/static_tracker response
_api_warned = False


def _split_tracker_url(tracker_url):
    base, rest = tracker_url.split("/tracker/", 1)
    return base, rest.strip("/").split("/")[0]


def _get_json(url, auth):
    response = requests.get(url, auth=auth, timeout=30)
    response.raise_for_status()
    return response.json()


def _names_for(base, checksum, auth):
    """(item id -> name, location id -> name) for a game's datapackage, by checksum."""
    if checksum not in _names_memo:
        path = os.path.join(DATAPACKAGE_DIR, f"{checksum}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                package = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            package = _get_json(f"{base}/api/datapackage/{checksum}", auth)
            os.makedirs(DATAPACKAGE_DIR, exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(package, fh)
            os.replace(path + ".tmp", path)
        _names_memo[checksum] = ({v: k for k, v in package.get("item_name_to_id", {}).items()},
                                 {v: k for k, v in package.get("location_name_to_id", {}).items()})
    return _names_memo[checksum]


def _received_names(api_items, item_names):
    return [item_names.get(entry[0], str(entry[0])) for entry in api_items]


def _start_inventories(tracker_url, auth, tracker_id, slots, received_now):
    """{slot name: {"start": {item: count}, "page": bool}}. Start inventory is the slot page's
    counts (which include start items) minus what the API says was received. "page" marks a
    slot whose page names don't match the API datapackage, so it keeps being read from its page."""
    try:
        with open(START_INVENTORY_JSON, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        cached = {}
    known = cached.get("slots", {}) if cached.get("tracker") == tracker_id else {}
    missing = [slot for slot in slots if slot["name"] not in known]
    if not missing:
        return known

    pages = {}
    for slot in missing:
        pages[slot["name"]] = track_items_from_slot(tracker_url, slot["url"], auth)
    # Re-read the API: a slot that received something between the two reads would make that
    # item look like a start item, so only slots whose received list didn't move are kept.
    base, _ = _split_tracker_url(tracker_url)
    again = {(p["team"], p["player"]): len(p["items"])
             for p in _get_json(f"{base}/api/tracker/{tracker_id}", auth)["player_items_received"]}
    for slot in missing:
        key = (slot["team"], slot["player"])
        if again.get(key) != len(received_now.get(key, [])):
            continue
        page_items, page_checked = pages[slot["name"]]
        page_counts = {}
        for item in page_items or []:
            try:
                page_counts[item["item_name"]] = page_counts.get(item["item_name"], 0) + int(item["amount"])
            except (TypeError, ValueError):
                pass
        names_match = (set(page_counts) <= slot["item_names"]
                       and set(page_checked) <= slot["location_names"])
        if not names_match:
            known[slot["name"]] = {"start": {}, "page": True}
            continue
        api_counts = {}
        for name in slot["received"]:
            api_counts[name] = api_counts.get(name, 0) + 1
        known[slot["name"]] = {"start": {name: count - api_counts.get(name, 0)
                                         for name, count in page_counts.items()
                                         if count > api_counts.get(name, 0)},
                               "page": False}
    with open(START_INVENTORY_JSON + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"tracker": tracker_id, "slots": known}, fh)
    os.replace(START_INVENTORY_JSON + ".tmp", START_INVENTORY_JSON)
    return known


def _result_from_api(tracker_url, auth):
    """The same {slot: {slot_name: {...}}} shape the page scraper builds, from the JSON API."""
    base, tracker_id = _split_tracker_url(tracker_url)
    slot_numbers, urls, slot_names, game_names, games_statuses, checks_statuses = get_tracker_urls(tracker_url, auth)
    tracker = _get_json(f"{base}/api/tracker/{tracker_id}", auth)
    if tracker_id not in _static_memo:
        _static_memo[tracker_id] = _get_json(f"{base}/api/static_tracker/{tracker_id}", auth)
    packages = _static_memo[tracker_id]["datapackage"]
    received = {(p["team"], p["player"]): p["items"] for p in tracker["player_items_received"]}
    checked = {(p["team"], p["player"]): p["locations"] for p in tracker["player_checks_done"]}

    slots = []
    for idx, url in enumerate(urls):
        team, player = (int(part) for part in url.rstrip("/").split("/")[-2:])
        item_names, location_names = _names_for(base, packages[game_names[idx]]["checksum"], auth)
        slots.append({"idx": idx, "url": url, "name": slot_names[idx], "team": team, "player": player,
                      "item_names": set(item_names.values()), "location_names": set(location_names.values()),
                      "received": _received_names(received.get((team, player), []), item_names),
                      "checked": [location_names.get(loc, str(loc)) for loc in checked.get((team, player), [])]})
    start = _start_inventories(tracker_url, auth, tracker_id, slots, received)

    result = {}
    for slot in slots:
        idx = slot["idx"]
        info = start.get(slot["name"], {})
        if info.get("page"):
            # The API's names for this game don't match the room's; read it from its page.
            page_items, slot["checked"] = track_items_from_slot(tracker_url, slot["url"], auth)
            counts = {}
            for item in page_items or []:
                try:
                    counts[item["item_name"]] = counts.get(item["item_name"], 0) + int(item["amount"])
                except (TypeError, ValueError):
                    pass
        else:
            counts = dict(info.get("start", {}))   # start items first, then in the order received
            for name in slot["received"]:
                counts[name] = counts.get(name, 0) + 1
        result[slot_numbers[idx]] = {
            slot["name"]: {
                "Game Name": game_names[idx],
                "Game Status": games_statuses[idx],
                "Checks Status": checks_statuses[idx],
                "Items": {str(i + 1): {"item_name": name, "amount": count}
                          for i, (name, count) in enumerate(counts.items())},
                "Checked Locations": slot["checked"],
            }
        }
    return result


def _result_from_pages(tracker_url, auth):
    """The original scraper: every slot's tracker page, one request each."""
    result = {}
    slot_numbers, urls, slot_names, game_names, games_statuses, checks_statuses = get_tracker_urls(tracker_url, auth)
    for idx, url in enumerate(urls):
        slot_number = slot_numbers[idx]
        slot_name = slot_names[idx]
        game_name = game_names[idx]
        game_status = games_statuses[idx]
        checks_status = checks_statuses[idx]
        items, checked = track_items_from_slot(tracker_url, url, auth)
        if items is not None:
            # Create a dictionary with numbered items (starting at 1)
            item_dict = {str(i + 1): item for i, item in enumerate(items)}
        else:
            item_dict = "Game Completed!"

        result[slot_number] = {
            slot_name: {
                "Game Name": game_name,
                "Game Status": game_status,
                "Checks Status": checks_status,
                "Items": item_dict,
                "Checked Locations": checked,
            }
        }
    return result


def get_all_tracker_received_items(tracker_url, auth):
    global _api_warned
    try:
        result = _result_from_api(tracker_url, auth)
        _api_warned = False
    except Exception as e:
        # An older WebHost without the API, or a hiccup: read every slot's page instead.
        if not _api_warned:
            print(f"[tracker] JSON API unavailable, reading every slot page instead: {e}")
            _api_warned = True
        result = _result_from_pages(tracker_url, auth)

    # Ensure the data subdirectory exists.
    os.makedirs("data", exist_ok=True)
    items_received_json = os.path.join("data", "items_received.json")

    # Load previous results, if they exist.
    old_result = {}
    if os.path.exists(items_received_json):
        try:
            with open(items_received_json, "r") as infile:
                old_result = json.load(infile)
        except Exception as e:
            print(f"Error loading old items_received file: {e}")
            old_result = {}

    # Compute diff: aggregate amounts by item name for new and old results,
    # then record the positive differences. Also, check for changes in game status (completed).
    diff = {}
    for slot, new_slot_data in result.items():
        # new_slot_data is a dict with a single key: the slot name.
        for slot_name_key, new_details in new_slot_data.items():
            new_items = new_details.get("Items", {})
            # Aggregate new items by item name.
            new_agg = {}
            if isinstance(new_items, dict):
                for key, item in new_items.items():
                    name = item.get("item_name", "Unknown")
                    try:
                        amount = int(item.get("amount", 0))
                    except:
                        amount = 0
                    new_agg[name] = new_agg.get(name, 0) + amount

            # Aggregate old items for the same slot.
            old_agg = {}
            if slot in old_result:
                old_slot_data = old_result[slot]
                if slot_name_key in old_slot_data:
                    old_details = old_slot_data[slot_name_key]
                    old_items = old_details.get("Items", {})
                    if isinstance(old_items, dict):
                        for key, item in old_items.items():
                            name = item.get("item_name", "Unknown")
                            try:
                                amount = int(item.get("amount", 0))
                            except:
                                amount = 0
                            old_agg[name] = old_agg.get(name, 0) + amount

            # Compare aggregated values.
            diff_items = {}
            for name, new_total in new_agg.items():
                old_total = old_agg.get(name, 0)
                if new_total > old_total:
                    diff_items[name] = new_total - old_total

            # Check game status for completion.
            new_game_status = new_details.get("Game Status", "").strip()
            old_game_status = ""
            if slot in old_result:
                old_slot_data = old_result[slot]
                if slot_name_key in old_slot_data:
                    old_game_status = old_slot_data[slot_name_key].get("Game Status", "").strip()

            # We consider the game completed if the new status equals "Game Completed!" or "Completed" (case-insensitive).
            is_completed = new_game_status.lower() in ["goal completed", "completed"]

            diff_entry = {}
            if diff_items:
                diff_entry["New Items"] = diff_items
            if is_completed and (new_game_status != old_game_status):
                diff_entry["Goal Completed"] = new_game_status

            if diff_entry:
                diff.setdefault(slot, {})[slot_name_key] = diff_entry

    # Write the new results to the JSON file.
    with open(items_received_json, "w") as outfile:
        json.dump(result, outfile, indent=4)

    return diff


def parse_checks_status(text):
    """Parse a tracker "Checks Status" cell ("28/417") into (checked, total).

    Returns None for anything unparseable (missing cell, odd formatting, zero total) so a
    single bad row can't skew or crash the multiworld total."""
    if not isinstance(text, str) or "/" not in text:
        return None
    checked_text, _, total_text = text.partition("/")
    try:
        checked = int(checked_text.strip().replace(",", ""))
        total = int(total_text.strip().replace(",", ""))
    except ValueError:
        return None
    if total <= 0:
        return None
    return checked, total


def overall_completion(items_received=None):
    """Sum every slot's "Checks Status" into (checked, total) for the whole multiworld.

    Reads data/items_received.json (rewritten by get_all_tracker_received_items every cycle)
    when no data is passed, so callers cost no extra HTTP requests. Returns None when no slot
    reports a parseable checks count."""
    if items_received is None:
        items_received_json = os.path.join("data", "items_received.json")
        try:
            with open(items_received_json, "r") as infile:
                items_received = json.load(infile)
        except Exception as e:
            print(f"[percentage] could not read {items_received_json}: {e}")
            return None

    total_checked = 0
    total_checks = 0
    found = False
    for slot_data in (items_received or {}).values():
        if not isinstance(slot_data, dict):
            continue
        # Each slot maps its slot name to the details dict.
        for details in slot_data.values():
            if not isinstance(details, dict):
                continue
            parsed = parse_checks_status(details.get("Checks Status"))
            if parsed is None:
                continue
            total_checked += parsed[0]
            total_checks += parsed[1]
            found = True

    if not found or total_checks <= 0:
        return None
    return total_checked, total_checks
