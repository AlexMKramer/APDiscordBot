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
- `GOMODE_SLOT_TIMEOUT`: seconds `/register_seed` may spend analyzing one slot. A slot that takes longer is stopped and put off, the other slots carry on, and it is tried again once they are done. Default 300.
- `GOMODE_SLOT_RETRY_TIMEOUT`: seconds a put-off slot may take on that retry. A slot that runs past this limit too is marked not supported, and the reason gives both limits. Default 1800, or twice `GOMODE_SLOT_TIMEOUT` if that is more. Set it to 0, or to a value not above `GOMODE_SLOT_TIMEOUT`, to skip the retry: a slot over the first limit is then marked not supported at once.

## Files the bot creates
The bot keeps its state in `data/` (kept out of git). Back it up if you care about slot assignments and tracked items.
