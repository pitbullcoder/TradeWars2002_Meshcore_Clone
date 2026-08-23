"""
Trade Wars-style space trading game over a MeshCore/Meshtastic radio mesh.

This module is the orchestrator: it wires the radio event loop to the
command registry, owns the session sign-in briefing and the channel
advertisement schedule, and re-exports the names the test suite reaches
for. The bulk of the game lives in focused modules:

    core         shared state, command registry, Ctx, parse
    messaging    chunking + send/ack transport
    display      sector/menu/kill-log rendering
    pathfinding  warp-graph traversal + escape-pod placement
    combat       mine-damage and fighter-attrition math
    attack       combat & recon command handlers (lay, probe, attack)
    movement     arrival/relocation: move, warp, tow, board, enter_sector
    trading      port trade + Stardock refit/shipyard flows
    p2p          the port-to-port auto-shuttle
    station      space station deploy/management flows
    session      single-helm lock + inactivity timeout

The RNG seams live in movement.random and attack.random (the modules
that roll mine damage, pod drift, and knockback); the reset block below
restores them to the real random module on every (re)load, so the test
suite's importlib.reload(main) hands each test a clean slate.
"""

import asyncio
import random
import re
import time

from datetime import datetime

from meshcore import MeshCore, EventType

from db import (
    init_db,
    log_message,
    get_or_create_player,
    reset_turns_if_needed,
    get_player_with_ship,
    pop_attack_events,
    get_kills_since,
    get_kill_log_cutoff,
    mark_kill_log_seen,
    get_stations_by_owner,
    apply_station_upkeep,
    get_last_tx_time,
    get_ship,
    set_ship_cargo,
    TOW_TURNS_PER_SECTOR,
)
# Re-exported (via __all__) for the test suite, which reaches it as
# main.get_station_in_sector.
from db import get_station_in_sector

import session
from session import (
    _activate_session,
    _touch_session,
    _release_session,
    monitor_inactivity,
)
from core import (
    Ctx,
    COMMANDS,
    command,
    CHANNEL_COMMANDS,
    parse,
    PENDING_WARPS,
    PENDING_TRADES,
    PENDING_UPGRADES,
    PENDING_ATTACKS,
    PENDING_STATIONS,
    PENDING_P2P,
    PENDING_TOWS,
    PENDING_BOARDS,
    _JETTISON_COMMODITIES,
    _resolve_commodity,
)
from messaging import (
    send_reply, send_channel_reply, is_stale_message, format_rx_path,
)
from display import (
    build_menu,
    build_submenu,
    build_sector_info,
    format_port_line,
    format_warps_line,
    format_attack_notices,
    format_kill_log,
)
# Re-exported (via __all__) so the test suite can reach it as
# main.KILL_LOG_MAX_ENTRIES.
from display import KILL_LOG_MAX_ENTRIES
# sectors_within_hop_range is re-exported (via __all__) so the test suite
# can reach it as main.sectors_within_hop_range.
from pathfinding import choose_escape_sector, sectors_within_hop_range
from combat import apply_mine_damage, resolve_attack
from trading import cmd_trade, cmd_trade_step, cmd_stardock_step
from station import cmd_deploy, cmd_station, cmd_station_step

print("MeshCore bot started...")

# Reset shared mutable state on every (re)load. The dicts physically live
# in `core` and are never replaced, so submodules holding references to
# them stay valid; clearing (not rebinding) is what lets the test suite's
# importlib.reload(main) hand each test a clean slate.
PENDING_WARPS.clear()
PENDING_TRADES.clear()
PENDING_UPGRADES.clear()
PENDING_ATTACKS.clear()
PENDING_STATIONS.clear()
PENDING_P2P.clear()
PENDING_TOWS.clear()
PENDING_BOARDS.clear()
session.ACTIVE_SESSION = None

# Public surface this module deliberately exposes -- notably the handlers
# and helpers the test suite reaches for as main.<n>, including several
# (apply_mine_damage, choose_escape_sector, sectors_within_hop_range, and
# every command defined in attack/movement/p2p/trading/station) that are
# defined in sibling modules and re-exported here on purpose.
__all__ = [
    "Ctx",
    "cmd_menu", "cmd_quit", "cmd_info", "cmd_status", "cmd_jettison",
    "cmd_combat", "cmd_lay_mines", "cmd_probe", "cmd_attack", "cmd_attack_step",
    "cmd_move", "cmd_confirm_warp",
    "cmd_trade", "cmd_trade_step", "cmd_stardock_step",
    "cmd_p2p", "cmd_p2p_step",
    "cmd_deploy", "cmd_station", "cmd_station_step",
    "cmd_tow", "cmd_tow_step", "cmd_board", "cmd_board_step",
    "enter_sector", "run_probe", "resolve_attack",
    "apply_mine_damage", "choose_escape_sector",
    "sectors_within_hop_range",
    "get_station_in_sector", "KILL_LOG_MAX_ENTRIES",
    "PENDING_WARPS", "PENDING_TRADES", "PENDING_UPGRADES", "PENDING_ATTACKS",
    "PENDING_STATIONS", "PENDING_P2P", "PENDING_TOWS", "PENDING_BOARDS",
    "on_message", "on_channel_message", "main",
    "maybe_advertise", "advertise_loop",
]


