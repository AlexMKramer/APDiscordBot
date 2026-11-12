# Commands

Type `/` in Discord and pick the bot to see them. Slot name boxes suggest names as you type. Replies with lists arrive as DMs.

## Setup
- `/get_server_data server_address slot_name [password]`: connects once with the bot's slot to download the item and slot info the bot needs. Example: `/get_server_data localhost:38281 BotSlot`. It also remembers the address for `/hint`.
- `/assign_slot slot_name`: links your Discord account to a slot. Use `*` as a wildcard to assign several (for example `Player*`). If no names are listed, restart the bot and try again.

## Runs
Owner only. With more than one run, commands answer for the run linked to the channel they are typed in. See [RUNS.md](RUNS.md).
- `/link_run name tracker_url [server_address]`: links a run to this channel.
- `/unlink_run [name]`: unlinks this channel's run, or the run called `name` from any channel (the names are suggested as you type). Its data is kept, and linking the same tracker again brings it back.
- `/list_runs`: lists the linked runs and their channels; a channel that was deleted is shown as missing.

## Items
- `/get_items_for_slot slot_name`: DM with every item the slot has received.
- `/get_new_items_for_slot slot_name`: DM with new items for one of your assigned slots.
- `/get_all_new_items`: DM with new items for all your assigned slots.
- `/get_all_tracked_items`: DM with all items for the slots you assigned yourself.
- `/track_item game_name item_name slot_name target_amount`: DM each time that item arrives, up to the amount you set.

## Preferences
- `/preferences [progression_color] [useful_color] [trap_color] [filler_color] [reset_all]`: for the bot owner and server administrators (members with Discord's Administrator permission). Shows this run's preferences, and changes the ones you give. Each run has its own.
  - The colour options take six hex digits, with or without the `#` (`AB57FF`, `#b600fb`). `default` (or `reset`) puts that colour back, and `reset_all` puts every preference back. A value that isn't valid changes nothing.
  - The colours apply everywhere item names are coloured: the feed, the item DMs, the most useful items list and the go-mode requirement list. Defaults: progression `AB57FF`, useful `6363E4`, trap `DE4040`, filler `5BCEFA`. A colour on its default keeps its own fallback for clients without truecolor (magenta, blue, red and cyan); a custom colour falls back to the nearest of Discord's eight colours.
  - With no options it only shows the current values.

## Hints
- `/hint slot_name item [password]`: asks the Archipelago server for a hint, which spends that slot's hint points. Allowed for the bot owner and for whoever assigned that slot. The password is not stored.
- The bot also keeps a pinned message in the feed channel listing every unfound hint.

## Go-mode and checks in logic
These need a registered seed. See [GOMODE.md](GOMODE.md).
- `/register_seed [seed_file] [server_path]`: owner only. Registers the generated seed zip.
- `/items_to_go_mode [slot_name]`: what you still need to reach your goal. Leave the name empty for all your slots.
- `/checks_in_logic [slot_names]`: DM with how many checks are in logic. Comma-separated, `*` works.
- `/which_checks_in_logic slot_name`: DM listing the checks in logic that are not done yet.
- `/sphere_zero_checks [slot_names]`: DM with the sphere 0 checks (open from the start, using only the start inventory) not done yet, per slot. Blank means every slot in the run; comma-separated, `*` works. Slots with none left are skipped, and unsupported slots are named.
- `/most_useful_items slot_name`: DM ranking the missing items that would open the most checks.
- `/unstuck_alerts action [slot_names]`: `on`, `off` or `list`. Sends a DM when a slot that was out of checks in logic gets some again.

Slots that have reached their goal are skipped by these commands.
