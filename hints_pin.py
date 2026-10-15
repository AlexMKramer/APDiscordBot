"""Keeps pinned message(s) in the feed channel listing every unfound hint.

Discord caps a message at 2000 characters, so a long list is split across as many pinned
messages as needed (split at line boundaries, the player heading repeated when a group
continues). Messages are edited only when their text changes; surplus ones are deleted and
deleted ones recreated.
"""
import json
import os

import discord

STATE_JSON = os.path.join("data", "hints_pin_state.json")
MAX_LEN = 1900
EMPTY_TEXT = "**Unfound hints**\nNone right now."


def _hint_line(hint):
    line = f"- {hint['item']} is at {hint['finder']}'s {hint['location']}"
    if hint.get("entrance"):
        line += f" (entrance: {hint['entrance']})"
    return line


def render_pages(hints, max_len=MAX_LEN):
    """Message texts for the hints, grouped under each receiving player."""
    if not hints:
        return [EMPTY_TEXT]
    pages = []
    current = ""
    receiver = None
    for hint in hints:
        new_group = hint["receiver"] != receiver
        receiver = hint["receiver"]
        line = _hint_line(hint)[:max_len - 200]
        addition = (f"__{receiver}__\n" if new_group else "") + line
        if current and len(current) + 1 + len(addition) > max_len:
            pages.append(current)
            current = ""
            addition = f"__{receiver}__\n{line}"   # a group continuing on a new message keeps its heading
        if current:
            current += "\n" + addition
        else:
            current = ("**Unfound hints**" if not pages else "**Unfound hints (cont.)**") + "\n" + addition
    pages.append(current)
    return pages


def _load_state():
    try:
        with open(STATE_JSON, encoding="utf-8") as fh:
            return json.load(fh).get("message_ids", [])
    except (OSError, json.JSONDecodeError):
        return []


def _save_state(ids):
    os.makedirs("data", exist_ok=True)
    with open(STATE_JSON + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"message_ids": ids}, fh)
    os.replace(STATE_JSON + ".tmp", STATE_JSON)


_no_mentions = discord.AllowedMentions.none()
_pin_warned = False


async def _pin(message):
    """Pin, logging (once) instead of failing when the bot lacks Manage Messages or the channel is at 50 pins."""
    global _pin_warned
    try:
        await message.pin()
    except discord.Forbidden:
        if not _pin_warned:
            print("[hints] cannot pin (needs Manage Messages); keeping the hints message unpinned")
            _pin_warned = True
    except discord.HTTPException as e:
        print(f"[hints] pin failed: {e}")


async def sync(channel, hints, load_state=_load_state, save_state=_save_state):
    """Make the channel's hint message(s) match the hints (a list of dicts from tracker_download)."""
    pages = render_pages(hints)
    old_ids = load_state()
    new_ids = []
    for i, text in enumerate(pages):
        message = None
        if i < len(old_ids):
            try:
                message = await channel.fetch_message(old_ids[i])
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(text, allowed_mentions=_no_mentions)
            await _pin(message)
        else:
            if message.content != text:
                await message.edit(content=text)
            if not getattr(message, "pinned", True):
                await _pin(message)
        new_ids.append(message.id)
    for stale_id in old_ids[len(pages):]:
        try:
            stale = await channel.fetch_message(stale_id)
            await stale.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"[hints] could not delete old hints message {stale_id}: {e}")
    if new_ids != old_ids:
        save_state(new_ids)
