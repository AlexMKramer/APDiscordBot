import time

import discord
from discord.ext import commands, tasks
from discord import option
import asyncio
import dotenv
import os
import datetime
import ap_connector
import json
import tracker_download

dotenv.load_dotenv()
discord_token = os.getenv("DISCORD_TOKEN")
discord_channel_id = os.getenv("DISCORD_CHANNEL_ID")
tracker_url = os.getenv("TRACKER_URL")
url_auth_username = os.getenv("URL_AUTH_USERNAME")
tracker_password = os.getenv("URL_AUTH_PASSWORD")

if url_auth_username and tracker_password:
    auth = (url_auth_username, tracker_password)
else:
    auth = None

# This enables users to interact with our bot as soon as it connects to the server.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='/', intents=intents)
bot.auto_sync_commands = True


@bot.event
async def on_connect():
    global tracker_url, auth
    if bot.auto_sync_commands:
        await bot.sync_commands()
    print(f'Logged in as {bot.user.name}')

    print("Starting user item tracker loop.")
    bot.loop.create_task(check_tracked_items_loop())

    print("Starting system item tracker loop.")
    channel = bot.get_channel(int(discord_channel_id))
    if channel is None:
        print(f"Channel with ID {discord_channel_id} not found.")
        print("No Discord channel ID provided. System item tracker will only send messages to users tracking items.")
        bot.loop.create_task(no_dm_tracker(tracker_url, auth))
    else:
        bot.loop.create_task(check_for_item_changes(tracker_url, auth, discord_channel_id))


@bot.event
async def on_disconnect():
    disconnect_time = datetime.datetime.now()
    print(f'{bot.user.name} failed to reconnect at {disconnect_time}')
    await asyncio.sleep(5)


async def game_name_autocomplete(ctx: discord.AutocompleteContext):
    game_names = []

    os.makedirs("data", exist_ok=True)
    data_package_json = os.path.join("data", "data_package.json")
    with open(data_package_json, "r") as f:
        data_package = json.load(f)

    for entry in data_package:
        game_name = entry.get("game")
        game_names.append(game_name)
    return [game_name for game_name in sorted(game_names) if game_name.lower().startswith(ctx.value.lower())]


async def items_autocomplete(ctx: discord.AutocompleteContext):
    selected_game = ctx.options.get("game_name")
    if not selected_game:
        return []  # No game selected yet, so no suggestions.

    os.makedirs("data", exist_ok=True)
    data_package_json = os.path.join("data", "data_package.json")
    with open(data_package_json, "r") as f:
        data_package = json.load(f)

    # Find the game data matching the selected game.
    game_data = None
    for entry in data_package:
        if entry.get("game", "") == selected_game:
            game_data = entry
            break

    if not game_data:
        return []  # No matching game found.

    # Get the list of item names from the game data
    item_names = sorted(list(game_data.get("item_name_to_id", {}).keys()))
    user_input = ctx.value or ""

    return [name for name in item_names if name.lower().startswith(ctx.value.lower())]


async def slot_name_autocomplete(ctx: discord.AutocompleteContext):

    os.makedirs("data", exist_ok=True)
    slot_info_json = os.path.join("data", "slot_info.json")
    with open(slot_info_json, "r") as f:
        slot_info = json.load(f)
    slot_names = sorted([info.get("slot_name") for info in slot_info.values()])
    return [name for name in slot_names if name.lower().startswith(ctx.value.lower())]


async def slot_name_for_assigned_slot_autocomplete(ctx: discord.AutocompleteContext):
    author_id = str(ctx.interaction.user.id)
    os.makedirs("data", exist_ok=True)
    listeners_file_json = os.path.join("data", "listeners.json")
    with open(listeners_file_json, "r") as f:
        try:
            listeners_data = json.load(f)
        except json.JSONDecodeError:
            listeners_data = {}

    if author_id not in listeners_data:
        return []  # No assignments for this user

    assignments = listeners_data[author_id]
    slot_names = sorted([assignment.get("slot_name") for assignment in assignments])
    return [name for name in slot_names if name.lower().startswith(ctx.value.lower())]


async def slot_name_for_game_autocomplete(ctx: discord.AutocompleteContext):
    game_name = ctx.options.get("game_name")

    os.makedirs("data", exist_ok=True)
    slot_info_json = os.path.join("data", "slot_info.json")
    with open(slot_info_json, "r") as f:
        slot_info = json.load(f)

    slot_names = [info.get("slot_name") for info in slot_info.values() if info.get("game") == game_name]
    return [name for name in slot_names if name.startswith(ctx.value)]