PUBLIC_CHANNEL_IDX = 0  # which channel index the bot listens to for public commands


# --- Companion connection ------------------------------------------------
# The game talks to a companion identity hosted by the openHop Repeater
# daemon on this same Pi, over its MeshCore frame protocol on TCP --
# rather than to a USB-attached companion radio. openHop serves one
# client per companion port, so this port is the game's alone (the
# weather and news bots hold their own identities on their own ports).
COMPANION_HOST = "127.0.0.1"
COMPANION_PORT = 5052


# Unlike a USB link, the transport here is a local service that gets
# restarted for config changes and upgrades -- routine, not exceptional.
# meshcore_py re-sends CMD_APP_START after each reconnect, which is what
# the frame server needs to re-establish the session; subscriptions and
# auto message fetching survive since the MeshCore object itself does.
COMPANION_RECONNECT_ATTEMPTS = 10


# --- Channel advertisement ----------------------------------------------
# Every ADVERT_INTERVAL_SECONDS the bot broadcasts an invitation on the
# public channel. The schedule keys off the messages log (the last time
# ADVERT_TEXT was actually transmitted), so restarting the bot resumes
# the countdown rather than re-advertising early; a fresh install with no
# prior broadcast advertises immediately. Kept under one radio chunk (130
# chars) so the logged text matches ADVERT_TEXT exactly.
ADVERT_TEXT = "Tradewars 2002 is ready! Submit an advert flood routed then DM me to play!"


# Master switch for the public channel ad. Set False to silence it
# (advertise_loop keeps running but maybe_advertise sends nothing).
# Since a disabled stretch logs no transmissions, flipping this back on
# after 48+ quiet hours broadcasts on the next scheduler wake-up.
ADVERT_ENABLED = True


ADVERT_INTERVAL_SECONDS = 48 * 60 * 60  # once every 48 hours


# How often advertise_loop wakes to check whether an ad is due. Short
# check naps (instead of one 48-hour sleep) keep the schedule accurate
# across system clock hiccups and retry failed sends within minutes.
ADVERT_CHECK_INTERVAL_SECONDS = 10 * 60


def _seconds_until_next_advert():
    """How long until the next channel ad is due: 0 if it's never been
    broadcast (or the last one was ADVERT_INTERVAL_SECONDS+ ago), else
    the remainder of the 48-hour countdown from the last transmission."""
    last = get_last_tx_time(f"chan{PUBLIC_CHANNEL_IDX}", ADVERT_TEXT)
    if last is None:
        return 0
    elapsed = time.time() - datetime.fromisoformat(last).timestamp()
    return max(0.0, ADVERT_INTERVAL_SECONDS - elapsed)


async def maybe_advertise(mc):
    """Broadcast the invitation if one is due. Returns True if it was
    sent (send_channel_reply logs the transmission, which is what arms
    the next 48-hour countdown). Split out from advertise_loop so the
    decision + send is testable without an infinite loop."""
    if not ADVERT_ENABLED:
        return False
    if _seconds_until_next_advert() > 0:
        return False
    print("→ broadcasting channel advertisement")
    await send_channel_reply(mc, PUBLIC_CHANNEL_IDX, ADVERT_TEXT)
    return True


async def advertise_loop(mc):
    """Background task: wake every ADVERT_CHECK_INTERVAL_SECONDS and
    broadcast the ad whenever 48 hours have passed since the last one.
    A failed send isn't logged as transmitted, so it's simply retried on
    the next wake-up."""
    while True:
        await maybe_advertise(mc)
        await asyncio.sleep(ADVERT_CHECK_INTERVAL_SECONDS)


# Matches anything that *looks* like a number a player might type as a
# move request -- including negatives and decimals -- so we can route it
# to cmd_move for a specific validation error, rather than letting it fall
# through to the generic "Unknown command" reply.
_NUMBER_LIKE = re.compile(r"^-?\d+(\.\d+)?$")


