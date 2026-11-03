# Configuration

Copy `.env_template` to `.env` and fill it in. Never share or commit `.env`. The bot reads it at start, so restart after changes (`docker compose up -d`).

## Required
- `DISCORD_TOKEN`: the bot token from the Discord Developer Portal (Bot page).
- `TRACKER_URL`: the room tracker page, for example `https://your-host/tracker/<room-id>`.

## Optional
- `DISCORD_CHANNEL_ID`: channel for the item feed and the pinned hints list. Without it the bot only sends DMs for items people track or request.
- `URL_AUTH_USERNAME`, `URL_AUTH_PASSWORD`: basic-auth login if your tracker is behind one.
- `FEED_POLL_SECONDS`: how often the tracker is read for the feed. Default 10, minimum 5.
- `AP_SERVER_ADDRESS`: server and port used by `/hint`. If unset, the address last given to `/get_server_data` is used.
- `GOMODE_OWNER_ID`: Discord user id allowed to run `/register_seed` and to hint for any slot. If unset, the server owner is used.

## Go-mode
- `GOMODE_AP_PYTHON`, `GOMODE_APWORLDS_DIR`: already set in `docker-compose.yml`. Change them only if you run without Docker. With several runs, a folder named after a run inside `GOMODE_APWORLDS_DIR` holds that run's apworlds; see [RUNS.md](RUNS.md).
- `GOMODE_AP_REPO`: path to a local Archipelago checkout to use instead of downloading it from GitHub.
- `GOMODE_RUNTIME_DIR`: where provisioned Archipelago trees and the seed cache go. Default `gomode_analyzer/runtime`.

## Files the bot creates
The bot keeps its state in `data/` (kept out of git). Back it up if you care about slot assignments and tracked items.