async def slot_name_for_assigned_game_autocomplete(ctx: discord.AutocompleteContext):
    # Get the user's ID
    author_id = str(ctx.interaction.user.id)
    # Ensure the data directory exists
    os.makedirs("data", exist_ok=True)
    listeners_file_json = os.path.join("data", "listeners.json")

    # Load the listeners file
    try:
        with open(listeners_file_json, "r") as f:
            listeners_data = json.load(f)
    except json.JSONDecodeError:
        listeners_data = {}

    # If the user has no assignments, return an empty list
    if author_id not in listeners_data:
        return []

    # Get all assignments for the user
    assignments = listeners_data[author_id]
    # Get the game name from the command options; it must be provided on your slash command.
    game_name = ctx.options.get("game_name")

    # Filter assignments to only those matching the specified game (case-insensitive)
    filtered_assignments = [
        assignment for assignment in assignments
        if assignment.get("game", "").lower() == game_name.lower()
    ]

    # Extract slot names from the filtered assignments (ignoring missing names)
    slot_names = sorted([assignment.get("slot_name") for assignment in filtered_assignments if assignment.get("slot_name")])

    # Filter suggestions based on the current autocomplete input (case-insensitive prefix match)
    current_input = ctx.value or ""
    suggestions = [name for name in slot_names if name.lower().startswith(current_input.lower())]
    return suggestions


@bot.slash_command(description="Enter the server address, the bot's slot name, and the password to connect to a server.")
@option("server_address", description="Enter the server address and port.", required = True)
@option("slot_name", description="Enter the bot's slot name.", required = True)
@option("password", description="Enter the server password.", required = False)
async def get_server_data(ctx, server_address: str, slot_name: str, password: str = None):
    initial_response = await ctx.respond("Connecting to server...")
    await ap_connector.main(initial_response, server_address, slot_name, password)
    await initial_response.edit_original_response(ap_connector.is_websocket_connected)


@bot.slash_command(description="Assign your discord account to a slot name.")
@option("slot_name", description="Enter your slot name.", autocomplete = slot_name_autocomplete, required=True)
async def assign_slot(ctx, slot_name: str):
    initial_response = await ctx.respond("Assigning slot name...", ephemeral=True)

    os.makedirs("data", exist_ok=True)
    slot_info_json = os.path.join("data", "slot_info.json")
    with open(slot_info_json, "r") as f:
        slot_info = json.load(f)

    # Look for the slot entry by matching the slot_name (case-insensitive)
    slot_entry = None
    slot_number = None
    for key, info in slot_info.items():
        if info.get("slot_name", "").lower() == slot_name.lower():
            slot_entry = info
            slot_number = key
            break

    if slot_entry is None:
        await initial_response.edit_original_response(
            content=f"{slot_name} not found. Check spelling and try again."
        )
        return

    # Extract game name from the slot entry
    game_name = slot_entry.get("game", "Unknown")

    # Prepare the assignment dictionary with slot number, slot name, game, and an empty list of items
    new_assignment = {
        "slot_number": slot_number,
        "slot_name": slot_entry.get("slot_name"),
        "game": game_name,
        "items": [],  # list to hold the items the user is tracking
        "tracked_items": []
    }

    author_id = str(ctx.author.id)

    os.makedirs("data", exist_ok=True)
    listeners_file_json = os.path.join("data", "listeners.json")

    # Load existing listeners data or initialize an empty dictionary
    if os.path.exists(listeners_file_json):
        with open(listeners_file_json, "r") as f:
            try:
                listeners_data = json.load(f)
            except json.JSONDecodeError:
                listeners_data = {}
    else:
        listeners_data = {}

    # Update the listeners data for this author.
    # Listeners_data will store a list of assignment dictionaries for each author.
    if author_id in listeners_data:
        # Check if the same slot is already assigned
        assignments = listeners_data[author_id]
        already_assigned = any(
            assignment.get("slot_name", "").lower() == slot_name.lower() for assignment in assignments
        )
        if already_assigned:
            await initial_response.edit_original_response(
                content=f"{slot_name} is already assigned to you."
            )
        else:
            assignments.append(new_assignment)
            await initial_response.edit_original_response(
                content=f"Assigned {slot_name} ({game_name}) to you."
            )
    else:
        listeners_data[author_id] = [new_assignment]
        await initial_response.edit_original_response(
            content=f"Assigned {slot_name} ({game_name}) to you."
        )

    # Save the updated listeners data back to listeners.json
    with open(listeners_file_json, "w") as outfile:
        json.dump(listeners_data, outfile, indent=4)

    outfile.close()


