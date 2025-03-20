import json
import os

from bs4 import BeautifulSoup
import requests


def get_tracker_urls(tracker_url, auth):
    page = requests.get(url=tracker_url, auth=auth)
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


def track_items_from_slot(tracker_url, url, auth):
    tracker_slot_url = tracker_url.split("/tracker")[0] + "/generic_tracker" + url
    page = requests.get(tracker_slot_url, auth=auth)
    soup = BeautifulSoup(page.content, "html.parser")

    items = []
    table = soup.find("table", id="received-table")
    if table is None:
        return None
    tbody = table.find("tbody")
    if not tbody:
        return None
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
    return items


def get_all_tracker_received_items(tracker_url, auth):
    # Build the new result from tracker data
    result = {}
    slot_numbers, urls, slot_names, game_names, games_statuses, checks_statuses = get_tracker_urls(tracker_url, auth)
    for idx, url in enumerate(urls):
        slot_number = slot_numbers[idx]
        slot_name = slot_names[idx]
        game_name = game_names[idx]
        game_status = games_statuses[idx]
        checks_status = checks_statuses[idx]
        items = track_items_from_slot(tracker_url, url, auth)
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
                "Items": item_dict
            }
        }

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


