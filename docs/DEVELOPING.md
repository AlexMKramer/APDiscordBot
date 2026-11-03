# Developing

- `main.py`: the Discord commands and background loops.
- `tracker_download.py`: reads the room tracker.
- `hint_client.py`, `hints_pin.py`: `/hint` and the pinned hints message.
- `gomode_bot.py` and `gomode_analyzer/`: go-mode (see [GOMODE.md](GOMODE.md)).

Run without Docker:
1. `pip install -r requirements.txt` (Python 3.11 or newer).
2. Fill in `.env`, then `python main.py`.

Go-mode also needs the Archipelago environment from `gomode-ap-requirements.txt`; the Dockerfile shows how it is built.