@bot.slash_command(description="Get a DM with a list of all items received for a slot.")
@option("slot_name", description="Enter your slot name.", autocomplete = slot_name_autocomplete, required=True)
async def get_items_for_slot(ctx, slot_name: str):
    # Send an initial ephemeral response to indicate processing.
    initial_response = await ctx.respond(content="Getting items...", ephemeral=True)

    # Build a minimal assignment dictionary using the provided slot name.
    assignment = {
        "slot_name": slot_name,
        "game": "Unknown"  # Default value; adjust if game info is available.
    }

    # Get the message built by send_items for this assignment.
    message = await send_items(ctx, assignment, initial_response)

    # Use plain code block formatting (without "ansi").
    wrapper_length = len("``ansi`\n") + len("\n```")
    max_content_length = 1950 - wrapper_length

    # Break the message into chunks that fit within Discord's limits.
    chunks = chunk_text_by_line(message, max_content_length)

    try:
        # DM each chunk to the user.
        for chunk in chunks:
            await ctx.author.send(f"```ansi\n{chunk}\n```")
        await initial_response.edit_original_response(
            content="I've sent you a DM with a list of items for the specified slot."
        )
    except discord.Forbidden:
        await initial_response.edit_original_response(
            content="I couldn't send you a DM. Please check your DM settings."
        )


@bot.slash_command(description="Get a DM with only the new items received for your assigned games.")
async def get_all_new_items(ctx):
    listeners_file = os.path.join("data", "listeners.json")
    items_received_file = os.path.join("data", "items_received.json")

    # Load listeners data.
    if not os.path.exists(listeners_file):
        await ctx.respond("You have no assignments.", ephemeral=True)
        return
    with open(listeners_file, "r") as f:
        try:
            listeners_data = json.load(f)
        except json.JSONDecodeError:
            await ctx.respond("Error reading listeners file.", ephemeral=True)
            return

    author_id = str(ctx.author.id)
    if author_id not in listeners_data:
        await ctx.respond("You have no assignments.", ephemeral=True)
        return

    # Load items_received data.
    if not os.path.exists(items_received_file):
        await ctx.respond("No items received data available.", ephemeral=True)
        return
    with open(items_received_file, "r") as f:
        try:
            items_received = json.load(f)
        except json.JSONDecodeError:
            await ctx.respond("Error reading items received file.", ephemeral=True)
            return

    diff_message_lines = []
    updated = False
    assignments = listeners_data[author_id]

    for assignment in assignments:
        slot_name = assignment.get("slot_name", "Unknown")
        # Locate the corresponding slot data in items_received.
        slot_data = None
        for slot_num, slot_entry in items_received.items():
            if slot_name in slot_entry:
                slot_data = slot_entry[slot_name]
                break
        if slot_data is None:
            continue

        items_dict = slot_data.get("Items", {})
        agg_new = {}
        if isinstance(items_dict, dict):
            for key, item_info in items_dict.items():
                name = item_info.get("item_name", "Unknown")
                try:
                    count = int(item_info.get("amount", 0))
                except Exception:
                    count = 0
                agg_new[name] = agg_new.get(name, 0) + count

        # "seen" items are stored under the "items" key in the assignment.
        seen_items = assignment.get("items", {})
        if not isinstance(seen_items, dict):
            seen_items = {}

        diff_items = {}
        for item_name, new_total in agg_new.items():
            seen_total = seen_items.get(item_name, 0)
            if new_total > seen_total:
                diff_items[item_name] = new_total - seen_total

        if diff_items:
            # Underline the slot name using ANSI escape sequences
            underline_start = "[4;2m"
            underline_end = "[0m"

            header = f"{underline_start}Items received for {slot_name}:{underline_end}"

            diff_message_lines.append(header)
            for item_name, diff_amount in diff_items.items():
                diff_message_lines.append(f"{item_name} +{diff_amount}")
            diff_message_lines.append("")  # blank line for separation
            # Update seen items to the current aggregated totals.
            for item_name, new_total in agg_new.items():
                seen_items[item_name] = new_total
            assignment["items"] = seen_items
            updated = True

    if updated:
        with open(listeners_file, "w") as f:
            json.dump(listeners_data, f, indent=4)

    if not diff_message_lines:
        diff_message = "No new items received."
    else:
        diff_message = "\n".join(diff_message_lines)

    # Chunk the diff message.
    wrapper_length = len("```ansi\n") + len("\n```")
    max_message_length = 1950 - wrapper_length
    chunks = chunk_text_by_line(diff_message, max_message_length)

    await ctx.respond("I've sent you a DM with your new items for all your assigned games.", ephemeral=True)
    try:
        for chunk in chunks:
            await ctx.author.send(f"```ansi\n{chunk}\n```")
    except discord.Forbidden:
        await ctx.respond("I couldn't send you a DM. Please check your DM settings.", ephemeral=True)


