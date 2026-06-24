import time

import discord
from discord.ext import commands, tasks
from discord import option
import asyncio
import dotenv
import os
import datetime
import fnmatch
import ap_connector
import json
import traceback
import tracker_download
import gomode_bot

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

# Discord user allowed to run owner-only commands (e.g. /register_seed). If unset, the
# bot falls back to the guild owner.
gomode_owner_id = os.getenv("GOMODE_OWNER_ID") or os.getenv("OWNER_ID")

# Guard so on_connect (which can fire on every reconnect) starts the background loops only once.
background_tasks_started = False


def is_owner(ctx) -> bool:
    if gomode_owner_id:
        return str(ctx.author.id) == str(gomode_owner_id)
    guild = getattr(ctx, "guild", None)
    return bool(guild and ctx.author.id == guild.owner_id)

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

    # on_connect fires on EVERY gateway (re)connect; start the background loops exactly once.
    # Otherwise each reconnect spawns another copy of each loop -> duplicate DMs / channel
    # posts, overlapping tracker scrapes piling up on the thread pool, and racing writes to
    # the JSON data files.
    global background_tasks_started
    if background_tasks_started:
        return
    background_tasks_started = True

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

    print("Starting go-mode notification loop.")
    bot.loop.create_task(check_go_mode_loop())


@bot.event
async def on_disconnect():
    # Fires on every gateway disconnect. py-cord auto-reconnects (and resumes the session),
    # so this is informational, NOT a failure -- the old "failed to reconnect" wording was
    # misleading. A matching on_resumed/on_connect line confirms the reconnect.
    print(f'Disconnected from the Discord gateway at {datetime.datetime.now()} (will auto-reconnect).')


@bot.event
async def on_resumed():
    print(f'Resumed the Discord gateway session at {datetime.datetime.now()}.')


@bot.event
async def on_application_command_error(ctx, error):
    # A genuinely-expired interaction (10062) is usually a transient hiccup and not actionable
    # -- log one line instead of a full traceback. Everything else keeps the default traceback.
    original = getattr(error, "original", error)
    if isinstance(original, discord.NotFound) and getattr(original, "code", None) == 10062:
        print(f"[interaction] command '{getattr(ctx.command, 'qualified_name', '?')}' "
              f"expired before the bot could respond (10062); ignoring.")
        return
    traceback.print_exception(type(error), error, error.__traceback__)


# data_package.json is large (~1.5 MB) and the two autocompletes below fire on every
# keystroke, so parse it at most once per change (cached by mtime) instead of re-reading +
# re-parsing on the event loop each time. Returns the last good value on a read/parse error
# (e.g. the file being mid-rewrite by ap_connector), so a keystroke can never crash the
# autocomplete on a partial file.
_data_package_cache = {"mtime": None, "data": []}