@command("menu", "help", "?", description="list commands ('help combat' for combat)")
async def cmd_menu(ctx, args):
    sub = args.strip().lower()
    if sub:
        return build_submenu(sub)
    return build_menu()


@command("combat", description="combat & recon commands (lay mines, send probes)")
async def cmd_combat(ctx, args):
    return build_submenu("combat")


@command("quit", "logout", description="sign off so another player can take a turn")
async def cmd_quit(ctx, args):
    _release_session(ctx.pubkey)
    return "You've signed off. Reply with anything to sign back in later."


@command("info", "i", description="show info for your current sector")
async def cmd_info(ctx, args):
    return build_sector_info(ctx.player["sector_id"], ctx.player["id"])


@command("status", "st", description="show credits, sector, ship, turns")
async def cmd_status(ctx, args):
    p = ctx.player
    defenses = f"Cargo Holds {p['holds_total']} Fighters {p['fighters']} Shields {p['shields']}"
    if p["mines"] > 0:
        defenses += f" Mines {p['mines']}"
    if p["probes"] > 0:
        defenses += f" Probes {p['probes']}"
    towing_line = ""
    if p.get("towing_ship_id"):
        towed = get_ship(p["towing_ship_id"])
        if towed is not None:
            towing_line = (
                f"\nTowing {towed['ship_type']} #{towed['id']} "
                f"({TOW_TURNS_PER_SECTOR} turns/sector)"
            )
    return (
        f"Sec{p['sector_id']} {p['credits']}cr {p['turns_remaining']}turn\n"
        f"{p['ship_type']}{towing_line}\n"
        f"{defenses}\n"
        f"fuel{p['fuel_ore']} organics{p['organics']} equipment{p['equipment']}\n"
        f"{format_warps_line(p['sector_id'])}\n"
        f"{format_port_line(p['sector_id'])}"
    )


def _cargo_aboard_line(p):
    """'fuel ore 10, organics 5, equipment 0' -- the full cargo manifest in
    the usual order, for jettison's bare-command inventory prompt."""
    return ", ".join(f"{label} {p[key]}" for label, key, _ in _JETTISON_COMMODITIES)


@command("jettison", "jet", description="dump cargo from holds: 'jettison <all|commodity> [n]'")
async def cmd_jettison(ctx, args):
    """
    Space commodity cargo out of the holds -- a pure loss (no credits),
    used to free up holds (e.g. to make room for a Station Core kit or a
    different commodity). A free action like trading/docking, so it costs
    no turn, and it's allowed anywhere. Forms:

      jettison                  -- show what's aboard + usage; dumps nothing,
                                   so a bare 'jettison' can't space a hold
                                   by accident
      jettison all              -- dump all three commodities at once
      jettison <commodity>      -- dump all of one (fuel/organics/equipment)
      jettison <commodity> <n>  -- dump n units of one

    Only the three tradeable commodities are touched; a carried Station
    Core kit is left alone (deploy or sell it to offload that).
    """
    p = ctx.player
    parts = args.split()
    total_cargo = p["fuel_ore"] + p["organics"] + p["equipment"]

    # Bare 'jettison': show the manifest and usage, but dump nothing.
    if not parts:
        if total_cargo <= 0:
            return "Your holds are empty -- nothing to jettison."
        return (
            f"Aboard: {_cargo_aboard_line(p)}.\n"
            "Jettison what? 'jettison all', or 'jettison <commodity> [amount]'."
        )

    first = parts[0].lower()

    # 'jettison all': clear every hold in one go.
    if first == "all":
        if total_cargo <= 0:
            return "Your holds are empty -- nothing to jettison."
        dumped = ", ".join(
            f"{p[key]} {label}"
            for label, key, _ in _JETTISON_COMMODITIES if p[key] > 0
        )
        set_ship_cargo(p["id"], 0, 0, 0)
        return f"Jettisoned all cargo ({dumped}) into space. Holds cleared."

    resolved = _resolve_commodity(first)
    if resolved is None:
        return (
            "Jettison what? Try 'all', or one of fuel/organics/equipment "
            "(optionally with an amount)."
        )
    label, key = resolved
    aboard = p[key]
    if aboard <= 0:
        return f"No {label} aboard to jettison."

    # Optional amount; with no number given, dump all of that commodity.
    if len(parts) >= 2:
        amount_arg = parts[1]
        if not re.match(r"^\d+$", amount_arg):
            return (
                f"Enter a whole number of {label} to jettison, "
                f"or just 'jettison {first}' for all of it."
            )
        qty = int(amount_arg)
        if qty == 0:
            return "Jettison how many? Enter a number from 1 up."
        if qty > aboard:
            return f"You only have {aboard} {label} aboard."
    else:
        qty = aboard

    # Write the one commodity back down by qty, leaving the others as-is.
    amounts = {c[1]: p[c[1]] for c in _JETTISON_COMMODITIES}
    amounts[key] -= qty
    set_ship_cargo(p["id"], amounts["fuel_ore"], amounts["organics"], amounts["equipment"])

    left = aboard - qty
    return f"Jettisoned {qty} {label} into space; {left} still aboard."