@bot.slash_command(description="Get a DM with new items received for a specified slot.")
@option("slot_name", description="Enter your slot name.", autocomplete = slot_name_for_assigned_slot_autocomplete, required=True)
async def get_new_items_for_slot(ctx, slot_name: str):
    listeners_file = os.path.join("data", "listeners.json")
    items_received_file = os.path.join("data", "items_received.json")

    # Load listeners data.
    if not os.path.exists(listeners_file):
        await ctx.respond("You have no assignments.", ephemeral=True)
        return
    with open(listeners_file, "r") as f:
        try:
            listeners_data = json.load(f)
        except json.JSONDecodeError:
            await ctx.respond("Error reading listeners file.", ephemeral=True)
            return

    author_id = str(ctx.author.id)
    if author_id not in listeners_data:
        await ctx.respond("You have no assignments.", ephemeral=True)
        return

    # Load items_received data.
    if not os.path.exists(items_received_file):
        await ctx.respond("No items received data available.", ephemeral=True)
        return
    with open(items_received_file, "r") as f:
        try:
            items_received = json.load(f)
        except json.JSONDecodeError:
            await ctx.respond("Error reading items received file.", ephemeral=True)
            return

    diff_message_lines = []
    updated = False
    assignments = listeners_data[author_id]
    for assignment in assignments:
        assigned_slot = assignment.get("slot_name", "")
        if assigned_slot.lower() != slot_name.lower():
            continue

        # Locate the corresponding slot data in items_received.
        slot_data = None
        for slot_num, slot_entry in items_received.items():
            if assigned_slot in slot_entry:
                slot_data = slot_entry[assigned_slot]
                break
        if slot_data is None:
            continue

        items_dict = slot_data.get("Items", {})
        agg_new = {}
        if isinstance(items_dict, dict):
            for key, item_info in items_dict.items():
                name = item_info.get("item_name", "Unknown")
                try:
                    count = int(item_info.get("amount", 0))
                except Exception:
                    count = 0
                agg_new[name] = agg_new.get(name, 0) + count

        seen_items = assignment.get("items", {})
        if not isinstance(seen_items, dict):
            seen_items = {}

        diff_items = {}
        for item_name, new_total in agg_new.items():
            seen_total = seen_items.get(item_name, 0)
            if new_total > seen_total:
                diff_items[item_name] = new_total - seen_total

        if diff_items:

            # Underline the slot name using ANSI escape sequences
            underline_start = "[4;2m"
            underline_end = "[0m"

            header = f"{underline_start}Items received for {slot_name}:{underline_end}"

            diff_message_lines.append(header)
            for item_name, diff_amount in diff_items.items():
                diff_message_lines.append(f"{item_name} +{diff_amount}")
            diff_message_lines.append("")
            for item_name, new_total in agg_new.items():
                seen_items[item_name] = new_total
            assignment["items"] = seen_items
            updated = True

    if updated:
        with open(listeners_file, "w") as f:
            json.dump(listeners_data, f, indent=4)

    if not diff_message_lines:
        diff_message = f"No new items received for {slot_name}."
    else:
        diff_message = "\n".join(diff_message_lines)

    wrapper_length = len("```ansi\n") + len("\n```")
    max_message_length = 1950 - wrapper_length
    chunks = chunk_text_by_line(diff_message, max_message_length)

    await ctx.respond("I've sent you a DM with your new items for the specified slot.", ephemeral=True)
    try:
        for chunk in chunks:
            await ctx.author.send(f"```ansi\n{chunk}\n```")
    except discord.Forbidden:
        await ctx.respond("I couldn't send you a DM. Please check your DM settings.", ephemeral=True)


