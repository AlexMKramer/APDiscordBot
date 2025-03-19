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

        # Structure the JSON with slot number as key, then slot name containing game name and items.
        result[slot_number] = {
            slot_name: {
                "Game Name": game_name,
                "Game Status": game_status,
                "Checks Status": checks_status,
                "Items": item_dict
            }
        }

    # Write the results to a JSON file.
    os.makedirs("data", exist_ok=True)
    items_received_json = os.path.join("data", "items_received.json")

    with open(items_received_json, "w") as outfile:
        json.dump(result, outfile, indent=4)

    # close the file
    outfile.close()

    print("JSON output saved to output.json")