# These imports run AFTER the command definitions above so the help menu
# keeps its original ordering: @command registers into core.COMMANDS at
# import time, and build_menu lists commands in registration order --
# trading/station (top of file), then this module's own commands, then
# lay/probe/attack, tow/board, and p2p. p2p imports movement itself, so
# the order here also keeps the movement import ahead of it.
from attack import (  # noqa: E402
    cmd_lay_mines, cmd_probe, run_probe, cmd_attack, cmd_attack_step,
)
from movement import (  # noqa: E402
    enter_sector, cmd_move, cmd_confirm_warp,
    cmd_tow, cmd_tow_step, cmd_board, cmd_board_step,
)
from p2p import cmd_p2p, cmd_p2p_step  # noqa: E402
import attack  # noqa: E402
import movement  # noqa: E402

# Restore the RNG seams in the feature modules: reload(main) is the test
# suite's between-test reset, and the feature modules themselves are NOT
# re-imported by it, so a FakeRandom installed by one test (as
# movement.random or attack.random) must not leak into the next.
movement.random = random
attack.random = random


async def on_channel_message(mc, event):
    payload = getattr(event, "payload", {})
    channel_idx = payload.get("channel_idx", -1)
    text = payload.get("text", "")

    if is_stale_message(payload):
        age = time.time() - payload["sender_timestamp"]
        print(f"CHAN[{channel_idx}] ignoring stale message (age {age:.0f}s): {text}")
        return

    # Some apps prefix the sender's nickname, e.g. "alice: weather 43215".
    # Channel messages carry no pubkey, so this is best-effort only.
    if ":" in text:
        _, _, after_colon = text.partition(":")
        content = after_colon.strip()
    else:
        content = text.strip()

    print(f"CHAN[{channel_idx}] RX: {text}")
    log_message("rx", f"chan{channel_idx}", "channel", text)

    verb, args = parse(content)
    handler = CHANNEL_COMMANDS.get(verb)
    if handler is None:
        return  # not a recognized public-channel command; stay quiet

    response = await handler(args)
    await send_channel_reply(mc, channel_idx, response)