def _load_data_package():
    path = os.path.join("data", "data_package.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _data_package_cache["data"]
    if _data_package_cache["mtime"] != mtime:
        try:
            with open(path, "r") as f:
                _data_package_cache["data"] = json.load(f)
            _data_package_cache["mtime"] = mtime
        except (OSError, json.JSONDecodeError):
            return _data_package_cache["data"]  # keep serving the last good parse
    return _data_package_cache["data"]


async def game_name_autocomplete(ctx: discord.AutocompleteContext):
    data_package = _load_data_package()
    game_names = [entry.get("game") for entry in data_package if entry.get("game")]
    return [game_name for game_name in sorted(game_names) if game_name.lower().startswith(ctx.value.lower())]


async def items_autocomplete(ctx: discord.AutocompleteContext):
    selected_game = ctx.options.get("game_name")
    if not selected_game:
        return []  # No game selected yet, so no suggestions.

    # Find the game data matching the selected game.
    game_data = None
    for entry in _load_data_package():
        if entry.get("game", "") == selected_game:
            game_data = entry
            break

    if not game_data:
        return []  # No matching game found.

    # Get the list of item names from the game data
    item_names = sorted(list(game_data.get("item_name_to_id", {}).keys()))
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

    # ap_connector.main runs the websocket session, which stays open while connected.
    # Launch it in the background instead of awaiting it here -- awaiting would block
    # the command callback forever. The connector edits initial_response itself to
    # report "Connected to the server" or any error.
    async def run_connection():
        try:
            await ap_connector.main(initial_response, server_address, slot_name, password)
        except Exception as e:
            try:
                await initial_response.edit_original_response(content=f"Failed to connect: {e}")
            except Exception:
                pass

    bot.loop.create_task(run_connection())


@bot.slash_command(description="(Owner) Register the current seed to enable go-mode tracking for its players.")
@option("seed_file", description="The generated AP_<seed>.zip (multidata + spoiler).", required=False)
@option("server_path", description="Or: the path to a seed zip already on the bot server (e.g. via FTP).", required=False)
async def register_seed(ctx, seed_file: discord.Attachment = None, server_path: str = None):
    if not is_owner(ctx):
        await ctx.respond("Only the server owner can register a seed.", ephemeral=True)
        return

    ok, why = gomode_bot.is_configured()
    if not ok:
        await ctx.respond(f"Go-mode tracking isn't configured yet: {why}", ephemeral=True)
        return

    if not seed_file and not server_path:
        await ctx.respond(
            "Attach the seed's generation zip, or pass `server_path` to one already on the server.",
            ephemeral=True,
        )
        return

    initial_response = await ctx.respond("Registering seed...", ephemeral=True)

    # Resolve the seed zip: a small Discord attachment, or a server-side path (large apworlds
    # stay on the server; only this ~1-2 MB zip ever travels through Discord).
    if seed_file is not None:
        seeds_dir = os.path.join(gomode_bot.RUNTIME_DIR, "seeds")
        os.makedirs(seeds_dir, exist_ok=True)
        # Discord doesn't guarantee a safe basename; strip any path components so a crafted
        # filename can't escape seeds_dir (e.g. "..\\..\\main.py" or an absolute path).
        safe_name = os.path.basename(seed_file.filename)
        if not safe_name or safe_name in (".", ".."):
            safe_name = "seed.zip"
        zip_path = os.path.join(seeds_dir, safe_name)
        try:
            await seed_file.save(zip_path)
        except Exception as e:
            await initial_response.edit_original_response(content=f"Couldn't download the attachment: {e}")
            return
    else:
        zip_path = server_path
        if not os.path.isfile(zip_path):
            await initial_response.edit_original_response(content=f"No file found at `{zip_path}` on the bot server.")
            return

    async def say(msg):
        try:
            await initial_response.edit_original_response(content=msg)
        except Exception:
            pass  # interaction token may have expired on a long precompute; DM below is the fallback

    async def run():
        try:
            registry = await gomode_bot.register_seed(zip_path, progress=say)
        except Exception as e:
            # Mirror the success path's DM: a late failure (after the ephemeral token expired)
            # would otherwise be invisible, since say()'s edit silently no-ops.
            msg = f"Registration failed: {e}"
            await say(msg)
            try:
                await ctx.author.send(msg)
            except discord.Forbidden:
                pass
            return
        summary = (
            f"**Registered seed `{registry['seed']}`** (Archipelago {registry['version']}).\n"
            f"- {registry['slot_count']} slots analyzed\n"
            f"- {registry['verified']} with a full requirement breakdown\n"
            f"- {registry['unsupported']} not supported (those players won't get go-mode tracking)\n"
            f"Players can now use `/items_to_go_mode`, and I'll DM them when they reach go mode."
        )
        await say(summary)
        # The precompute can run long enough to expire the ephemeral token; DM the owner so the
        # result is never lost.
        try:
            await ctx.author.send(summary)
        except discord.Forbidden:
            pass

    bot.loop.create_task(run())


def _load_listeners() -> dict:
    try:
        with open(os.path.join("data", "listeners.json"), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _go_mode_overview_line(name: str, st: dict) -> str:
    game = st.get("game") or ""
    tag = f" ({game})" if game else ""
    status = st.get("status")
    if status == "unregistered":
        return f"• {name}{tag} — not part of the registered seed"
    if status == "tracker_mismatch":
        return f"• {name}{tag} — tracker shows a different game; is the right seed registered?"
    if status == "unsupported":
        return f"• {name}{tag} — go-mode tracking not supported for this game"
    if status not in ("ok",):  # "error" or anything transient
        return f"• {name}{tag} — couldn't check right now, try again shortly"
    igm = st.get("in_go_mode")
    if igm is True:
        return f"• {name}{tag} — ✅ in go mode!"
    if igm is False:
        return f"• {name}{tag} — ⏳ not yet"
    return f"• {name}{tag} — (couldn't determine right now)"


async def _send_go_mode_detail(ctx, initial_response, slot_name: str):
    """Detailed 'what do I still need' for one slot, against current inventory."""
    inventory = gomode_bot.current_inventory(slot_name)
    res = await gomode_bot.analyze_slot_live(slot_name, inventory)
    if res is None:
        await initial_response.edit_original_response(
            content="No seed is registered yet. Ask the server owner to run /register_seed.")
        return

    game = res.get("game") or ""
    header = f"**{slot_name}**" + (f" ({game})" if game else "")
    status = res.get("status")
    if status == "unsupported":
        await initial_response.edit_original_response(
            content=f"{header}\nGo-mode tracking isn't supported for this game yet.")
        return
    if status != "ok":  # "error" -- a transient build/subprocess failure, not "unsupported"
        await initial_response.edit_original_response(
            content=f"{header}\nCouldn't check this slot right now — please try again shortly.")
        return
    if res.get("in_go_mode"):
        await initial_response.edit_original_response(
            content=f"{header}\n🎉 You're in go mode — you have everything you need to finish!")
        return

    lines = res.get("requirements_text") or ["(no requirement details available)"]
    body = f"{header}\n" + "\n".join(lines)
    if len(body) <= 1900:
        await initial_response.edit_original_response(content=body)
        return
    # Long requirement trees: DM the full list, and only claim success if the DM actually sent
    # (catch HTTPException, not just Forbidden, so a rate-limit mid-stream can't escape).
    try:
        for chunk in chunk_text_by_line(body, 1900):
            await ctx.author.send(chunk)
        await initial_response.edit_original_response(
            content=f"{header}\nThe list is long — I've sent it to you in a DM.")
    except discord.Forbidden:
        await initial_response.edit_original_response(
            content=f"{header}\nThe list is long, but I couldn't DM you — please enable DMs.")
    except discord.HTTPException:
        await initial_response.edit_original_response(
            content=f"{header}\nThe list is long, but I hit an error sending the DM — please try again.")


@bot.slash_command(description="See what you still need to reach go mode for your assigned slots.")
@option("slot_name", description="A specific slot (leave blank to see all your slots).",
        autocomplete=slot_name_for_assigned_slot_autocomplete, required=False)
async def items_to_go_mode(ctx, slot_name: str = None):
    initial_response = await ctx.respond("Checking go-mode status...", ephemeral=True)

    if gomode_bot.load_registry() is None:
        await initial_response.edit_original_response(
            content="No seed is registered yet. Ask the server owner to run /register_seed.")
        return

    author_id = str(ctx.author.id)
    assignments = _load_listeners().get(author_id, [])
    my_slots = [a.get("slot_name") for a in assignments if a.get("slot_name")]
    if not my_slots:
        await initial_response.edit_original_response(
            content="You have no assigned slots. Use /assign_slot first.")
        return

    # A specific slot (or the only one you have) -> the detailed list of what's left.
    if slot_name:
        if not any(s.lower() == slot_name.lower() for s in my_slots):
            await initial_response.edit_original_response(
                content=f"You don't have **{slot_name}** assigned to you.")
            return
        await _send_go_mode_detail(ctx, initial_response, slot_name)
        return
    if len(my_slots) == 1:
        await _send_go_mode_detail(ctx, initial_response, my_slots[0])
        return

    # Otherwise an at-a-glance overview of every slot you hold.
    status = await gomode_bot.go_mode_status(my_slots)
    lines = ["**Go-mode status for your slots:**"]
    lines += [_go_mode_overview_line(name, status.get(name, {})) for name in sorted(my_slots)]
    lines.append("")
    lines.append("Run `/items_to_go_mode slot:<name>` to see exactly what a slot still needs.")
    content = "\n".join(lines)
    if len(content) <= 1900:
        await initial_response.edit_original_response(content=content)
        return
    # Many assigned slots (e.g. via a wildcard assign) can overflow -- DM the full list rather
    # than silently truncating it.
    try:
        for chunk in chunk_text_by_line(content, 1900):
            await ctx.author.send(chunk)
        await initial_response.edit_original_response(
            content="Your list is long — I've sent the full status to you in a DM.")
    except discord.Forbidden:
        await initial_response.edit_original_response(
            content="Your status list is too long to show here and I couldn't DM you — please enable DMs.")
    except discord.HTTPException:
        await initial_response.edit_original_response(
            content="Your status list is long, but I hit an error sending the DM — please try again.")


@bot.slash_command(description="Assign your discord account to a slot name. Use * as a wildcard to assign several at once.")
@option("slot_name", description="A slot name, or a wildcard like Alex_* to assign every matching slot.", autocomplete = slot_name_autocomplete, required=True)
async def assign_slot(ctx, slot_name: str):
    initial_response = await ctx.respond("Assigning slot name...", ephemeral=True)

    pattern = slot_name.strip()

    os.makedirs("data", exist_ok=True)
    slot_info_json = os.path.join("data", "slot_info.json")
    try:
        with open(slot_info_json, "r") as f:
            slot_info = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        await initial_response.edit_original_response(
            content="No server data found yet. Run /get_server_data first."
        )
        return

    # If the input contains wildcard characters, match every slot against it;
    # otherwise look for a single exact (case-insensitive) match, as before.
    is_wildcard = any(ch in pattern for ch in "*?[")
    matched_slots = []  # list of (slot_number, info)
    for key, info in slot_info.items():
        name = info.get("slot_name", "")
        if is_wildcard:
            if fnmatch.fnmatchcase(name.lower(), pattern.lower()):
                matched_slots.append((key, info))
        elif name.lower() == pattern.lower():
            matched_slots.append((key, info))
            break

    if not matched_slots:
        if is_wildcard:
            message = f"No slots matched the pattern `{pattern}`. Try a different wildcard."
        else:
            message = f"{pattern} not found. Check spelling and try again."
        await initial_response.edit_original_response(content=message)
        return

    author_id = str(ctx.author.id)

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

    # Each author maps to a list of assignment dictionaries.
    assignments = listeners_data.setdefault(author_id, [])
    existing_names = {a.get("slot_name", "").lower() for a in assignments}

    newly_assigned = []
    skipped = []
    for slot_number, info in matched_slots:
        name = info.get("slot_name")
        game_name = info.get("game", "Unknown")
        if name.lower() in existing_names:
            skipped.append(name)
            continue
        assignments.append({
            "slot_number": slot_number,
            "slot_name": name,
            "game": game_name,
            "items": [],  # list to hold the items the user is tracking
            "tracked_items": []
        })
        existing_names.add(name.lower())
        newly_assigned.append(name)

    # Save the updated listeners data back to listeners.json
    with open(listeners_file_json, "w") as outfile:
        json.dump(listeners_data, outfile, indent=4)

    # Build a result message.
    lines = []
    if newly_assigned:
        if len(newly_assigned) == 1:
            lines.append(f"Assigned **{newly_assigned[0]}** to you.")
        else:
            lines.append(f"Assigned **{len(newly_assigned)}** slots to you:")
            lines.extend(f"• {name}" for name in newly_assigned)
    if skipped:
        lines.append(f"Already assigned ({len(skipped)}): {', '.join(skipped)}")
    if not newly_assigned and not skipped:
        lines.append("No slots assigned.")

    content = "\n".join(lines)
    # Stay under Discord's 2000-character message limit for large wildcard matches.
    if len(content) > 1900:
        parts = []
        if newly_assigned:
            parts.append(f"Assigned {len(newly_assigned)} slots to you.")
        if skipped:
            parts.append(f"{len(skipped)} were already assigned.")
        content = " ".join(parts)

    await initial_response.edit_original_response(content=content)


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
        # get_all_tracker_received_items is BLOCKING (synchronous requests, one HTTP call per
        # slot). Run it in a thread so it can't stall the event loop and make the bot miss
        # Discord's 3s interaction-ack window (error 10062 "Unknown interaction").
        # Wrapped so a transient error (e.g. the tracker host timing out) is logged and retried
        # next cycle instead of killing the loop permanently.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, tracker_download.get_all_tracker_received_items, tracker_url, auth)
        except Exception as e:
            print(f"[tracker] scrape failed (will retry next cycle): {e}")
        await asyncio.sleep(60)


# Loop function to check for changes every 60 seconds and send a DM to a specific channel.
async def check_for_item_changes(tracker_url, auth, channel_id):
    await bot.wait_until_ready()
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        print(f"Channel with ID {channel_id} not found.")
        return

    while not bot.is_closed():
        # Get the new diff from tracker data. This is BLOCKING (synchronous requests, one HTTP
        # call per slot -- ~8s for a large seed), so run it in a thread; otherwise it stalls the
        # event loop and the bot misses Discord's 3s interaction-ack window (error 10062, seen as
        # "Application didn't respond" on commands AND autocompletes).
        # Wrapped so a transient failure (e.g. the tracker host timing out) is logged and retried
        # next cycle instead of killing the loop permanently (it is started once and never
        # restarted, so an unhandled exception would stop tracking until a full bot restart).
        try:
            diff = await asyncio.get_running_loop().run_in_executor(
                None, tracker_download.get_all_tracker_received_items, tracker_url, auth)
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
        except Exception as e:
            print(f"[tracker] item-change check failed (will retry next cycle): {e}")

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
    while not bot.is_closed():
        # Wrapped so a transient error never kills the loop (it is started once and never
        # restarted, so an unhandled exception would stop per-user item tracking until a full
        # bot restart).
        try:
            await _run_tracked_items_check()
        except Exception as e:
            print(f"[tracked-items] check failed (will retry next cycle): {e}")
        await asyncio.sleep(10)


async def _run_tracked_items_check():
    os.makedirs("data", exist_ok=True)
    listeners_data_json = os.path.join("data", "listeners.json")
    items_received_json = os.path.join("data", "items_received.json")

    # Load items_received.json
    try:
        with open(items_received_json, "r") as f:
            items_received = json.load(f)
    except Exception as e:
        print(f"Error loading {items_received_json}: {e}")
        return

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
            except Exception as e:
                print(f"[tracked-items] could not DM user {user_id}: {e}")

    # If any updates were made, save the updated listeners data back to file
    if any_update:
        with open(listeners_data_json, "w") as f:
            json.dump(listeners_data, f, indent=4)


async def check_go_mode_loop():
    """Edge-triggered go-mode notifications: DM the player who holds a slot the moment it
    reaches go mode. Verified slots are evaluated instantly in-process; the few fallback
    slots share one fast oracle subprocess. Each slot is notified once per registered seed."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _run_go_mode_notifications()
        except Exception as e:  # never let one bad cycle kill the loop
            print(f"[go-mode] notification check error: {e}")
        await asyncio.sleep(120)


# Process-level guards so a persistence failure can't cause re-DMing, and so unchanged
# fallback slots aren't rebuilt by the oracle every cycle. Both are keyed with the seed so
# they self-reset when a new seed is registered.
_go_mode_dm_sent = set()       # {(seed, author_id, slot_name)} delivered this process run
_go_mode_fallback_sig = {}     # {slot_name: (seed, inventory_signature)} last "not yet" check


def _inventory_sig(inv: dict):
    return tuple(sorted(inv.items()))


async def _run_go_mode_notifications():
    reg = gomode_bot.load_registry()
    if not reg:
        return  # no seed registered yet
    seed = reg.get("seed")

    # slot_name -> set of Discord user ids assigned to it (usually exactly one)
    slot_to_authors = {}
    for author_id, assignments in _load_listeners().items():
        for a in assignments:
            sn = a.get("slot_name")
            if sn:
                slot_to_authors.setdefault(sn, set()).add(author_id)
    if not slot_to_authors:
        return

    notified = gomode_bot.load_notified(seed)  # set of "author_id:slot_name" tokens

    def tok(author_id, sn):
        return f"{author_id}:{sn}"  # author_id is always numeric, so this is unambiguous

    def pending_authors(sn):
        # Authors for this slot not yet notified (persisted OR delivered this run). Keyed
        # per (author, slot) so a late-assigned author still gets the DM.
        return [a for a in slot_to_authors[sn]
                if tok(a, sn) not in notified and (seed, a, sn) not in _go_mode_dm_sent]

    candidate_slots = [sn for sn in slot_to_authors if pending_authors(sn)]
    if not candidate_slots:
        return

    items_received = gomode_bot._load_items_received()
    cache = gomode_bot.load_cache()

    def is_fallback(sn):
        rec = gomode_bot.slot_for_name(cache, sn) if cache else None
        return bool(rec and rec.get("status") == "ok"
                    and not rec.get("requirements", {}).get("verified"))

    # Throttle: a fallback slot can't reach go mode without its inventory changing, and each
    # check rebuilds its world. Skip fallback slots whose inventory is unchanged since the last
    # "not yet" result. Verified slots are cheap (in-process) and always checked.
    to_check = []
    for sn in candidate_slots:
        if is_fallback(sn):
            sig = (seed, _inventory_sig(gomode_bot.inventory_for_slot(items_received, sn)))
            if _go_mode_fallback_sig.get(sn) == sig:
                continue
        to_check.append(sn)
    if not to_check:
        return

    status = await gomode_bot.go_mode_status(to_check, items_received=items_received)

    changed = False
    for sn in to_check:
        st = status.get(sn, {})
        igm = st.get("in_go_mode")
        # Remember a definitive "not yet" for fallback slots so we don't rebuild next cycle.
        if is_fallback(sn) and st.get("status") == "ok" and igm is False:
            _go_mode_fallback_sig[sn] = (
                seed, _inventory_sig(gomode_bot.inventory_for_slot(items_received, sn)))
        if not (st.get("status") == "ok" and igm is True):
            continue
        game = st.get("game") or ""
        msg = (f"🎉 **{sn}**" + (f" ({game})" if game else "") +
               " has reached **go mode** — you now have everything you need to reach your "
               "goal in logic. Congrats!")
        for author_id in pending_authors(sn):
            try:
                user = await bot.fetch_user(int(author_id))
                if user:
                    await user.send(msg)
                    # Mark notified ONLY after the DM actually sends, so a closed-DM/transient
                    # failure is retried next cycle instead of being silently lost.
                    notified.add(tok(author_id, sn))
                    _go_mode_dm_sent.add((seed, author_id, sn))
                    changed = True
            except discord.Forbidden:
                print(f"[go-mode] could not DM {author_id} for {sn} (DMs disabled)")
            except Exception as e:
                print(f"[go-mode] DM error for {author_id}/{sn}: {e}")

    if changed:
        try:
            gomode_bot.save_notified(seed, notified)
        except Exception as e:
            # Persistence failed, but the in-process guard already prevents re-DMing this run.
            print(f"[go-mode] could not persist notified state: {e}")


if __name__ == "__main__":
    bot.run(discord_token)