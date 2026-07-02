"""
The single-player-at-the-helm lock and its inactivity timeout. Only one
pubkey holds ACTIVE_SESSION at a time; a background task warns then frees
the lock if its holder goes silent (a radio dropout looks the same as a
slow reply, so we time it out either way).
"""

import asyncio
import time

from core import (
    PENDING_TRADES, PENDING_WARPS, PENDING_UPGRADES, PENDING_ATTACKS,
    PENDING_STATIONS, PENDING_P2P, PENDING_TOWS, PENDING_BOARDS,
)
from messaging import send_reply


# If the active player goes silent for this long, send one warning...
INACTIVITY_WARNING_SECONDS = 3 * 60


# ...and if they're still silent this much longer (total), free the lock.
INACTIVITY_KICK_SECONDS = 8 * 60


# How often the background task checks for inactivity.
INACTIVITY_CHECK_INTERVAL_SECONDS = 15


# Only one player may be "at the helm" (issuing commands) at a time.
# There's no clean way to tell "logged out" apart from "radio out of
# range" or "app crashed" -- both just look like silence -- so instead of
# trying to detect that, we hand out a single lock and time it out if its
# holder goes quiet for too long.
#
# None when nobody is active, otherwise:
#   {"pubkey":, "sender":, "last_activity": <time.time()>, "warned": bool}
ACTIVE_SESSION = None


def _activate_session(pubkey, sender):
    """Hand the lock to pubkey, starting a fresh inactivity clock."""
    global ACTIVE_SESSION
    ACTIVE_SESSION = {
        "pubkey": pubkey,
        "sender": sender,
        "last_activity": time.time(),
        "warned": False,
    }


def _touch_session(pubkey):
    """Reset the inactivity clock -- called on every message the active
    player sends, so any reply (not just gameplay commands) counts as
    activity."""
    if ACTIVE_SESSION and ACTIVE_SESSION["pubkey"] == pubkey:
        ACTIVE_SESSION["last_activity"] = time.time()
        ACTIVE_SESSION["warned"] = False


def _release_session(pubkey):
    """Free the lock if pubkey currently holds it, and drop any
    trade/warp/attack state they had in progress -- that state only makes
    sense mid-session, and leaving it around would let a *later* session
    for the same pubkey resume a stale flow unexpectedly."""
    global ACTIVE_SESSION
    PENDING_TRADES.pop(pubkey, None)
    PENDING_WARPS.pop(pubkey, None)
    PENDING_UPGRADES.pop(pubkey, None)
    PENDING_ATTACKS.pop(pubkey, None)
    PENDING_STATIONS.pop(pubkey, None)
    PENDING_P2P.pop(pubkey, None)
    PENDING_TOWS.pop(pubkey, None)
    PENDING_BOARDS.pop(pubkey, None)
    if ACTIVE_SESSION and ACTIVE_SESSION["pubkey"] == pubkey:
        ACTIVE_SESSION = None


async def monitor_inactivity(mc):
    """
    Background loop pairing with the lock above: warn the active player
    once after INACTIVITY_WARNING_SECONDS of silence, then free the lock
    after INACTIVITY_KICK_SECONDS total. This is what recovers the game
    when a player's radio drops out of range, their app crashes, or they
    just wander off -- we can't distinguish those from a slow reply, so
    we just give it a generous timeout either way.
    """
    while True:
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL_SECONDS)

        session = ACTIVE_SESSION
        if session is None:
            continue

        idle_for = time.time() - session["last_activity"]
        pubkey = session["pubkey"]
        sender = session["sender"]

        if idle_for >= INACTIVITY_KICK_SECONDS:
            print(f"→ {sender} timed out after {idle_for:.0f}s idle, freeing lock")
            _release_session(pubkey)
            await send_reply(
                mc, pubkey, sender,
                "Logged out for inactivity so another player can sign in. "
                "Reply with anything to sign back in."
            )
        elif idle_for >= INACTIVITY_WARNING_SECONDS and not session["warned"]:
            session["warned"] = True
            minutes_left = max(1, round((INACTIVITY_KICK_SECONDS - idle_for) / 60))
            print(f"→ warning {sender}, idle {idle_for:.0f}s")
            await send_reply(
                mc, pubkey, sender,
                f"Still there? You'll be logged out in about {minutes_left} "
                "min if you don't reply."
            )