async def on_message(mc, event):
    payload = getattr(event, "payload", {})
    pubkey = payload.get("pubkey_prefix", "UNKNOWN")
    message = payload.get("text", "")

    if is_stale_message(payload):
        age = time.time() - payload["sender_timestamp"]
        print(f"RX from {pubkey} ignoring stale message (age {age:.0f}s): {message}")
        return

    contact = mc.get_contact_by_key_prefix(pubkey)
    sender = contact["adv_name"] if contact else pubkey[:8]

    # Inbound DMs carry only a hop count (not the hop hashes), so this
    # logs 'direct' / 'N hops', or None on firmware that omits it.
    rx_path = format_rx_path(payload)
    print(f"RX from {sender} [{rx_path or 'path unknown'}]: {message}")
    log_message("rx", pubkey, sender, message, rx_path)

    player, is_new = get_or_create_player(pubkey, sender)

    if is_new:
        print(f"→ new player onboarded: {sender}")
        welcome = (
            f"Welcome {sender}! Sec{player['sector_id']} "
            f"{player['credits']}cr {player['turns_remaining']}trn. "
            f"Reply 'menu' for commands."
        )
        await send_reply(mc, pubkey, sender, welcome)
        return

    reset_turns_if_needed(player["id"])
    player = get_player_with_ship(pubkey)  # re-fetch in case turns were just reset

    # Lockout: someone else is at the helm -- turn this sender away
    # without touching any game state. New players still get onboarded
    # above regardless of the lock; it's gameplay commands that wait.
    if session.ACTIVE_SESSION is not None and session.ACTIVE_SESSION["pubkey"] != pubkey:
        other = session.ACTIVE_SESSION["sender"]
        print(f"→ {sender} turned away, {other} is active")
        await send_reply(
            mc, pubkey, sender,
            f"{other} is currently at the helm. Try again in a few minutes."
        )
        return

    signin_notice = ""
    if session.ACTIVE_SESSION is None:
        if player["turns_remaining"] <= 0:
            print(f"→ {sender} has no turns left, not activating")
            await send_reply(
                mc, pubkey, sender,
                "You're out of turns for now. Check back after they reset."
            )
            return
        _activate_session(pubkey, sender)
        print(f"→ {sender} is now active")
        # Sign-in briefing, assembled before any command runs: the player's
        # personal "while you were away" combat notices, then the public
        # kill log -- every ship/pod lost (to anyone, by combat or mines)
        # since they last signed in. Read the kill cutoff first, then
        # advance it, so this window is reported exactly once.
        notices = []
        events = pop_attack_events(player["id"])
        if events:
            notices.append(format_attack_notices(events))
        # Bring the player's own stations up to date (daily shield fuel burn
        # and any completed upgrades) -- lazy upkeep, evaluated on sign-in.
        for st in get_stations_by_owner(player["id"]):
            apply_station_upkeep(st["id"])
        cutoff = get_kill_log_cutoff(player["id"])
        kills = get_kills_since(cutoff)
        mark_kill_log_seen(player["id"])
        kill_log = format_kill_log(kills)
        if kill_log:
            notices.append(kill_log)
        if notices:
            signin_notice = "\n\n".join(notices) + "\n"
    else:
        _touch_session(pubkey)

    ctx = Ctx(mc, pubkey, sender, player)

    if pubkey in PENDING_TRADES:
        response = await cmd_trade_step(ctx, message)
    elif pubkey in PENDING_UPGRADES:
        response = await cmd_stardock_step(ctx, message)
    elif pubkey in PENDING_ATTACKS:
        response = await cmd_attack_step(ctx, message)
    elif pubkey in PENDING_STATIONS:
        response = await cmd_station_step(ctx, message)
    elif pubkey in PENDING_P2P:
        response = await cmd_p2p_step(ctx, message)
    elif pubkey in PENDING_TOWS:
        response = await cmd_tow_step(ctx, message)
    elif pubkey in PENDING_BOARDS:
        response = await cmd_board_step(ctx, message)
    elif pubkey in PENDING_WARPS:
        response = await cmd_confirm_warp(ctx, message)
    else:
        stripped = message.strip()
        if _NUMBER_LIKE.match(stripped):
            response = await cmd_move(ctx, stripped)
        else:
            verb, args = parse(message)
            if verb in COMMANDS:
                _, handler = COMMANDS[verb]
                response = await handler(ctx, args)
            else:
                print(f"→ unrecognized command from {sender}")
                response = "Unknown command. Reply 'menu' for list."

    # Re-fetch so a move/trade that just spent the player's last turn is
    # reflected here, then free the lock if they're out so the next
    # player isn't stuck waiting on the inactivity timeout.
    player = get_player_with_ship(pubkey)
    if (
        player["turns_remaining"] <= 0
        and session.ACTIVE_SESSION is not None
        and session.ACTIVE_SESSION["pubkey"] == pubkey
    ):
        _release_session(pubkey)
        response += "\n\nYou're out of turns. Logged out to let someone else play."

    if signin_notice:
        response = signin_notice + response

    print(f"→ replying to {sender}: {response}")
    await send_reply(mc, pubkey, sender, response)


async def main():
    init_db()

    mc = await MeshCore.create_tcp(
        COMPANION_HOST, COMPANION_PORT,
        auto_reconnect=True, max_reconnect_attempts=COMPANION_RECONNECT_ATTEMPTS,
    )
    print(f"Connected OK ({COMPANION_HOST}:{COMPANION_PORT})")

    result = await mc.commands.get_contacts()
    if result.type == EventType.ERROR:
        print(f"Error getting contacts: {result.payload}")

    # Refetch the contact list whenever the radio pushes a PATH_UPDATE
    # (or a new advert). Off by default in meshcore_py, which leaves
    # mc.contacts frozen at the startup snapshot above -- so radio_path
    # would report a contact's route as it stood at boot (e.g. [flood]
    # forever) even after the firmware learns a direct path, and
    # send_msg_with_retry would keep applying flood retry limits to it.
    # Refetches are incremental (lastmod-based) over the serial link, so
    # this costs no radio airtime.
    mc.auto_update_contacts = True

    await mc.start_auto_message_fetching()

    mc.subscribe(
        EventType.CONTACT_MSG_RECV,
        lambda event: asyncio.create_task(on_message(mc, event))
    )

    mc.subscribe(
        EventType.CHANNEL_MSG_RECV,
        lambda event: asyncio.create_task(on_channel_message(mc, event)),
        attribute_filters={"channel_idx": PUBLIC_CHANNEL_IDX}
    )

    asyncio.create_task(monitor_inactivity(mc))
    asyncio.create_task(advertise_loop(mc))

    print("Bot is running...")
    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
