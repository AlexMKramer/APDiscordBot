import json
import os
import threading
import time

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
last_source = "api"   # where the latest read came from: "api" or "pages" (the slow fallback)
_read_lock = threading.Lock()   # one read at a time: it rewrites items_received.json and diffs against it
_last_hints = None     # unfound hints from the latest API read; None when it came from the pages
HINTS_JSON = os.path.join("data", "hints.json")


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


def unfound_hints(api_hints, slot_names, item_names_by_player, location_names_by_player):
    """Unfound hints from the /api/tracker "hints" list, as readable dicts, sorted.

    Each hint is listed under both its receiver and its finder (and group members), so they are
    de-duplicated. A hint is a Hint tuple [receiving_player, finding_player, location, item,
    found, entrance, item_flags, status] or, on a different WebHost, a dict with those keys.
    Filler hints (item_flags 0) are left out."""
    fields = ("receiving_player", "finding_player", "location", "item", "found", "entrance")
    seen = {}
    for entry in api_hints or []:
        team = entry.get("team", 0)
        for hint in entry.get("hints", []):
            if isinstance(hint, dict):
                receiver, finder, location, item, found, entrance = (hint.get(f) for f in fields)
                flags = hint.get("item_flags")
            else:
                receiver, finder, location, item, found = hint[:5]
                entrance = hint[5] if len(hint) > 5 else ""
                flags = hint[6] if len(hint) > 6 else None
            if found:
                continue
            # Same rule as the item feed: flags 0 is filler (traps, useful and progression stay).
            # A hint with no flags is kept.
            if flags == 0:
                continue
            key = (team, receiver, finder, location, item, entrance or "")
            seen[key] = {
                "receiver": slot_names.get(receiver, f"Player {receiver}"),
                "item": item_names_by_player.get(receiver, {}).get(item, str(item)),
                "finder": slot_names.get(finder, f"Player {finder}"),
                "location": location_names_by_player.get(finder, {}).get(location, str(location)),
                "entrance": entrance or "",
            }
    return sorted(seen.values(), key=lambda h: (h["receiver"].lower(), h["finder"].lower(),
                                                h["location"].lower(), h["item"].lower()))


def load_hints():
    """The unfound hints last written by the tracker loop, or None if there is no data."""
    try:
        with open(HINTS_JSON, encoding="utf-8") as fh:
            return json.load(fh).get("hints")
    except (OSError, json.JSONDecodeError):
        return None


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
                      "item_by_id": item_names, "location_by_id": location_names,
                      "item_names": set(item_names.values()), "location_names": set(location_names.values()),
                      "received": _received_names(received.get((team, player), []), item_names),
                      "checked": [location_names.get(loc, str(loc)) for loc in checked.get((team, player), [])]})
    start = _start_inventories(tracker_url, auth, tracker_id, slots, received)

    # Item names belong to the receiving player's game, location names to the finding player's.
    global _last_hints
    # A slot whose API names don't match its room (see above) gets ids rather than wrong names.
    by_player = {slot["player"]: ({**slot, "item_by_id": {}, "location_by_id": {}}
                                  if start.get(slot["name"], {}).get("page") else slot)
                 for slot in slots}
    try:
        _last_hints = unfound_hints(
            tracker.get("hints"), {n: s["name"] for n, s in by_player.items()},
            {n: s["item_by_id"] for n, s in by_player.items()},
            {n: s["location_by_id"] for n, s in by_player.items()})
    except Exception as e:
        print(f"[tracker] could not read hints: {e}")
        _last_hints = None

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


def _write_hints(hints):
    with open(HINTS_JSON + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"hints": hints}, fh, indent=2)
    for attempt in range(10):
        try:
            os.replace(HINTS_JSON + ".tmp", HINTS_JSON)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


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


ANNOUNCED_JSON = os.path.join("data", "announced_marks.json")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _item_counts(items):
    """{item name: total count} from a slot's "Items" dict (anything else counts as empty)."""
    counts = {}
    if isinstance(items, dict):
        for item in items.values():
            name = item.get("item_name", "Unknown")
            counts[name] = counts.get(name, 0) + _int(item.get("amount", 0))
    return counts


def _marks_from_result(result):
    """Marks matching a saved result: its item counts, and the slots whose goal was completed."""
    marks = {"items": {}, "goals": []}
    for slot_data in (result or {}).values():
        if not isinstance(slot_data, dict):
            continue
        for slot_name, details in slot_data.items():
            if not isinstance(details, dict):
                continue
            marks["items"][slot_name] = _item_counts(details.get("Items"))
            if _goal_done(details):
                marks["goals"].append(slot_name)
    return marks


