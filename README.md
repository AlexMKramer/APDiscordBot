# APDiscordBot

A self-hosted Discord bot for an Archipelago multiworld. It posts new items to a channel within seconds, DMs players the items they received, and can tell them what they still need to reach their goal.

## What you need
- A computer or server that stays on, with Docker and Docker Compose (or Python 3.12 if you skip Docker).
- A Discord account to create the bot application.
- An Archipelago WebHost room tracker URL (for example `https://your-host/tracker/<room-id>`).
- The Archipelago server address, and a slot for the bot (see [Use](#use)).

## Install
1. Clone the repo and open the folder: `git clone https://github.com/AlexMKramer/APDiscordBot.git`, then `cd APDiscordBot`.
2. Copy the template: `cp .env_template .env`.
3. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a New Application.
4. Under Bot, copy the token. Put it in `.env` as `DISCORD_TOKEN`.
5. Under OAuth2 > URL Generator, tick `bot` and `applications.commands`. Under Bot Permissions tick Send Messages and Manage Messages (the second lets the bot pin the hints list).
6. Open the invite URL it shows and add the bot to your Discord server.
7. Fill in `TRACKER_URL` in `.env`. Add `DISCORD_CHANNEL_ID` for the item feed channel. All options are in [docs/CONFIG.md](docs/CONFIG.md).
8. Start it: `docker compose up -d --build`.

## Use
First-time setup, in Discord:
1. `/get_server_data` with the server address and port, the bot's slot name, and the password if the slot has one. Needed once.
2. `/assign_slot` to link your Discord account to your slot.

Everyday commands:
- `/get_new_items_for_slot` shows new items for one of your slots.
- `/get_all_new_items` shows new items for all your slots.
- `/get_items_for_slot` lists everything a slot has received.
- `/track_item` sends a DM when a specific item arrives.
- `/hint` asks the server for a hint on an item.
- `/items_to_go_mode` shows what you still need to reach your goal (needs [go-mode](docs/GOMODE.md)).

Every command is listed in [docs/COMMANDS.md](docs/COMMANDS.md).

One bot can follow several runs at once, each linked to its own channel: the server owner runs `/link_run` in that run's channel. See [docs/RUNS.md](docs/RUNS.md).

## Logs
The bot writes a log for each run to the console (`docker compose logs -f`) and to `data/logs/bot_<date>_<time>.log`, keeping the newest 10. Set `LOG_LEVEL=DEBUG` in `.env` for more detail; see [docs/LOGS.md](docs/LOGS.md).

## Known issues
See the [issues page](https://github.com/AlexMKramer/APDiscordBot/issues).

## More
- [Commands](docs/COMMANDS.md)
- [Configuration (.env)](docs/CONFIG.md)
- [Go-mode, checks in logic and registering a seed](docs/GOMODE.md)
- [Several runs in one bot](docs/RUNS.md)
- [Developing](docs/DEVELOPING.md)
