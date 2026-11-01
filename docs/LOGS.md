# Bot log

The bot writes one readable log per run. It goes to two places:

- standard output, so `docker compose logs -f app` shows it;
- `data/logs/bot_<YYYY-MM-DD_HH-MM-SS>.log` (the `data/` folder on the bot server, next to `items_received.json`).
  The newest 10 files are kept; older ones are deleted at startup.

## Line format

```
14:03:22.118 INFO  [items] Announced Hookshot for Player1
```

`HH:MM:SS.mmm`, a level (`DEBUG` `INFO` `WARN` `ERROR`), a category in brackets, then a plain sentence. The date is
in the file name. A caught error is one `ERROR` line followed by its traceback.

DEBUG lines (packets, queue sizes, other detail) appear only when `LOG_LEVEL=DEBUG` is set in `.env`.
Without it the level is INFO.

## Categories

| Category | What it covers |
|---|---|
| `start` | Bot, Python and py-cord versions; each background loop starting; the go-mode analyzer becoming ready |
| `config` | Which settings were read (tracker host, whether a login or feed channel is set), log file name |
| `conn` | Discord login, ready, disconnect, resume; Archipelago server connect, refusal, drop; tracker read failures and recovery |
| `items` | Each item announced in the feed (`Announced Hookshot for Player1`), and items seen on a get_server_data connection |
| `checks` | Checks in logic results per slot, logged when a slot's numbers change |
| `goal` | A slot reaching its goal, go-mode results (logged when they change), seed registration, go-mode and unstuck DMs |
| `feed` | Feed polling, quiet periods, hourly completion percentage |
| `hints` | `/hint` results and the pinned hints message |
| `cmd` | Each slash command used: `/hint by <discord name>: Player1, Hookshot` |
| `error` | Every caught exception, with its traceback |

## Examples

```
14:00:00.012 INFO  [start] AP Discord bot starting. Python 3.12.3, py-cord 2.6.1
14:00:00.013 INFO  [config] Tracker host archipelago.example.org, tracker login set, feed channel set, go-mode owner set, log level INFO
14:00:03.402 INFO  [conn] Logged in to Discord as APBot
14:03:22.118 INFO  [cmd] /hint by Player2: Player1, Hookshot
14:05:10.870 ERROR [conn] Tracker read failed, will retry: HTTPSConnectionPool(...) timed out
14:05:30.881 INFO  [conn] Tracker read recovered after 2 failed attempts
14:07:44.002 INFO  [goal] Goal complete: Player1 (A Link to the Past)
```

## Never in the log

The Discord token, the tracker login, the tracker URL's credentials, server passwords (the `password` option of
`/hint` and `/get_server_data` is skipped), or full paths that contain a user name. As a safety net the log
handlers blank out the token and tracker login values if one ever turns up in a message.

Names are written, not ids: slot, game, item and location names. An id appears only as `unknown item 1234`
when the name is not known, or for a Discord user the bot could not look up.

The feed's item line has no sender because the tracker does not give one; the `get_server_data` connection logs
`Received Hookshot for Player1 from Player2 (Location)`.

## Code

`log_setup.py` sets everything up once (`log_setup.setup()` at the top of `main.py`). Other modules get a logger
named after the category: `log = log_setup.get("conn")`.
