import asyncio
import json
import re
import time
from json import JSONEncoder, JSONDecoder
import os
from time import sleep

from discord.ext import tasks
import websockets

is_websocket_connected = False
auto_reconnect = False
packet_queue = asyncio.Queue()

# One-shot data-collection progress for a get_server_data run. The bot only needs to
# stay connected long enough to receive the slot info (the "Connected" packet) and a
# data package for every game in the room; once it has those it disconnects (per the
# README: "get the packets it needs, and disconnect"). These reset at the start of main().
room_info_received = False
connected_received = False
expected_data_packages = 0
received_data_packages = 0
intentional_close = False

def encode(data):
    return JSONEncoder(
    ensure_ascii=False,
    check_circular=False,
    separators=(',', ':'),
).encode(data)


def decode(data):
    return JSONDecoder().decode(data)


# Send the initial connection to the server
async def send_connect_packet(websocket, slot_name, password):
    global auto_reconnect
    auto_reconnect = True

    payload = [{
        'cmd': 'Connect',
        'password': password,
        'name': slot_name,
        "version": {"build": 0, "class": "Version", "major": 0, "minor": 5},
        'tags': ["TextOnly"],
        'items_handling': 0b000,
        'uuid': "",
        'game': "",
        "slot_data": False,
    }]
    await send(websocket, payload)


# Disconnect from the server
async def disconnect(websocket):
    await websocket.close()
    global is_websocket_connected, auto_reconnect
    is_websocket_connected = False
    print("Disconnected from the server.")
    auto_reconnect = False

async def check_connection(websocket, slot_name, password):
    global is_websocket_connected, auto_reconnect
    while True:
        await asyncio.sleep(10)
        if not is_websocket_connected:
            if auto_reconnect:
                print("Attempting to reconnect...")
                await send_connect_packet(websocket, slot_name, password)
            else:
                print("Auto-reconnect is disabled. Exiting.")
                break


# Test hello package
async def send_hello(websocket):

    payload = [{
        'cmd': 'Say',
        'text': "Don't worry, I'm just grabbing some data."
    }]
    print("Sending hello message")
    await send(websocket, payload)


# Send a packet to the server containing the payload
async def send(websocket, payload):
    # Encode the payload
    payload = encode(payload)
    print(f"Sending: {payload}")
    await websocket.send(payload)


# Get the items for the given slot
async def get_data_package(websocket, game):
    payload = [{
        'cmd': 'GetDataPackage',
        'games': [game]
    }]
    await send(websocket, payload)


# Process data packages
async def process_data_package(data_package):
    # Extract the games dictionary; if missing, use an empty dict.
    new_games = data_package.get('games', {})
    if not new_games:
        return []

    # Define the path for our data file
    os.makedirs("data", exist_ok=True)
    data_package_json = os.path.join("data", "data_package.json")

    # Load existing data if it exists
    if os.path.exists(data_package_json):
        with open(data_package_json, "r") as f:
            try:
                existing_data = json.load(f)
            except json.JSONDecodeError:
                existing_data = []
    else:
        existing_data = []

    # Convert existing data to a dictionary for easy access
    existing_games_dict = {entry["game"]: entry for entry in existing_data}

    # Process new games
    for game_name, game_data in new_games.items():
        if game_name in existing_games_dict:
            continue  # Skip if the game already exists

        # Get items and locations; default to empty dicts if keys are missing.
        items = game_data.get("item_name_to_id", {})
        locations = game_data.get("location_name_to_id", {})

        # Sort the inner dictionaries by key (optional)
        sorted_items = dict(sorted(items.items()))
        sorted_locations = dict(sorted(locations.items()))

        # Add new game entry to the dictionary
        existing_games_dict[game_name] = {
            "game": game_name,
            "item_name_to_id": sorted_items,
            "location_name_to_id": sorted_locations
        }

    # Convert back to a list for JSON output
    updated_data = list(existing_games_dict.values())

    # Save the updated data back atomically: write a temp file then os.replace, so a reader
    # (e.g. an autocomplete) can never observe a half-written data_package.json.
    tmp_path = data_package_json + ".tmp"
    with open(tmp_path, "w") as outfile:
        json.dump(updated_data, outfile, indent=4)
    os.replace(tmp_path, data_package_json)

    return updated_data