# Helper function to split text into chunks without breaking lines
def chunk_text_by_line(content, max_length):
    """
    Split content into chunks that do not exceed max_length.
    Splitting is done at newline boundaries so that lines are not broken.
    """
    lines = content.splitlines()
    chunks = []
    current_chunk = ""
    for line in lines:
        if not current_chunk:
            current_chunk = line
        else:
            if len(current_chunk) + 1 + len(line) > max_length:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += "\n" + line
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


# Updated send_items: searches for the assignment's slot in the new items_received.json format,
# and builds a plain-text message listing the items and their amounts.
async def send_items(ctx, assignment, initial_response):
    # Extract the slot name and game from the assignment dictionary
    slot_name = assignment.get("slot_name", "Unknown")
    game_name = assignment.get("game", "Unknown")

    # Load the items_received.json file
    try:
        os.makedirs("data", exist_ok=True)
        items_received_json = os.path.join("data", "items_received.json")
        with open(items_received_json, "r") as f:
            items_received = json.load(f)

    except Exception as e:
        await ctx.author.send(content=f"Error reading items file: {e}. Talk to the server admin for help.")
        return f"Error reading items file: {e}"

    # Look for the slot data by searching each slot number's entry for the matching slot name
    slot_data = None
    for slot_num, slot_entry in items_received.items():
        if slot_name in slot_entry:
            slot_data = slot_entry[slot_name]
            break

    if slot_data is None:
        message = f"No items found for slot: {slot_name}"
        return message

    items_dict = slot_data.get("Items", {})

    # Underline the slot name using ANSI escape sequences
    underline_start = "[4;2m"
    underline_end = "[0m"

    header = f"{underline_start}Items received for {slot_name}:{underline_end}"
    lines = [header]

    # Build a line for each received item
    for key, item_info in items_dict.items():
        item_name = item_info.get("item_name", "Unknown")
        amount = item_info.get("amount", "Unknown")
        if amount == 1:
            line = f"{item_name}"
        else:
            line = f"{item_name} x{amount}"
        lines.append(line)

    message = "\n".join(lines)
    return message


def format_diff_message(diff):
    lines = []

    # Underline the slot name using ANSI escape sequences
    underline_start = "[4;2m"
    underline_end = "[0m"

    for slot, slot_data in diff.items():
        for slot_name, details in slot_data.items():

            # Add a game completion message if the game status changed to completed.
            if "Goal Completed" in details:
                lines.append(f"{underline_start}{slot_name} Goal Completed!  All items released.{underline_end}")

            else:

                header = f"{underline_start}Items received for {slot_name}:{underline_end}"
                lines.append(header)

                new_items = details.get("New Items", {})
                for item_name, change in new_items.items():
                    lines.append(f"{item_name} +{change}")

            lines.append("")  # blank line for separation
    return "\n".join(lines)


async def no_dm_tracker(tracker_url, auth):
    while True:
        await tracker_download.get_all_tracker_received_items(tracker_url, auth)
        await asyncio.sleep(60)


# Loop function to check for changes every 60 seconds and send a DM to a specific channel.
async def check_for_item_changes(tracker_url, auth, channel_id):
    await bot.wait_until_ready()
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        print(f"Channel with ID {channel_id} not found.")
        return

    while not bot.is_closed():
        # Get the new diff from tracker data.
        diff = tracker_download.get_all_tracker_received_items(tracker_url, auth)
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        if diff:
            print(f"Changes found at {current_time}")
            message = format_diff_message(diff)
            # Prepare to send the message in a code block.
            # Adjust the maximum content length to account for the code block wrappers.
            wrapper_length = len("```\n") + len("\n```")
            max_content_length = 1950 - wrapper_length
            chunks = chunk_text_by_line(message, max_content_length)
            for chunk in chunks:
                await channel.send(f"```ansi\n{chunk}\n```")
        else:
            print(f"No changes found at {current_time}")

        # Wait 60 seconds before checking again.
        await asyncio.sleep(60)


