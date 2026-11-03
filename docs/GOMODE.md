# Go-mode and checks in logic

Go-mode answers "what do I still need to reach my goal?". The same engine tells you how many checks are in logic and which items would open the most. It rebuilds each slot from the generated seed and the apworlds that made it, so it needs both.

## Set it up
1. Put the `.apworld` files used to generate the seed in `apworlds/` (the same `custom_worlds` you generated with). They are not committed. In Docker this folder is already inside the container.
2. Start the bot (`docker compose up -d --build`). The image builds a separate Python environment for Archipelago.
3. As the bot owner (`GOMODE_OWNER_ID`), run `/register_seed` and attach the generated `AP_<seed>.zip`. If the zip is already on the bot's machine, pass `server_path` instead.
4. Wait for it to finish. Every slot is built once, so later commands answer fast.

Register again whenever you generate a new seed.

## Use it
See the go-mode section of [COMMANDS.md](COMMANDS.md). The bot also sends a DM when a slot reaches go-mode, and `/unstuck_alerts` tells you when a stuck slot has checks again.

## Limits
- A game whose logic can't be rebuilt is reported as unsupported, with the reason.
- Replies list item names only, never where an item is placed.
- Engine details: [gomode_analyzer/README.md](../gomode_analyzer/README.md).
