"""
Outbound message formatting and transport: splitting long replies into
radio-sized chunks, dropping stale queued messages, and the send/ack
loops for direct and channel replies. No game logic lives here.
"""

import asyncio
import textwrap
import time

from db import log_message
from meshcore import EventType


MAX_MSG_LEN = 130  # hard limit enforced by the meshcore radio/app


# Messages older than this (based on the sender's own sender_timestamp,
# not arrival time) are ignored. This catches commands that queued up on
# the radio while the app was disconnected and all arrive in a burst once
# it reconnects -- without this, a stale "move" or other command would
# get acted on as if it just happened.
MAX_MESSAGE_AGE_SECONDS = 120


# Floor on how long we'll wait for a chunk's delivery ACK. The firmware
# suggests a timeout scaled to the known path length, but on a marginal
# or recently re-routed link that suggestion can be optimistic -- timing
# out early burns send_msg_with_retry's attempts on ACKs that were
# actually still in flight. Passed as min_timeout so the suggested value
# still applies whenever it's the larger of the two.
MIN_ACK_TIMEOUT_SECONDS = 6


# Pause after each acknowledged chunk before transmitting the next one.
# The instant a chunk's ACK arrives, the mesh is still settling from
# that exchange; firing the next 130-char packet back-to-back invites
# collisions that burn the retry budget and can kill the whole reply.
INTER_CHUNK_DELAY_SECONDS = 1.0


# When a chunk exhausts send_msg_with_retry's attempts, wait this long
# for the mesh to quiet down, then give that same chunk one more full
# retry round before declaring the link dead.
FAILED_CHUNK_RETRY_DELAY_SECONDS = 3.0


# Serializes every outbound transmission -- multipart replies, channel
# adverts, inactivity warnings -- so no two senders ever interleave on
# the radio. Replies, the advert loop, and the inactivity monitor all
# run as independent asyncio tasks; without this lock any of them could
# transmit mid-sequence while send_reply was between chunks of a
# multipart message, contending for airtime with its ACKs.
_TX_LOCK = asyncio.Lock()


def _prepare_lines(text, limit):
    """Split text into lines, word-wrapping any line that's too long on its own."""
    raw_lines = text.split("\n")
    lines = []
    for line in raw_lines:
        if len(line) <= limit:
            lines.append(line)
        else:
            lines.extend(textwrap.wrap(line, width=limit) or [""])
    return lines


def _pack_lines(lines, limit):
    """Greedily join lines with '\\n', keeping each resulting chunk <= limit."""
    chunks = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def chunk_message(text, limit=MAX_MSG_LEN):
    """
    Split text into one or more chunks that each fit within `limit` chars,
    preserving newlines for readability. Any single line longer than the
    limit on its own gets word-wrapped as a fallback. If more than one
    chunk is needed, each is prefixed with "(i/n) " so the recipient can
    tell a reply was split — the limit used for wrapping/packing is
    re-derived each pass so the prefix never pushes a chunk over `limit`.
    """
    chunks = _pack_lines(_prepare_lines(text, limit), limit)
    if len(chunks) <= 1:
        return chunks

    # Reserve room for the "(i/n) " prefix, then redo wrapping/packing at
    # the reduced width. Do a second pass in case digit count of n changes
    # after the first pass (e.g. 9 -> 10 chunks).
    n = len(chunks)
    prefix_width = len(f"({n}/{n}) ")
    reduced_limit = max(10, limit - prefix_width)
    chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
    n2 = len(chunks)
    if n2 != n:
        prefix_width = len(f"({n2}/{n2}) ")
        reduced_limit = max(10, limit - prefix_width)
        chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
        n2 = len(chunks)

    return [f"({i + 1}/{n2}) {c}" for i, c in enumerate(chunks)]


def is_stale_message(payload, max_age=MAX_MESSAGE_AGE_SECONDS):
    """
    True if payload's sender_timestamp is older than max_age seconds.
    sender_timestamp is set by the sender's radio when the message was
    originally sent, not when our app received it -- so this catches
    messages that sat queued on the radio (e.g. while the app was
    disconnected) and all arrived in a burst once it reconnected.
    Messages without a sender_timestamp are never treated as stale.
    """
    sender_timestamp = payload.get("sender_timestamp")
    if sender_timestamp is None:
        return False
    return (time.time() - sender_timestamp) > max_age


async def _send_chunk_acked(mc, pubkey, chunk):
    """
    Transmit one chunk and wait for its delivery ACK, with a second
    chance: if send_msg_with_retry exhausts its own retries (returns
    None), pause FAILED_CHUNK_RETRY_DELAY_SECONDS for the mesh to settle
    and give the chunk one more full retry round. Returns True once the
    recipient's radio acknowledged the chunk, False if both rounds died.
    """
    result = await mc.commands.send_msg_with_retry(
        pubkey, chunk, min_timeout=MIN_ACK_TIMEOUT_SECONDS
    )
    if result is not None:
        return True
    print(f"  No ack for chunk, retrying after "
          f"{FAILED_CHUNK_RETRY_DELAY_SECONDS:.0f}s settle: {chunk}")
    await asyncio.sleep(FAILED_CHUNK_RETRY_DELAY_SECONDS)
    result = await mc.commands.send_msg_with_retry(
        pubkey, chunk, min_timeout=MIN_ACK_TIMEOUT_SECONDS
    )
    return result is not None


async def send_reply(mc, pubkey, sender, text):
    """
    Send each chunk and wait for the recipient's radio to actually
    acknowledge it before sending the next chunk -- a real delivery
    confirmation rather than a fixed delay, which is only possible for
    direct messages (channel broadcasts have no per-recipient ACK).

    The whole reply is sent under _TX_LOCK so no other outbound
    transmission (another reply, the channel advert, an inactivity
    warning) can interleave with the chunk sequence. Each chunk gets
    send_msg_with_retry's attempts plus one settle-and-retry round via
    _send_chunk_acked; a brief pause follows each ACK so back-to-back
    chunks don't collide with their own ACK traffic. If a chunk still
    can't be delivered after all that, the link is treated as dead and
    the remaining chunks are dropped (blasting them into a dead link
    would just flood the mesh) -- the log line below says exactly which
    chunk the reply died on.
    """
    async with _TX_LOCK:
        chunks = chunk_message(text)
        for i, chunk in enumerate(chunks):
            if not await _send_chunk_acked(mc, pubkey, chunk):
                print(f"  Error sending reply (no ack received), dropping "
                      f"{len(chunks) - i - 1} remaining chunk(s): {chunk}")
                return
            print(f"  Reply sent + acked: {chunk}")
            log_message("tx", pubkey, sender, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(INTER_CHUNK_DELAY_SECONDS)


async def send_channel_reply(mc, channel_idx, text):
    """
    Broadcast a reply to everyone on the given channel (not a private
    DM). Channel broadcasts have no per-recipient ACK, so delivery can't
    be confirmed -- the best available is holding _TX_LOCK (so a
    broadcast never interleaves with a multipart DM in progress) and
    pausing INTER_CHUNK_DELAY_SECONDS between chunks so consecutive
    broadcasts aren't stepping on their own airtime.
    """
    async with _TX_LOCK:
        chunks = chunk_message(text)
        for i, chunk in enumerate(chunks):
            result = await mc.commands.send_chan_msg(channel_idx, chunk)
            if result.type == EventType.ERROR:
                print(f"  Error sending channel reply: {result.payload}")
                return
            print(f"  Channel reply sent OK: {chunk}")
            log_message("tx", f"chan{channel_idx}", "channel", chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(INTER_CHUNK_DELAY_SECONDS)