# Slash command to DM the user a list of all items for their tracked slots.
@bot.slash_command(description="Get a list of all items for your tracked slots.")
async def get_all_tracked_items(ctx):
    # Send an initial ephemeral response to let the user know we are processing.
    initial_response = await ctx.respond("Getting items...", ephemeral=True)
    author_id = str(ctx.author.id)

    os.makedirs("data", exist_ok=True)
    listeners_data_json = os.path.join("data", "listeners.json")

    if not os.path.exists(listeners_data_json):
        await initial_response.edit_original_response(
            content="Sorry, you aren't tracking any slots. Run '/assign_slot' to track a slot."
        )
        return

    # Load listeners.json data.
    with open(listeners_data_json, "r") as f:
        try:
            listeners_data = json.load(f)
        except json.JSONDecodeError:
            await initial_response.edit_original_response(
                content="Error reading listeners file."
            )
            return

    if author_id not in listeners_data:
        await initial_response.edit_original_response(
            content="Sorry, you aren't tracking any slots. Run '/assign_slot' to track a slot."
        )
        return

    tracked_assignments = listeners_data[author_id]
    combined_message = ""
    for assignment in tracked_assignments:
        message = await send_items(ctx, assignment, initial_response)
        combined_message += f"\n{message}\n"

    # Prepare the message for DM; use plain text code blocks.
    wrapper_length = len("```ansi\n") + len("\n```")
    max_content_length = 1950 - wrapper_length
    chunks = chunk_text_by_line(combined_message, max_content_length)



    try:
        for chunk in chunks:
            await ctx.author.send(f"```ansi\n{chunk}\n```")
        await initial_response.edit_original_response(
            content="I've sent you a DM with a list of items for the slots you are tracking."
        )
    except discord.Forbidden:
        await initial_response.edit_original_response(
            content="I couldn't send you a DM. Please check your DM settings."
        )


@bot.slash_command(description="Enter the name of an item blocking your progress to get a DM when that item is found.")
@option('game_name', description="Enter the name of the game.", autocomplete=game_name_autocomplete, required=True)
@option("item_name", description="Enter the name of the item.", autocomplete=items_autocomplete, required=True)
@option("slot_name", description="Enter your slot name.", autocomplete=slot_name_for_assigned_game_autocomplete, required=True)
@option("target_amount", description="Enter the number of items you are tracking for.", required=True)
async def track_item(ctx, game_name: str, item_name: str, slot_name: str, target_amount: int):
    initial_response = await ctx.respond("tracking item...", ephemeral=True)

    os.makedirs("data", exist_ok=True)
    listeners_data_json = os.path.join("data", "listeners.json")

    # Load existing listeners data (or initialize if not present)
    if os.path.exists(listeners_data_json):
        with open(listeners_data_json, "r") as f:
            try:
                listeners_data = json.load(f)
            except json.JSONDecodeError:
                listeners_data = {}
    else:
        listeners_data = {}

    author_id = str(ctx.author.id)

    # Check if the user has any assignments
    if author_id not in listeners_data:
        await initial_response.edit_original_response(
            content="You haven't assigned any slots yet. Use the assign_slot command first."
        )
        return

    # Look for an assignment that matches the given game and slot (case-insensitive)
    user_assignments = listeners_data[author_id]
    matching_assignment = None
    for assignment in user_assignments:
        if assignment.get("game", "").lower() == game_name.lower() and assignment.get("slot_name", "").lower() == slot_name.lower():
            matching_assignment = assignment
            break

    if matching_assignment is None:
        await initial_response.edit_original_response(
            content=f"You don't have a slot assigned for game **{game_name}** with slot name **{slot_name}**. Please assign a slot for that game first."
        )
        return

    # Ensure the assignment has a "tracked_items" dictionary to store tracked items
    if "tracked_items" not in matching_assignment:
        matching_assignment["tracked_items"] = {}

    # Check if the item is already being tracked
    if item_name in matching_assignment["tracked_items"]:
        if "target" in matching_assignment["tracked_items"][item_name] == target_amount:
            await initial_response.edit_original_response(
                content=f"**{item_name}** is already being tracked for **{game_name}** under slot **{slot_name}**.  Current amount: {matching_assignment['tracked_items'][item_name]['current']}."
            )
            return
        elif "target" in matching_assignment["tracked_items"][item_name] != target_amount:
            await initial_response.edit_original_response(
                content=f"Now tracking **{item_name}** (target: {target_amount}) for **{game_name}** under slot **{slot_name}**.  Current amount: {matching_assignment['tracked_items'][item_name]['current']}."
            )
            matching_assignment["tracked_items"][item_name] = {"target": target_amount, "current": {matching_assignment['tracked_items'][item_name]['current']}}
    else:
        # Add the new item to track with its target amount and initial count of 0
        matching_assignment["tracked_items"][item_name] = {"target": target_amount, "current": 0}

        # Save the updated listeners data back to file
        with open(listeners_data_json, "w") as f:
            json.dump(listeners_data, f, indent=4)

        await initial_response.edit_original_response(
            content=f"Now tracking **{item_name}** (target: {target_amount}) for **{game_name}** under slot **{slot_name}**."
        )