async def build_reverse_data_package_mapping(data_package):
    reverse_item = {}
    reverse_location = {}

    for game in data_package:
        for name, id_val in game.get("item_name_to_id", {}).items():
            reverse_item[id_val] = name
        for name, id_val in game.get("location_name_to_id", {}).items():
            reverse_location[id_val] = name
    return reverse_item, reverse_location


# Listen for packets being sent to us and send them to the read_response function
async def handle_messages(discord_ack, websocket):
    global is_websocket_connected
    try:
        while websocket.state == websockets.protocol.State.OPEN:
            is_websocket_connected = True
            response = await websocket.recv()
            # Process response (assuming response is a JSON-encoded list of messages)
            for msg in decode(response):
                await add_packet_to_queue(discord_ack, websocket, msg)
    except websockets.exceptions.ConnectionClosed:
        is_websocket_connected = False
        # Don't clobber the success/refusal message when we closed on purpose.
        if not intentional_close:
            await discord_ack.edit_original_response(content="Connection was closed.")
        print("Connection was closed.")

async def add_packet_to_queue(discord_ack, websocket, packet):
    await packet_queue.put((discord_ack, websocket, packet))
    print(f"Added a packet to the queue. Queue size: {packet_queue.qsize()}")


# Read the provided packet and process it by type
@tasks.loop(seconds=1)
async def read_response():
    global is_websocket_connected, auto_reconnect
    global room_info_received, connected_received, expected_data_packages, received_data_packages, intentional_close
    try:
        if not packet_queue.empty():
            discord_ack, websocket, msg = await packet_queue.get()
            if msg.get("cmd") == "Connected":
                connected_received = True
                print(msg)
                print("Connected to the server")

                slot_info = msg.get("slot_info", {})
                slot_mapping = {}

                for slot, info in slot_info.items():
                    slot_mapping[slot] = {
                        "slot_name": info.get("name", "Unknown"),
                        "game": info.get("game", "Unknown")
                    }
                main.slot_mapping = slot_mapping

                os.makedirs("data", exist_ok=True)
                slot_info_json = os.path.join("data", "slot_info.json")
                with open (slot_info_json, "w") as f:
                    json.dump(slot_mapping, f, indent=4)

                is_websocket_connected = True
                auto_reconnect = True
                await discord_ack.edit_original_response(content="Connected to the server")




            elif msg.get("cmd") == "RoomInfo":
                print("Got room info packet")
                print(msg)
                games_in_server = msg.get("games", {})
                room_info_received = True
                # We request one data package per game, so expect one response each.
                expected_data_packages = len(games_in_server)
                for game in games_in_server:
                    print(f"Getting data package for {game}")
                    await get_data_package(websocket, game)

            elif msg.get("cmd") == "DataPackage":
                print("Got a data package")
                received_data_packages += 1

                main.data_package_mapping = await process_data_package(msg["data"])

            elif msg.get("cmd") == "PrintJSON":
                if msg.get("type") == "ItemSend":
                    print("Someone got an item, processing information.")
                    print(msg)

                    data_array = msg.get("data")
                    if not data_array:
                        print("No data array found in the message.")
                        return

                    # Extract player IDs from the data array.
                    # (We assume elements with type "player_id" have the slot number in their "text".)
                    player_ids = [element.get("text") for element in data_array if element.get("type") == "player_id"]
                    # Check how many player ids were found
                    if len(player_ids) == 2:
                        sender_slot_id = player_ids[0]
                        receiver_slot_id = player_ids[1]
                    elif len(player_ids) == 1:
                        sender_slot_id = player_ids[0]
                        receiver_slot_id = player_ids[0]
                    else:
                        print("Unexpected number of player IDs found:", player_ids)
                        return

                    sender_name = main.slot_mapping.get(sender_slot_id, {}).get("slot_name", "Unknown")
                    sender_game = main.slot_mapping.get(sender_slot_id, {}).get("game", "Unknown")

                    receiver_name = main.slot_mapping.get(receiver_slot_id, {}).get("slot_name", "Unknown")
                    receiver_game = main.slot_mapping.get(receiver_slot_id, {}).get("game", "Unknown")

                    item_id = [element.get("text") for element in data_array if element.get("type") == "item_id"]
                    item_flag = [element.get("flags") for element in data_array if element.get("type") == "item_id"]
                    item_flag = int(item_flag[0] if item_flag else "None")
                    item_player = [element.get("player") for element in data_array if element.get("type") == "item_id"]

                    location_id = [element.get("text") for element in data_array if element.get("type") == "location_id"]
                    location_player = [element.get("player") for element in data_array if element.get("type") == "location_id"]

                    os.makedirs("data", exist_ok=True)
                    data_package_json = os.path.join("data", "data_package.json")

                    with open(data_package_json, "r") as f:
                        data_package = json.load(f)

                    receiver_game_data = next((item for item in data_package if item["game"] == receiver_game), None)
                    sender_game_data = next((item for item in data_package if item["game"] == sender_game), None)

                    if receiver_game_data:
                        # Reverse the mapping: {id: name}
                        reverse_item_mapping = {v: k for k, v in receiver_game_data["item_name_to_id"].items()}
                        # Convert item_id[0] to an integer if it's not already
                        try:
                            item_id_value = int(item_id[0])
                        except ValueError:
                            item_id_value = item_id[0]
                        item_name = reverse_item_mapping.get(item_id_value, "Unknown")
                    else:
                        item_name = "Unknown"

                    if sender_game_data:
                        # Reverse the mapping for locations
                        reverse_location_mapping = {v: k for k, v in sender_game_data["location_name_to_id"].items()}
                        try:
                            location_id_value = int(location_id[0])
                        except ValueError:
                            location_id_value = location_id[0]
                        location_name = reverse_location_mapping.get(location_id_value, "Unknown")
                    else:
                        location_name = "Unknown"

                    print(f"Sender: {sender_name}, Receiver: {receiver_name}, Item: {item_name}, Location: {location_name}, Flag: {item_flag}")

                    # Store the packet data
                    os.makedirs("data", exist_ok=True)
                    items_received_json = os.path.join("data", "items_received.json")

                    # Load existing data, or initialize if file doesn't exist
                    if os.path.exists(items_received_json):
                        with open(items_received_json, "r") as f:
                            try:
                                data = json.load(f)
                            except json.JSONDecodeError:
                                data = {}
                    else:
                        data = {}

                    # If sender key doesn't exist, create it as an empty dict
                    if receiver_name not in data:
                        data[receiver_name] = {}

                    # Determine next index as a string (e.g., "0", "1", ...)
                    next_index = str(len(data[receiver_name]))

                    # Append the new entry
                    data[receiver_name][next_index] = {
                        "item": item_name,
                        "flag": str(item_flag),
                        "location": location_name,
                        "sending_player": sender_name
                    }

                    # Write the updated data back to the JSON file
                    with open(items_received_json, "w") as f:
                        json.dump(data, f, indent=4)



            elif msg.get("cmd") == "ConnectionRefused":
                # AP sends a list of error strings (e.g. ["InvalidSlot"]); fall back
                # to the older singular field just in case.
                errors = msg.get("errors") or msg.get("error") or ["Unknown reason"]
                if isinstance(errors, list):
                    reason = ", ".join(str(e) for e in errors)
                else:
                    reason = str(errors)
                print("Connection refused. Reason:", reason)
                intentional_close = True
                await discord_ack.edit_original_response(
                    content=f"The server refused the connection: {reason}. Check the slot name and password."
                )
                await disconnect(websocket)
                is_websocket_connected = False
            elif msg.get("cmd") == "Bounced":
                print("Boing!")
            else:
                print(f"Received unknown packet: {msg}")

            # Once we have the slot info and every game's data package, we're done.
            await finish_data_collection_if_complete(discord_ack, websocket)
            print(f"Queue size: {packet_queue.qsize()}")
    except Exception as e:
        print(f"An error occurred while processing the packet: {e}")
        await discord_ack.edit_original_response(content=f"An error occurred while processing the packet: {e}")


