"""One-shot hint request: connect to the Archipelago server as a slot (text-only, so the player's own
game client is not affected), say "!hint <item>", collect the server's replies, and disconnect.

The password is only ever put in the Connect packet. It is never printed, logged or placed in a result.
"""
import asyncio
import json
import re
import time

import websockets

QUIET_SECONDS = 1.0      # stop collecting this long after the last relevant reply
REFUSAL_TEXT = {
    "InvalidSlot": "that slot name is not on the server",
    "InvalidPassword": "the password is wrong or missing",
    "InvalidGame": "the game does not match",
    "IncompatibleVersion": "the server refused the bot's version",
    "InvalidItemsHandling": "the server refused the connection settings",
}


class HintResult:
    def __init__(self, status, lines=None, hint_points=None, detail=""):
        self.status = status           # ok | refused | timeout | unreachable | error
        self.lines = lines or []       # readable reply lines from the server
        self.hint_points = hint_points
        self.detail = detail

    def __repr__(self):
        return f"HintResult({self.status!r}, {self.lines!r}, points={self.hint_points!r}, {self.detail!r})"


def _clean_address(address):
    return re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", address.strip())


def _name_maps(data_package):
    """{game: ({item id: name}, {location id: name})} from the bot's data_package.json shape."""
    out = {}
    for entry in data_package or []:
        game = entry.get("game")
        if not game:
            continue
        out[game] = ({int(v): k for k, v in entry.get("item_name_to_id", {}).items()},
                     {int(v): k for k, v in entry.get("location_name_to_id", {}).items()})
    return out


def render_message(msg, slot_names, slot_games, maps):
    """Plain text for a PrintJSON packet. Returns (text, games whose names are missing)."""
    pieces, missing = [], set()
    for part in msg.get("data", []):
        text = str(part.get("text", ""))
        kind = part.get("type")
        try:
            if kind == "player_id":
                text = slot_names.get(int(text), f"Player {text}")
            elif kind in ("item_id", "location_id"):
                player = int(part.get("player", 0))
                game = slot_games.get(player)
                table = maps.get(game)
                if table is None:
                    missing.add(game)
                    text = f"#{text}"
                else:
                    name = table[0 if kind == "item_id" else 1].get(int(text))
                    text = name if name else f"#{text}"
        except (ValueError, TypeError):
            pass
        pieces.append(text)
    return "".join(pieces).strip(), {g for g in missing if g}


async def _recv_json(ws, timeout):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def _session(ws, slot, password, item, data_package, timeout):
    deadline = time.monotonic() + timeout
    slot_names, slot_games = {}, {}
    maps = _name_maps(data_package)
    points = None
    messages = []           # relevant PrintJSON packets
    last_relevant = None
    sent = False
    connecting = False

    while True:
        wait = max(0.1, deadline - time.monotonic())
        if last_relevant is not None:
            wait = min(wait, max(0.05, QUIET_SECONDS - (time.monotonic() - last_relevant)))
        try:
            packets = await _recv_json(ws, wait)
        except asyncio.TimeoutError:
            if last_relevant is not None:
                break                       # quiet period over: we have the answer
            return HintResult("timeout", detail="no reply from the server")
        for pkt in packets:
            cmd = pkt.get("cmd")
            if cmd == "RoomInfo" and not connecting:
                connecting = True
                await ws.send(json.dumps([{
                    "cmd": "Connect", "password": password or "", "name": slot,
                    "version": {"major": 0, "minor": 6, "build": 0, "class": "Version"},
                    "tags": ["TextOnly"], "items_handling": 0, "uuid": "", "game": "", "slot_data": False}]))
            elif cmd == "ConnectionRefused":
                errors = pkt.get("errors") or ["Unknown"]
                why = "; ".join(REFUSAL_TEXT.get(str(e), str(e)) for e in errors)
                return HintResult("refused", detail=why)
            elif cmd == "Connected":
                points = pkt.get("hint_points")
                for p in pkt.get("players", []):
                    slot_names[int(p["slot"])] = p.get("alias") or p.get("name")
                for k, info in (pkt.get("slot_info") or {}).items():
                    slot_names.setdefault(int(k), info.get("name"))
                    slot_games[int(k)] = info.get("game")
                await ws.send(json.dumps([{"cmd": "Say", "text": f"!hint {item}"}]))
                sent = True
            elif cmd == "PrintJSON" and sent and pkt.get("type") in ("Hint", "CommandResult"):
                messages.append(pkt)
                last_relevant = time.monotonic()
        if time.monotonic() >= deadline:
            if not messages:
                return HintResult("timeout", detail="no reply from the server")
            break

    # Turn the packets into text, fetching any game's names we do not already hold.
    rendered = [render_message(m, slot_names, slot_games, maps) for m in messages]
    need = set().union(*[r[1] for r in rendered]) if rendered else set()
    if need:
        await ws.send(json.dumps([{"cmd": "GetDataPackage", "games": sorted(need)}]))
        got_until = time.monotonic() + 10
        while time.monotonic() < got_until:
            try:
                packets = await _recv_json(ws, max(0.1, got_until - time.monotonic()))
            except asyncio.TimeoutError:
                break
            hit = False
            for pkt in packets:
                if pkt.get("cmd") == "DataPackage":
                    hit = True
                    for game, gd in pkt.get("data", {}).get("games", {}).items():
                        maps[game] = ({int(v): k for k, v in gd.get("item_name_to_id", {}).items()},
                                      {int(v): k for k, v in gd.get("location_name_to_id", {}).items()})
            if hit:
                break
        rendered = [render_message(m, slot_names, slot_games, maps) for m in messages]
    lines = []
    for text, _ in rendered:
        if text and text not in lines:
            lines.append(text)
    return HintResult("ok", lines, points)


async def request_hint(address, slot, password, item, data_package=None, timeout=15):
    """Ask the server for a hint on `item` as `slot`. Never raises; returns a HintResult."""
    cleaned = _clean_address(address)
    last_error = None
    for scheme in ("ws", "wss"):
        try:
            async with websockets.connect(f"{scheme}://{cleaned}", open_timeout=8, max_size=2 ** 26) as ws:
                return await _session(ws, slot, password, item, data_package, timeout)
        except websockets.InvalidMessage:
            last_error = "handshake failed"
            continue                         # probably a TLS-only endpoint: try wss
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            last_error = type(e).__name__
            break
        except Exception as e:               # keep the command alive on anything odd
            return HintResult("error", detail=type(e).__name__)
    return HintResult("unreachable", detail=last_error or "")