def _goal_done(details):
    return str(details.get("Game Status", "")).strip().lower() in ("goal completed", "completed")


def _load_marks(tracker_id, old_result):
    """The announced marks for this room: {"tracker", "items": {slot: {item: count}}, "goals": [slot]}.

    A different room starts from nothing. With no marks file yet, they start from the last saved
    result so an update does not announce the whole inventory again."""
    try:
        with open(ANNOUNCED_JSON, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("tracker") != tracker_id:
            return {"tracker": tracker_id, "items": {}, "goals": []}
        return {"tracker": tracker_id, "items": saved.get("items", {}), "goals": list(saved.get("goals", []))}
    except (OSError, ValueError, AttributeError):
        marks = _marks_from_result(old_result)
        marks["tracker"] = tracker_id
        return marks


def _save_marks(marks):
    with open(ANNOUNCED_JSON + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(marks, fh)
    for attempt in range(10):
        try:
            os.replace(ANNOUNCED_JSON + ".tmp", ANNOUNCED_JSON)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def _diff_against_marks(result, marks):
    """What is new in `result` since the marks, as {slot: {slot name: {"New Items": {...},
    "Goal Completed": status}}}. Raises the marks to the new counts and lifts `result` in place
    to at least the marked counts and a completed goal."""
    diff = {}
    for slot, slot_data in result.items():
        for slot_name, details in slot_data.items():
            marked = marks["items"].setdefault(slot_name, {})
            items = details.get("Items")
            counts = _item_counts(items)
            diff_items = {}
            for name, count in counts.items():
                if count > marked.get(name, 0):
                    diff_items[name] = count - marked.get(name, 0)
                    marked[name] = count
            if isinstance(items, dict):
                # Lift anything that read below its mark back up, so the saved result never drops.
                for name, count in marked.items():
                    deficit = count - counts.get(name, 0)
                    if deficit <= 0:
                        continue
                    entry = next((e for e in items.values() if e.get("item_name") == name), None)
                    if entry is not None:
                        entry["amount"] = _int(entry.get("amount")) + deficit
                    else:
                        key = len(items) + 1
                        while str(key) in items:
                            key += 1
                        items[str(key)] = {"item_name": name, "amount": deficit}

            diff_entry = {}
            if diff_items:
                diff_entry["New Items"] = diff_items
            status = str(details.get("Game Status", "")).strip()
            if slot_name in marks["goals"]:
                if not _goal_done(details):
                    details["Game Status"] = "Goal Completed"
            elif _goal_done(details):
                marks["goals"].append(slot_name)
                diff_entry["Goal Completed"] = status
            if diff_entry:
                diff.setdefault(slot, {})[slot_name] = diff_entry
    return diff


def get_all_tracker_received_items(tracker_url, auth):
    with _read_lock:
        return _read_tracker(tracker_url, auth)


def _read_tracker(tracker_url, auth):
    global _api_warned, _last_hints, last_source
    try:
        _last_hints = None
        result = _result_from_api(tracker_url, auth)
        _api_warned = False
        last_source = "api"
    except Exception as e:
        last_source = "pages"
        _last_hints = None
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

    # The WebHost can serve an older copy of the tracker between two newer ones (separate caches
    # per worker, or the API and the pages cached apart). Counts are therefore compared with the
    # highest count already announced, not with the previous read, and the saved result never
    # goes below it: a stale copy neither re-announces an item when the count rises back nor
    # makes it vanish from the inventory other loops read.
    try:
        tracker_id = _split_tracker_url(tracker_url)[1]
    except ValueError:
        tracker_id = tracker_url
    marks = _load_marks(tracker_id, old_result)
    diff = _diff_against_marks(result, marks)
    _save_marks(marks)

    # Write to a temp file and swap it in: other loops read this file while it's being written,
    # and a half-written file reads as empty (no items, nothing checked).
    with open(items_received_json + ".tmp", "w") as outfile:
        json.dump(result, outfile, indent=4)
    for attempt in range(10):
        try:
            os.replace(items_received_json + ".tmp", items_received_json)
            break
        except PermissionError:  # Windows: a reader has the file open for a moment
            if attempt == 9:
                raise
            time.sleep(0.1)

    # Hints only come from the API; the page fallback leaves the last file as it was.
    if _last_hints is not None:
        _write_hints(_last_hints)

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