async def finish_data_collection_if_complete(discord_ack, websocket):
    """Disconnect once the bot has everything a get_server_data run needs: the slot
    info from the Connected packet and a data package for every game in the room."""
    global intentional_close
    if intentional_close:
        return
    if not (room_info_received and connected_received):
        return
    if received_data_packages < expected_data_packages:
        return

    intentional_close = True
    await discord_ack.edit_original_response(
        content=f"Collected all server data ({received_data_packages} game data package(s)). "
                f"Disconnecting -- you can now use the other commands."
    )
    await disconnect(websocket)



async def main(discord_ack, address="archipelago.gg:38281", slot_name="island_bot", password=""):

    global is_websocket_connected, auto_reconnect
    global room_info_received, connected_received, expected_data_packages, received_data_packages, intentional_close
    auto_reconnect = False

    # Reset one-shot data-collection progress for this run.
    room_info_received = False
    connected_received = False
    expected_data_packages = 0
    received_data_packages = 0
    intentional_close = False

    # Normalize the address: trim whitespace and strip any scheme the user pasted
    # (e.g. "https://host:port" -> "host:port") so we don't build "ws://https://...",
    # which would make the resolver try to look up a host literally named "https".
    cleaned = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", address.strip())

    # read_response is a persistent tasks.loop that drains the shared packet queue.
    # Start it once -- starting an already-running loop raises RuntimeError, which
    # would otherwise blow up the second time get_server_data is invoked.
    if not read_response.is_running():
        read_response.start()

    # Prefer a plaintext (ws://) connection, but fall back to TLS (wss://) when the
    # server speaks TLS to our handshake (raising InvalidMessage: "did not receive a
    # valid HTTP response"). Servers behind a reverse proxy commonly require wss://.
    schemes = ["ws", "wss"]

    while True:
        connection_error = None

        for scheme in schemes:
            uri = f"{scheme}://{cleaned}"
            try:
                async with websockets.connect(uri) as websocket:
                    print(f"Connected to {uri}")
                    schemes = [scheme]  # pin the working scheme for any reconnects
                    is_websocket_connected = True
                    # Send initial connection payload
                    await send_connect_packet(websocket, slot_name, password)

                    # Run tasks concurrently; these stop when the connection closes.
                    await asyncio.gather(
                        handle_messages(discord_ack, websocket),
                        send_hello(websocket),
                        check_connection(websocket, slot_name, password)
                    )
                # The session ended (socket closed); stop trying other schemes.
                connection_error = None
                break
            except websockets.InvalidMessage:
                # Handshake wasn't valid HTTP -- usually a scheme mismatch, e.g. a
                # plaintext ws:// handshake against a TLS-only endpoint. Try the next.
                print(f"{scheme}:// handshake failed; trying the next scheme.")
                connection_error = "handshake failed"
                continue
            except websockets.ConnectionClosed:
                print("Websocket connection closed.")
                is_websocket_connected = False
                connection_error = None
                break
            except Exception as e:
                print(f"Failed to connect via {scheme}://: {e}")
                connection_error = e
                continue

        is_websocket_connected = False

        if connection_error is not None:
            # Every scheme failed to establish a connection.
            await discord_ack.edit_original_response(
                content="Could not connect to the server. Double-check the address and port, then try again."
            )
            break

        # The connection closed cleanly. Reconnect only if it was requested.
        if not auto_reconnect:
            break

        print("Attempting to reconnect in 5 seconds...")
        await asyncio.sleep(5)