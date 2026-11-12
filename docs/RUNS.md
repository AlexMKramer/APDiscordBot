# Several runs in one bot

One bot can follow more than one Archipelago run at the same time. A **run** is one multiworld: its own tracker,
its own feed channel, its own server address, its own slot assignments and its own go-mode seed. Everything a player
types works inside the channel of the run they mean.

## How it works

- **One channel is one run, and one tracker is one run.** The run's item feed, hourly progress, pinned hints and
  tracked-item notices all go to its channel.
- **Commands act on the run of the channel they are used in** (a thread counts as its channel). The same slot name can
  exist in two runs; `/assign_slot Player1` in one channel and in the other gives two separate assignments.
- **With one run**, a command works from any channel, exactly as it did before runs existed.
- **With two or more runs**, a command used outside a run's channel is answered with
  `Use this in a run's channel: #a, #b`, and the slot-name suggestions stay empty there.
- **Direct messages** from the bot start with `[Run name] ` while two or more runs are active, so you can tell them
  apart. With one run they are unchanged.

## Setting up a run (owner only)

In the channel the run's feed should go to:

```
/link_run name:Autumn tracker_url:https://archipelago.gg/tracker/<id> server_address:archipelago.gg:38281
```

`server_address` is optional; `/get_server_data` in the same channel sets or changes it later. `/link_run` refuses a
channel or tracker that is already linked, and a name another active run already uses. The run starts at once, with
no restart.

Then, in the same channel, as for a single run: `/get_server_data` (once), `/register_seed` (for go-mode), and
players use `/assign_slot`.

`/unlink_run` (in the run's channel, or `/unlink_run name:Autumn` from any channel) stops the run: it stops posting and tracking, and is marked inactive. Its data
stays in `data/runs/<run>/`. Running `/link_run` again with the same tracker URL restores the run with its
assignments, items and seed.

`/list_runs` shows every run: name, channel, tracker, server address, whether it is active and whether a seed is
registered.

If a run's channel is deleted, the bot keeps running and logs one line per run saying the channel no longer exists, and
posts nothing for it. It does not unlink the run on its own, because a channel can be unreadable for a while. `/list_runs`
shows the channel as missing; `/unlink_run name:<run>` removes the run.

## Apworlds per run

Go-mode rebuilds each slot with the same apworld that the seed was made with, so two runs that use different versions of
a game's apworld need their own copies.

- **Each run has its own Archipelago tree.** `/register_seed` in a run's channel sets one up in
  `<runtime dir>/<run>/ap-<version>/`, with its own `custom_worlds/` and `manifest.json`. Registering one run never
  changes another run's tree.
- **Which apworlds a run gets.** Inside `GOMODE_APWORLDS_DIR`, a folder named after the run (its name, else its id) is
  used when there is one; otherwise the apworlds directly in `GOMODE_APWORLDS_DIR` are used. The progress message of
  `/register_seed` says which one. For example:

  ```
  apworlds/                  shared by every run without a folder of its own
    crabchampions.apworld
    Autumn/                  used by the run named Autumn
      crabchampions.apworld  (another version)
      ...                    (every apworld that run's seed needs)
  ```

  A run's folder replaces the shared set; it does not add to it, so it has to hold every apworld the seed used.
- **A tree shows only the current set.** Registering again removes `.apworld` files left from an earlier set before the
  new ones are copied in.
- **Runs registered before this change keep working.** A run's registration remembers its tree, so a run set up
  earlier keeps using the shared tree in the runtime directory until `/register_seed` is run for it again; nothing
  is provisioned again on its own. A bot with no runs file keeps using the runtime directory itself, as before.

## Where the files are

| Path | What |
|------|------|
| `data/runs.json` | The list of runs (name, channel, tracker, server address, active). |
| `data/runs/<run>/` | One run's own files: `slot_info.json`, `listeners.json`, `items_received.json`, `announced_marks.json`, `hints.json`, `hints_pin_state.json`, `percentage_state.json`, `registered_seed.json`, `start_inventory.json`, `go_mode_notified.json`, `unstuck_alerts.json`, `unstuck_state.json`, `server_address.txt`, `preferences.json` (only the values that differ from the defaults; see `/preferences`). |
| `gomode_analyzer/runtime/<run>/` | The run's go-mode seed cache, Archipelago tree and manifest (under `GOMODE_RUNTIME_DIR` if set). |
| `data/data_package.json`, `data/datapackage_cache/`, `data/logs/` | Shared by every run. |

## The first start after updating

A bot that was set up with `DISCORD_CHANNEL_ID` and `TRACKER_URL` in `.env` and has no `data/runs.json` yet turns that
setup into the first run, automatically, on its first start:

1. The run is named after its feed channel (`Run 1` if the bot cannot read the name).
2. Its files are copied into `data/runs/<run>/` and checked, then the originals are moved to `data/_pre_multirun/`.
   They are never deleted.
3. `data/runs.json` is written last, so a start that was interrupted simply does it again on the next start.

Players notice nothing: same channel, same assignments, same seed. Slash commands are registered with Discord again
once, because `/link_run`, `/unlink_run` and `/list_runs` are new.

The `.env` settings `DISCORD_CHANNEL_ID`, `TRACKER_URL` and `AP_SERVER_ADDRESS` only matter for this first start (and
for a bot that has no runs file); after that the runs file is what counts. The tracker login
(`URL_AUTH_USERNAME`, `URL_AUTH_PASSWORD`) stays in `.env` and is used for every run.

## Undoing it

1. Stop the bot and start the previous version.
2. Move the files from `data/_pre_multirun/` back into `data/` (the `runtime/seed_cache.json` inside it goes back to
   `gomode_analyzer/runtime/`), and delete `data/runs.json` and `data/runs/`.

Anything that changed after the update is in `data/runs/<run>/` and is not carried back.
