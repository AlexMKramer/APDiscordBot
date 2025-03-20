import asyncio
import json
import time
from json import JSONEncoder, JSONDecoder
import os
from time import sleep

from discord.ext import tasks
import websockets

is_websocket_connected = False
auto_reconnect = False
packet_queue = asyncio.Queue()

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
async def get_data_package(websocket, games):
    payload = [{
        'cmd': 'GetDataPackage',
        'games': games
    }]
    await send(websocket, payload)


# Process data packages
async def process_data_package(data_package):

    # print(f"Processing data package: {data_package}")
    # Extract the games dictionary; if missing, use an empty dict.
    games = data_package.get('games', {})
    result = []

    # Iterate over the sorted game names
    for game_name in sorted(games.keys()):
        game_data = games[game_name]

        # Get items and locations; default to empty dicts if keys are missing.
        items = game_data.get('item_name_to_id', {})
        locations = game_data.get('location_name_to_id', {})

        # Sort the inner dictionaries by key (optional)
        sorted_items = dict(sorted(items.items()))
        sorted_locations = dict(sorted(locations.items()))

        # Append the formatted game data to the result list
        result.append({
            "game": game_name,
            "item_name_to_id": sorted_items,
            "location_name_to_id": sorted_locations
        })
    json_result = json.dumps(result, indent=4)

    os.makedirs("data", exist_ok=True)
    data_package_json = os.path.join("data", "data_package.json")
    # Write the processed data package to data_package.json
    with open(data_package_json, "w") as outfile:
        outfile.write(json_result)

    outfile.close()

    # print(json_result)
    return result


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
        await discord_ack.edit_original_response(content="Connection was closed.")
        print("Connection was closed.")

async def add_packet_to_queue(discord_ack, websocket, packet):
    await packet_queue.put((discord_ack, websocket, packet))
    print(f"Added a packet to the queue. Queue size: {packet_queue.qsize()}")


# Read the provided packet and process it by type
@tasks.loop(seconds=1)
async def read_response():
    global is_websocket_connected, auto_reconnect
    try:
        if not packet_queue.empty():
            discord_ack, websocket, msg = await packet_queue.get()
            if msg.get("cmd") == "Connected":
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
                await get_data_package(websocket, games_in_server)

            elif msg.get("cmd") == "DataPackage":
                print("Got a data package")

                main.data_package_mapping = await process_data_package(msg["data"])

                # close the connection
                time.sleep(5)

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
                refusal_reason = msg.get("error", "Unknown reason")
                print("Connection refused. Reason:", refusal_reason)
                await disconnect(websocket)
                is_websocket_connected = False
            elif msg.get("cmd") == "Bounced":
                print("Boing!")
            else:
                print(f"Received unknown packet: {msg}")
            print(f"Queue size: {packet_queue.qsize()}")
    except Exception as e:
        print(f"An error occurred while processing the packet: {e}")
        await discord_ack.edit_original_response(content=f"An error occurred while processing the packet: {e}")



async def main(discord_ack, address="archipelago.gg:38281", slot_name="island_bot", password=""):

    data_package_mapping = []
    reverse_item = {}
    reverse_location = {}
    players_mapping = {}
    player_received_items = {}

    global is_websocket_connected, auto_reconnect
    auto_reconnect = False

    while True:
        try:
            async with websockets.connect(f"ws://{address}") as websocket:
                is_websocket_connected = True
                # Send initial connection payload
                await send_connect_packet(websocket, slot_name, password)

                # Run tasks concurrently; these will stop if the connection closes or an error occurs.
                await asyncio.gather(
                    handle_messages(discord_ack, websocket),
                    read_response.start(),
                    send_hello(websocket),
                    check_connection(websocket, slot_name, password)
                )
        except websockets.ConnectionClosed:
            print("Websocket connection closed.")
            is_websocket_connected = False
            # If auto_reconnect is not enabled, break out of the loop.
            if not auto_reconnect:
                break
            else:
                print("Attempting to reconnect in 5 seconds...")
                await asyncio.sleep(5)
        except Exception as e:
            print(f"Unexpected error: {e}")
            is_websocket_connected = False
            break