async def check_tracked_items_loop():
    await bot.wait_until_ready()  # Ensure the bot is ready before starting the loop

    os.makedirs("data", exist_ok=True)
    listeners_data_json = os.path.join("data", "listeners.json")
    items_received_json = os.path.join("data", "items_received.json")


    while not bot.is_closed():
        # Load items_received.json
        try:
            with open(items_received_json, "r") as f:
                items_received = json.load(f)
        except Exception as e:
            print(f"Error loading {items_received_json}: {e}")
            await asyncio.sleep(10)
            continue

        # Load listeners.json (the tracking assignments)
        if os.path.exists(listeners_data_json):
            with open(listeners_data_json, "r") as f:
                try:
                    listeners_data = json.load(f)
                except json.JSONDecodeError:
                    listeners_data = {}
        else:
            listeners_data = {}

        any_update = False  # Flag to determine if we need to update the file

        # Iterate over each user (by author ID) in listeners_data
        for user_id, assignments in listeners_data.items():
            user_messages = []  # Collect messages for the user across assignments
            for assignment in assignments:
                slot_name = assignment.get("slot_name")
                tracked_items = assignment.get("tracked_items", {})

                # If tracked_items is a list (from an older structure), convert it to a dictionary.
                if isinstance(tracked_items, list):
                    new_tracked = {}
                    for item in tracked_items:
                        # Set a default target amount of 1 (or adjust as needed)
                        new_tracked[item] = {"target": 1, "current": 0}
                    assignment["tracked_items"] = new_tracked
                    tracked_items = new_tracked

                # Find the corresponding slot in items_received.json.
                slot_items_data = None
                for slot_num, slot_data in items_received.items():
                    if slot_name in slot_data:
                        slot_items_data = slot_data[slot_name]
                        break

                if slot_items_data:
                    items_dict = slot_items_data.get("Items", {})
                    # For each tracked item, calculate the total received amount.
                    items_to_remove = []
                    for tracked_item, tracking_info in tracked_items.items():
                        target = tracking_info.get("target", 0)
                        current = tracking_info.get("current", 0)
                        total_received = 0

                        # Sum amounts for matching tracked_item in the received items
                        for key, item_info in items_dict.items():
                            if item_info.get("item_name", "").lower() == tracked_item.lower():
                                try:
                                    amt = int(item_info.get("amount", 0))
                                except ValueError:
                                    amt = 0
                                total_received += amt

                        if total_received > current:
                            new_count = total_received - current
                            tracking_info["current"] = total_received
                            if total_received >= target:
                                user_messages.append(
                                    f"Your tracked item **{tracked_item}** has reached the target ({total_received}/{target}) for slot **{slot_name}** in game **{assignment.get('game', 'Unknown')}**. Tracking for this item is now complete."
                                )
                                items_to_remove.append(tracked_item)
                            else:
                                user_messages.append(
                                    f"You received **{new_count}** new **{tracked_item}** (total: {total_received}/{target}) for slot **{slot_name}** in game **{assignment.get('game', 'Unknown')}**."
                                )
                    # Remove items that have reached or exceeded the target from tracking
                    for item in items_to_remove:
                        tracked_items.pop(item, None)
                    any_update = True

            # DM the user if there are any messages
            if user_messages:
                try:
                    user_obj = await bot.fetch_user(int(user_id))
                    if user_obj:
                        await user_obj.send("\n".join(user_messages))
                except discord.Forbidden:
                    print(f"Could not DM user {user_id}. They might have DMs disabled.")

        # If any updates were made, save the updated listeners data back to file
        if any_update:
            with open(listeners_data_json, "w") as f:
                json.dump(listeners_data, f, indent=4)

        # Wait a while before checking again (adjust the sleep time as needed)
        await asyncio.sleep(10)


if __name__ == "__main__":
    bot.run(discord_token)