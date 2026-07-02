"""
Trade Wars-style space trading game over a MeshCore/Meshtastic radio mesh.

This module is the orchestrator: it wires the radio event loop to the
command registry, owns the player-arrival path (enter_sector and the
movement commands, which is also where mine detonation is resolved), and
re-exports the names the test suite reaches for. The bulk of the game now
lives in focused modules:

    core         shared state, command registry, Ctx, parse
    messaging    chunking + send/ack transport
    display      sector/menu rendering
    pathfinding  warp-graph traversal + escape-pod placement
    combat       mine-damage math
    trading      port trade + Stardock refit/shipyard flows
    session      single-helm lock + inactivity timeout

enter_sector stays here on purpose: it defaults its RNG to this module's
`random`, which is the seam the tests monkeypatch.
"""

import asyncio
import random
import re
import time

from meshcore import MeshCore, EventType

from db import (
    init_db,
    log_message,
    get_or_create_player,
    reset_turns_if_needed,
    spend_turn,
    get_player_with_ship,
    get_adjacent_sectors,
    get_all_warps,
    get_port,
    execute_trade,
    apply_port_restock,
    move_player_to_sector,
    buy_ship,
    lay_mines,
    get_hostile_mine_total,
    clear_hostile_mines,
    set_ship_defenses,
    set_ship_cargo,
    consume_probe,
    detonate_one_hostile_mine,
    get_ships_in_sector,
    record_attack_event,
    pop_attack_events,
    record_kill,
    get_kills_since,
    get_kill_log_cutoff,
    mark_kill_log_seen,
    SHIP_CATALOG,
    ESCAPE_POD_SHIP,
    DEFAULT_SHIP_TYPE,
    HOME_SECTOR,
    SAFE_ZONE_MAX_SECTOR,
    get_station_in_sector,
    get_stations_by_owner,
    apply_station_upkeep,
    set_station_defenses,
    delete_station,
    get_ships_docked_at_station,
    get_ship,
    get_parked_ships_in_sector,
    set_parked_ship_defenses,
    move_ship_to_sector,
    delete_ship,
    set_towing,
    swap_active_ship,
    spend_turns,
)

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
    ship_label,
    _warp_confirm_options,
    _resume_navigation_suffix,
)
from messaging import send_reply, send_channel_reply, is_stale_message
from display import build_menu, build_submenu, build_sector_info, format_port_line, format_warps_line
from pathfinding import find_shortest_path, choose_escape_sector
# Re-exported so the test suite can reach it as main.sectors_within_hop_range.
from pathfinding import sectors_within_hop_range  # noqa: F401
from combat import roll_mine_damage, apply_mine_damage, resolve_attack, _plural
from trading import cmd_trade, cmd_trade_step, cmd_stardock_step
from station import (
    cmd_deploy, cmd_station, cmd_station_step,
    engaged_fighters,
)

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
# and helpers the test suite reaches for as main.<name>, including a couple
# (apply_mine_damage, choose_escape_sector, sectors_within_hop_range) that
# are defined in sibling modules and re-exported here on purpose.
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
    "PENDING_WARPS", "PENDING_TRADES", "PENDING_UPGRADES", "PENDING_ATTACKS",
    "PENDING_STATIONS", "PENDING_P2P", "PENDING_TOWS", "PENDING_BOARDS",
    "on_message", "on_channel_message", "main",
]


PUBLIC_CHANNEL_IDX = 0  # which channel index the bot listens to for public commands


MIN_SECTOR_ID = 1


MAX_SECTOR_ID = 1000  # matches galaxy.py's NUM_SECTORS


# --- Safe zone ---------------------------------------------------------
# Sectors 1..SAFE_ZONE_MAX_SECTOR (imported from db) are a protected zone
# around the Stardock: no mines may be laid there and no ship-to-ship
# combat is allowed, so new players can't be ambushed the moment they
# leave the Stardock. Galaxy generation also fully interconnects these
# sectors, so the Stardock stays reachable and can't be walled off.


# --- Ship combat balance ----------------------------------------------
# When a pilot already in an escape pod is finished off, they don't just
# lose the pod -- they're wiped back to a fresh start: a default-hull ship
# at the home sector with their credits reset to this amount.
POD_KILL_RESET_CREDITS = 20000


# --- Towing ------------------------------------------------------------
# Dragging one of your own unmanned hulls behind you. Slow going: every
# sector moved while towing burns this many turns instead of one, and a
# move is refused outright when fewer remain (rather than burning the
# player down to zero mid-haul).
TOW_TURNS_PER_SECTOR = 5


def _tow_move_block(p):
    """The refusal message if `p` is towing but can't afford a towed
    move (fewer than TOW_TURNS_PER_SECTOR turns left), else None. Called
    before any player-initiated sector move actually happens, so a
    too-expensive move never half-fires."""
    if p.get("towing_ship_id") and p["turns_remaining"] < TOW_TURNS_PER_SECTOR:
        return (
            f"Towing burns {TOW_TURNS_PER_SECTOR} turns per sector and you have "
            f"{p['turns_remaining']} left. Release the tow ('tow') to travel light, "
            "or wait for the daily reset."
        )
    return None


# --- Public kill log --------------------------------------------------
# At most this many kill-log entries are shown at sign-in (the most recent
# ones), with a one-line note counting any older kills not shown. The log
# covers "everything since you last played", which over a busy stretch
# could be a lot -- this keeps the sign-in briefing from flooding a slow
# radio link while still surfacing the full count.
KILL_LOG_MAX_ENTRIES = 20


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


# Cargo commodities a player can jettison, each with the argument aliases
# it accepts. Order matches the fuel/organics/equipment order used
# everywhere else (status, trading). The Station Core kit is deliberately
# NOT here -- it's a STATION_CORE_PRICE-cr fixture offloaded by deploying
# or selling it, never spaced by accident through this command.
_JETTISON_COMMODITIES = [
    ("fuel ore", "fuel_ore", ("fuel", "fuel_ore", "ore")),
    ("organics", "organics", ("organics", "org")),
    ("equipment", "equipment", ("equipment", "equip")),
]


def _resolve_commodity(arg):
    """Map a commodity argument to its (label, ship column), or None if it
    doesn't name one of the three cargo commodities."""
    arg = arg.lower()
    for label, key, aliases in _JETTISON_COMMODITIES:
        if arg in aliases:
            return label, key
    return None


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


@command("lay", "mine", description="lay mines in this sector: 'lay <n>'", menu="combat")
async def cmd_lay_mines(ctx, args):
    """
    Deploy mines from the ship into the current sector, where they wait
    for the next pilot who isn't their owner (the owner can re-enter
    safely -- see enter_sector). Banned in the Sec1..SAFE_ZONE_MAX_SECTOR
    safe zone. Only ships with a mine bay ever carry mines to begin with,
    so a Falcon (mines always 0) is turned away by the "none aboard"
    check without needing a separate hull test.
    """
    p = ctx.player
    sector_id = p["sector_id"]

    if sector_id <= SAFE_ZONE_MAX_SECTOR:
        return (
            f"Can't lay mines in Sec{sector_id} -- the Sec1-{SAFE_ZONE_MAX_SECTOR} "
            "safe zone is off limits."
        )

    aboard = p["mines"]
    if aboard <= 0:
        return "No mines aboard. Buy some at a Stardock (needs a ship with a mine bay)."

    arg = args.strip()
    if not arg:
        return f"Lay how many mines? You have {aboard} aboard. Try 'lay <n>'."
    if not re.match(r"^\d+$", arg):
        return f"Enter a whole number of mines to lay. You have {aboard} aboard."

    qty = int(arg)
    if qty == 0:
        return "Lay how many? Enter a number from 1 up."
    if qty > aboard:
        return f"You only have {aboard} mines aboard."

    lay_mines(p["id"], sector_id, qty)
    left = aboard - qty
    return (
        f"Laid {_plural(qty, 'mine')} in Sec{sector_id}; {left} still aboard. "
        "They'll detonate on the next pilot through who isn't you."
    )


@command("probe", description="send a recon probe to a sector: 'probe <n>'", menu="combat")
async def cmd_probe(ctx, args):
    """
    Launch a recon probe toward a target sector. The probe follows the
    same shortest-path route a piloted warp would (see cmd_move), but the
    player stays put -- it's remote scouting. It reports each sector it
    passes through, just as the player would see on arrival, and is
    consumed on launch whether or not it makes it.

    A probe is fragile: a single hostile mine in any sector it enters
    destroys it on the spot (that one mine is spent; the rest of the
    field stays put for real ships). Probes are bought at a Stardock.
    """
    p = ctx.player

    if p["probes"] <= 0:
        return "No probes aboard. Buy some at a Stardock (100cr each)."

    arg = args.strip()
    if not arg:
        return f"Send a probe where? You have {p['probes']}. Try 'probe <sector>'."
    if not re.match(r"^\d+$", arg):
        return f"'{arg}' isn't a sector number. Try 'probe <sector>'."

    target = int(arg)
    if target < MIN_SECTOR_ID or target > MAX_SECTOR_ID:
        return f"Sec{target} is out of range. Sectors range from {MIN_SECTOR_ID} to {MAX_SECTOR_ID}."
    if target == p["sector_id"]:
        return "The probe's already in your sector -- send it somewhere else."

    graph = get_all_warps()
    path = find_shortest_path(graph, p["sector_id"], target)
    if path is None:
        return f"No route found to Sec{target}."

    consume_probe(p["id"])
    return run_probe(p, path)


def run_probe(p, path):
    """
    Fly a launched probe along `path` (which starts at the player's
    current sector) and build its travelogue. Each sector it reaches is
    reported with the same info screen the player would see there. The
    first sector holding a hostile mine destroys the probe -- one mine is
    spent (detonate_one_hostile_mine), the report ends there, and the
    rest of the route goes unscouted. Returns the full report string.
    """
    hops = path[1:]  # the player's current sector isn't re-reported
    left = p["probes"] - 1  # one was just consumed launching this probe
    lines = [f"Probe away to Sec{path[-1]} ({len(hops)} hops); {left} left aboard."]
    for sector_id in hops:
        if get_hostile_mine_total(sector_id, p["id"]) > 0:
            detonate_one_hostile_mine(sector_id, p["id"])
            lines.append(f"Sec{sector_id}: a mine detonates -- PROBE DESTROYED here.")
            break
        lines.append(build_sector_info(sector_id, p["id"]))
    else:
        lines.append(f"Probe reached Sec{path[-1]} and signs off.")
    return "\n".join(lines)


@command("a", "attack", description="attack a ship here: 'a', 'a <name>', or 'a #<ship id>'", menu="combat")
async def cmd_attack(ctx, args):
    """
    Aim an attack at something in your sector: another pilot's ship, an
    unmanned (parked) ship, or an enemy space station. Three forms:

      a <name>     -- target a pilot by name directly
      a #<ship id> -- target an unmanned ship by its id (as shown on the
                      sector-info "Unmanned:" line)
      a station    -- target the enemy station here
      a            -- guided: cycles yes/no through every target present,
                      pilots first, then unmanned ships, then the station

    Rather than throwing every fighter at once, a chosen target leads to
    a "how many fighters?" prompt (see cmd_attack_step, which resolves
    via resolve_attack). Combat is banned in the
    Sec1..SAFE_ZONE_MAX_SECTOR safe zone -- which also makes ships parked
    there (e.g. at the Stardock) unattackable. You can't attack your own
    parked ships. Sets up PENDING_ATTACKS; the follow-up reply is routed
    to cmd_attack_step by on_message.
    """
    p = ctx.player  # attacker

    if p["sector_id"] <= SAFE_ZONE_MAX_SECTOR:
        return (
            f"No combat in the Sec1-{SAFE_ZONE_MAX_SECTOR} safe zone. "
            "Catch them outside it to open fire."
        )

    foes = get_ships_in_sector(p["sector_id"], p["id"])
    parked_here = get_parked_ships_in_sector(p["sector_id"])
    enemy_parked = [s for s in parked_here if s["owner_id"] != p["id"]]
    station = get_station_in_sector(p["sector_id"])
    enemy_station = station if (station is not None and station["owner_id"] != p["id"]) else None

    arg = args.strip()
    if arg.lower() == "station":
        if enemy_station is None:
            return "There's no enemy station here to attack."
        return _aim_attack(ctx, _station_target(enemy_station))

    id_match = re.match(r"^#(\d+)$", arg)
    if id_match:
        ship_id = int(id_match.group(1))
        ship = next((s for s in parked_here if s["id"] == ship_id), None)
        if ship is None:
            return f"No unmanned ship #{ship_id} here."
        if ship["owner_id"] == p["id"]:
            return f"{ship_label(ship)} is your own ship -- you can't attack it."
        return _aim_attack(ctx, _parked_target(ship))

    if arg:
        ship = next((f for f in foes if f["name"].lower() == arg.lower()), None)
        if ship is None:
            here = ", ".join(f["name"] for f in foes) or "none"
            hint = " (or 'a station')" if enemy_station else ""
            if enemy_parked:
                hint += " ('a #<id>' for an unmanned ship)"
            return f"No ship named '{arg}' here. Ships here: {here}{hint}."
        return _aim_attack(ctx, _player_target(ship))

    # Bare 'a': walk the targets one at a time -- pilots, then unmanned
    # ships, then the enemy station -- asking yes/no for each.
    queue = (
        [_player_target(f) for f in foes]
        + [_parked_target(s) for s in enemy_parked]
        + ([_station_target(enemy_station)] if enemy_station else [])
    )
    if not queue:
        return "No other ships here to attack."
    if p["fighters"] <= 0:
        return "You have no fighters to attack with."
    PENDING_ATTACKS[ctx.pubkey] = {"stage": "choose", "queue": queue, "idx": 0}
    return _attack_choose_prompt(queue, 0)


def _attack_choose_prompt(queue, idx):
    target = queue[idx]
    more = " (no = next target)" if idx + 1 < len(queue) else ""
    return f"Attack {target['name']} ({target['fighters']} ftr)? yes/no{more}"


def _player_target(ship):
    """Attack-target dict for another pilot's (manned) ship."""
    return {"kind": "player", "id": ship["id"], "name": ship["name"],
            "fighters": ship["fighters"]}


def _parked_target(ship):
    """Attack-target dict for an unmanned parked/towed ship, shaped like
    the others (with is_parked to branch on at resolution)."""
    return {
        "kind": "parked",
        "is_parked": True,
        "ship_id": ship["id"],
        "owner_id": ship["owner_id"],
        "name": ship_label(ship),
        "fighters": ship["fighters"],
        "shields": ship["shields"],
    }


def _aim_attack(ctx, target):
    """Lock PENDING_ATTACKS onto `target` and return the fighter-count
    prompt -- the shared tail of every cmd_attack form (and of a 'yes' in
    the guided cycle)."""
    p = ctx.player
    if p["fighters"] <= 0:
        return "You have no fighters to attack with."

    pending = {"stage": "fighters", "kind": target["kind"], "target_name": target["name"]}
    if target["kind"] == "station":
        pending["station_id"] = target["station_id"]
    elif target["kind"] == "parked":
        pending["ship_id"] = target["ship_id"]
    else:
        pending["target_id"] = target["id"]
    PENDING_ATTACKS[ctx.pubkey] = pending
    return (
        f"Attack {target['name']} with how many fighters? "
        f"You have {p['fighters']}. Reply with a number, 'all', or 'cancel'."
    )


def _station_target(station):
    """Build the attack-target dict for a station, shaped enough like a
    ship target that the fighter-commitment prompt and resolver can treat
    them the same (with is_station to branch on)."""
    return {
        "kind": "station",
        "is_station": True,
        "station_id": station["id"],
        "owner_id": station["owner_id"],
        "name": f"Space Station - {station['owner_name']}",
        "fighters": station["fighters"],
        "shields": station["shields"],
        "credits": station.get("credits", 0),
    }


async def cmd_attack_step(ctx, message):
    """
    Handle the reply to cmd_attack's "how many fighters?" prompt. A whole
    number commits that many fighters (1..however many are aboard); 'all'
    commits the lot; 'no'/'cancel' calls the attack off. Anything else
    re-prompts without firing. On a valid count the target's live ship row
    is re-fetched (it must still be in the sector) and the attack is
    resolved by _resolve_attack; PENDING_ATTACKS is cleared either way.
    """
    p = ctx.player
    pubkey = ctx.pubkey

    pending = PENDING_ATTACKS.get(pubkey)
    if not pending:
        PENDING_ATTACKS.pop(pubkey, None)
        return "No attack in progress."

    text = message.strip().lower()

    if pending.get("stage") == "choose":
        # Guided target cycle: 'yes' locks onto the current target and
        # falls through to the fighter-count prompt; 'no' moves on to the
        # next target (calling the whole thing off when the list runs
        # out); 'cancel' stops immediately.
        queue, idx = pending["queue"], pending["idx"]
        if text in ("cancel",):
            PENDING_ATTACKS.pop(pubkey, None)
            return f"Attack called off. You remain in Sec{p['sector_id']}."
        if text in ("n", "no"):
            idx += 1
            if idx >= len(queue):
                PENDING_ATTACKS.pop(pubkey, None)
                return "Attack called off -- no more targets here."
            pending["idx"] = idx
            return _attack_choose_prompt(queue, idx)
        if text in ("y", "yes"):
            return _aim_attack(ctx, queue[idx])
        return (
            f"Reply 'yes' to attack {queue[idx]['name']}, 'no' for the next "
            "target, or 'cancel'."
        )

    if text in ("n", "no", "cancel"):
        PENDING_ATTACKS.pop(pubkey, None)
        return f"Attack called off. You remain in Sec{p['sector_id']}."

    available = p["fighters"]
    if available <= 0:
        # Somehow out of fighters since the prompt -- nothing to commit.
        PENDING_ATTACKS.pop(pubkey, None)
        return "You have no fighters to attack with."

    if text == "all":
        engaged = available
    elif re.match(r"^\d+$", text):
        engaged = int(text)
    else:
        return (
            f"Commit how many fighters? Reply with a number from 1 to {available}, "
            "'all', or 'cancel'."
        )

    if engaged == 0:
        return "Commit how many? Enter a number from 1 up, or 'cancel'."
    if engaged > available:
        return f"You only have {available} fighters aboard. Pick up to that, or 'cancel'."

    # Re-fetch the target's current state -- it must still be in the sector.
    kind = pending.get("kind", "station" if pending.get("is_station") else "player")
    if kind == "station":
        station = get_station_in_sector(p["sector_id"])
        if station is None or station["id"] != pending["station_id"]:
            PENDING_ATTACKS.pop(pubkey, None)
            return f"{pending['target_name']} is no longer here. Attack called off."
        station = apply_station_upkeep(station["id"])
        target = _station_target(station)
    elif kind == "parked":
        parked_here = get_parked_ships_in_sector(p["sector_id"])
        ship = next((s for s in parked_here if s["id"] == pending["ship_id"]), None)
        if ship is None:
            PENDING_ATTACKS.pop(pubkey, None)
            return f"{pending['target_name']} is no longer here. Attack called off."
        target = _parked_target(ship)
    else:
        foes = get_ships_in_sector(p["sector_id"], p["id"])
        target = next((f for f in foes if f["id"] == pending["target_id"]), None)
        if target is None:
            PENDING_ATTACKS.pop(pubkey, None)
            return f"{pending['target_name']} is no longer in this sector. Attack called off."

    PENDING_ATTACKS.pop(pubkey, None)
    return _resolve_attack(ctx, target, engaged)


def _resolve_attack(ctx, target, engaged):
    """
    Resolve a committed attack of `engaged` of the attacker's fighters
    against `target`, returning the player-facing report. The fighters
    held back (everything not committed) are untouched -- only the engaged
    wing can be lost -- so the attacker ends with their reserve plus
    whatever engaged fighters survive (see resolve_attack for the math).

    On a kill, an ordinary ship's pilot ejects into an escape pod and
    drifts to an adjacent sector, losing their hull; finishing off a pilot
    who's ALREADY in a pod wipes them out -- credits reset and a fresh
    default ship next login. Either way the victim gets a notice when they
    sign in (record_attack_event). Where the pod drifts to is NOT revealed
    to the attacker -- they have to track the survivor down themselves.
    """
    p = ctx.player

    if target.get("is_station"):
        return _resolve_attack_on_station(ctx, target, engaged)
    if target.get("is_parked"):
        return _resolve_attack_on_parked(ctx, target, engaged)

    atk_after, df_after, ds_after, destroyed = resolve_attack(
        engaged, target["fighters"], target["shields"]
    )
    reserve = p["fighters"] - engaged
    fighters_after = reserve + atk_after  # untouched reserve + engaged survivors
    spent = engaged - atk_after
    set_ship_defenses(p["id"], p["shields"], fighters_after)  # keep shields, spend fighters

    if not destroyed:
        set_ship_defenses(target["id"], ds_after, df_after)
        record_attack_event(target["id"], p["name"], p["sector_id"], "attacked")
        return (
            f"You hit {target['name']} with {_plural(spent, 'fighter')}! "
            f"They're left with {df_after} fighters, {ds_after} shields. "
            f"You have {fighters_after} fighters."
        )

    if target["ship_type"] == ESCAPE_POD_SHIP:
        # Finishing off a pod: total wipe -- fresh default ship, credits
        # reset, back to the home sector.
        falcon = SHIP_CATALOG[DEFAULT_SHIP_TYPE]
        buy_ship(
            target["id"], DEFAULT_SHIP_TYPE,
            falcon["base_holds"], falcon["base_fighters"], falcon["base_shields"], falcon["base_mines"],
            credit_delta=POD_KILL_RESET_CREDITS - target["credits"],
        )
        move_player_to_sector(target["id"], HOME_SECTOR)
        record_attack_event(target["id"], p["name"], p["sector_id"], "pod_destroyed")
        record_kill(target["name"], p["name"], p["sector_id"], "pod")
        return (
            f"You blew apart {target['name']}'s escape pod! They lose everything and "
            f"restart with {POD_KILL_RESET_CREDITS}cr in a {DEFAULT_SHIP_TYPE} next login. "
            f"You have {fighters_after} fighters."
        )

    # Ordinary ship destroyed: eject into a pod, drift to an adjacent
    # sector, lose the hull (credits and cargo go with the ship). The
    # destination is computed but deliberately kept out of the reply --
    # the attacker isn't told where the pod went.
    pod = SHIP_CATALOG[ESCAPE_POD_SHIP]
    buy_ship(
        target["id"], ESCAPE_POD_SHIP,
        pod["base_holds"], pod["base_fighters"], pod["base_shields"], pod["base_mines"],
        credit_delta=0,
    )
    adjacent = get_adjacent_sectors(p["sector_id"])
    dest = random.choice(adjacent) if adjacent else p["sector_id"]
    move_player_to_sector(target["id"], dest)
    record_attack_event(target["id"], p["name"], p["sector_id"], "destroyed")
    record_kill(target["name"], p["name"], p["sector_id"], "ship")
    return (
        f"You destroyed {target['name']}'s {target['ship_type']}! They eject in an "
        f"Escape Pod and slip away (ship lost, credits intact). "
        f"You have {fighters_after} fighters."
    )


def _resolve_attack_on_station(ctx, target, engaged):
    """
    Resolve a player's committed attack against a space station. Same
    fighter-vs-fighter / fighter-vs-shield math as a ship, but on a kill
    the station is removed from the sector (its owner loses it) and the
    owner gets a sign-in notice -- a station isn't a ship/pod, so it does
    NOT go in the public kill log. Returns the attacker-facing report.
    """
    p = ctx.player

    atk_after, df_after, ds_after, destroyed = resolve_attack(
        engaged, target["fighters"], target["shields"]
    )
    fighters_after = (p["fighters"] - engaged) + atk_after
    spent = engaged - atk_after
    set_ship_defenses(p["id"], p["shields"], fighters_after)  # keep shields, spend fighters

    if destroyed:
        # Spares docked inside share the station's fate: delete_station
        # removes them with it. The owner gets one notice per lost hull on
        # top of the station notice; the attacker's report counts them.
        docked = get_ships_docked_at_station(target["station_id"])
        delete_station(target["station_id"])
        record_attack_event(target["owner_id"], p["name"], p["sector_id"], "station_destroyed")
        for _ship in docked:
            record_attack_event(target["owner_id"], p["name"], p["sector_id"], "unmanned_destroyed")
        docked_note = (
            f" {_plural(len(docked), 'docked ship')} went down with it."
            if docked else ""
        )
        treasury = target.get("credits", 0)
        treasury_note = (
            f" Its treasury of {treasury}cr is lost to the void."
            if treasury > 0 else ""
        )
        return (
            f"You destroyed {target['name']}! It's wreckage now.{docked_note}"
            f"{treasury_note} "
            f"You have {fighters_after} fighters."
        )

    set_station_defenses(target["station_id"], ds_after, df_after)
    return (
        f"You hit {target['name']} with {_plural(spent, 'fighter')}! "
        f"It's left with {df_after} fighters, {ds_after} shields. "
        f"You have {fighters_after} fighters."
    )


def _resolve_attack_on_parked(ctx, target, engaged):
    """
    Resolve a committed attack against an unmanned (parked or towed)
    ship. Same fighter-vs-fighter / fighter-vs-shield math -- the hull
    defends itself with whatever fighters and shields were left aboard
    when it was parked. Nobody's flying it, so a kill just removes the
    hull (no pod, no pilot relocation) and, like a station, it does NOT
    go in the public kill log; the owner gets a sign-in notice either
    way. Returns the attacker-facing report.
    """
    p = ctx.player

    atk_after, df_after, ds_after, destroyed = resolve_attack(
        engaged, target["fighters"], target["shields"]
    )
    fighters_after = (p["fighters"] - engaged) + atk_after
    spent = engaged - atk_after
    set_ship_defenses(p["id"], p["shields"], fighters_after)  # keep shields, spend fighters

    if destroyed:
        delete_ship(target["ship_id"])
        record_attack_event(target["owner_id"], p["name"], p["sector_id"], "unmanned_destroyed")
        return (
            f"You destroyed the unmanned {target['name']}! It's wreckage now. "
            f"You have {fighters_after} fighters."
        )

    set_parked_ship_defenses(target["ship_id"], ds_after, df_after)
    record_attack_event(target["owner_id"], p["name"], p["sector_id"], "unmanned_attacked")
    return (
        f"You hit the unmanned {target['name']} with {_plural(spent, 'fighter')}! "
        f"It's left with {df_after} fighters, {ds_after} shields. "
        f"You have {fighters_after} fighters."
    )


def format_attack_notices(events):
    """Turn queued attack_events (oldest first) into the sign-in briefing a
    victim sees -- one line each, phrased by outcome, tagged with when."""
    phrasing = {
        "attacked": "{who} attacked you in Sec{sec}",
        "destroyed": "{who} destroyed your ship in Sec{sec}; you ejected in a pod",
        "pod_destroyed": "{who} blew up your escape pod in Sec{sec}; you were reset",
        "station_destroyed": "{who} destroyed your space station in Sec{sec}",
        "unmanned_attacked": "{who} attacked your unmanned ship in Sec{sec}",
        "unmanned_destroyed": "{who} destroyed your unmanned ship in Sec{sec}",
    }
    lines = ["While you were away:"]
    for e in events:
        what = phrasing.get(e["outcome"], "{who} attacked you in Sec{sec}").format(
            who=e["attacker_name"], sec=e["sector_id"]
        )
        when = e["created_at"][:16].replace("T", " ")  # YYYY-MM-DD HH:MM, UTC
        lines.append(f"- {what} ({when} UTC).")
    return "\n".join(lines)


def _format_one_kill(k):
    """One public kill-log line: '<killer> destroyed/wiped <victim>'s
    ship/escape pod in SecN (time UTC)'. A None killer means mines."""
    when = k["created_at"][:16].replace("T", " ")  # YYYY-MM-DD HH:MM, UTC
    killer = k["killer_name"] or "Mines"
    if k["kind"] == "pod":
        return f"{killer} wiped {k['victim_name']}'s escape pod in Sec{k['sector_id']} ({when} UTC)"
    return f"{killer} destroyed {k['victim_name']}'s ship in Sec{k['sector_id']} ({when} UTC)"


def format_kill_log(kills):
    """Render the public kill log shown at sign-in: one line per kill,
    oldest first. Returns "" for an empty list (so no section is shown at
    all). If there are more than KILL_LOG_MAX_ENTRIES, only the most recent
    that many are listed, with a leading note counting the older ones."""
    if not kills:
        return ""
    omitted = max(0, len(kills) - KILL_LOG_MAX_ENTRIES)
    shown = kills[-KILL_LOG_MAX_ENTRIES:] if omitted else kills
    lines = ["Kills since you last played:"]
    if omitted:
        lines.append(f"(+{omitted} earlier not shown)")
    lines.extend("- " + _format_one_kill(k) for k in shown)
    return "\n".join(lines)


def enter_sector(ctx, sector_id, lead, rng=None):
    """
    Move the player into `sector_id` and resolve any mines waiting there,
    returning (message, destroyed).

    `lead` is the arrival verb shown before the sector number ("Moved
    to" / "Warped to" / "Arrived at"), so this one function backs every
    way a player can land somewhere.

    If the sector holds mines owned by anyone else, they all detonate at
    once (the player's own mines there, if any, don't). Damage cascades
    shields -> fighters -> hull. A survivor keeps flying with reduced
    defenses; a casualty flying an ordinary hull is ejected into an Escape
    Pod (cargo and current hull lost, credits kept) and drifts
    ESCAPE_POD_MIN_HOPS..MAX_HOPS away. A casualty who was ALREADY in an
    Escape Pod has nothing to eject into, so they're wiped back to a fresh
    default ship at the home Stardock with credits reset -- the same total
    loss as having their pod shot out from under them in combat. When
    `destroyed` is True the player has been relocated (drifted or reset)
    and any plotted route they were following should be dropped -- they're
    no longer where that route expected.

    The pod's own landing is deliberately NOT re-checked for mines: a
    wreck shouldn't chain-detonate its way across the map.
    """
    r = rng if rng is not None else random
    pubkey = ctx.pubkey

    # A towed hull is dragged along: it relocates with the player and the
    # move costs TOW_TURNS_PER_SECTOR turns instead of one (the caller has
    # already refused the move if that can't be afforded -- see
    # _tow_move_block). A dangling towing_ship_id (ship destroyed or sold
    # out from under the tow) is quietly dropped.
    towed = None
    towing_id = ctx.player.get("towing_ship_id")
    if towing_id:
        towed = get_ship(towing_id)
        if towed is None:
            set_towing(ctx.player["id"], None)

    move_player_to_sector(ctx.player["id"], sector_id)
    if towed is not None:
        spend_turns(ctx.player["id"], TOW_TURNS_PER_SECTOR)
        move_ship_to_sector(towed["id"], sector_id)
        tow_note = f" Towing {ship_label(towed)}: -{TOW_TURNS_PER_SECTOR} turns."
    else:
        spend_turn(ctx.player["id"])  # each sector-to-sector move costs a turn
        tow_note = ""
    # If the player is destroyed below, the tow line goes with the hull
    # (every destruction path swaps the hull via buy_ship, which clears
    # towing_ship_id) and the towed ship is left adrift right here.
    tow_lost_note = (
        f"\nYour tow line snaps -- {ship_label(towed)} is left adrift in Sec{sector_id}."
        if towed is not None else ""
    )
    p = get_player_with_ship(pubkey)  # fresh defenses to test the hit against

    hostile = get_hostile_mine_total(sector_id, p["id"])
    if hostile <= 0:
        arrival_line = f"{lead} Sec{sector_id}.{tow_note}"
    else:
        # The mines go off and are spent, kill or not.
        clear_hostile_mines(sector_id, p["id"])
        total_damage = roll_mine_damage(hostile, r)
        shields_after, fighters_after, shields_lost, fighters_lost, destroyed = apply_mine_damage(
            p["shields"], p["fighters"], total_damage
        )

        if destroyed:
            # Destroyed by mines. What happens next depends on what blew up:
            #   * A pilot ALREADY in an Escape Pod has nothing to eject into,
            #     so they're wiped back to a fresh default ship at the home
            #     Stardock with credits reset (like a pod-kill).
            #   * An ordinary hull ejects its pilot into a pod that drifts
            #     ESCAPE_POD_MIN..MAX hops away.
            # Either way `destroyed` is True, so a plotted route is dropped.
            if p["ship_type"] == ESCAPE_POD_SHIP:
                falcon = SHIP_CATALOG[DEFAULT_SHIP_TYPE]
                buy_ship(
                    p["id"], DEFAULT_SHIP_TYPE,
                    falcon["base_holds"], falcon["base_fighters"], falcon["base_shields"], falcon["base_mines"],
                    credit_delta=POD_KILL_RESET_CREDITS - p["credits"],
                )
                move_player_to_sector(p["id"], HOME_SECTOR)
                record_kill(p["name"], None, sector_id, "pod")  # None killer = mines
                message = (
                    f"{_plural(hostile, 'mine')} detonate for {total_damage} damage -- your "
                    f"Escape Pod is GONE! You lose everything and restart with "
                    f"{POD_KILL_RESET_CREDITS}cr in a {DEFAULT_SHIP_TYPE} at the Stardock."
                    f"{tow_lost_note}\n"
                    f"{build_sector_info(HOME_SECTOR, p['id'])}"
                )
                return message, True

            graph = get_all_warps()
            escape_sector = choose_escape_sector(graph, sector_id, r)
            pod = SHIP_CATALOG[ESCAPE_POD_SHIP]
            buy_ship(
                p["id"], ESCAPE_POD_SHIP,
                pod["base_holds"], pod["base_fighters"], pod["base_shields"], pod["base_mines"],
                credit_delta=0,
            )
            if escape_sector is not None:
                move_player_to_sector(p["id"], escape_sector)
            landed = escape_sector if escape_sector is not None else sector_id
            record_kill(p["name"], None, sector_id, "ship")  # None killer = mines
            message = (
                f"{_plural(hostile, 'mine')} detonate for {total_damage} damage -- your "
                f"{p['ship_type']} is DESTROYED! You eject in an Escape Pod and drift to "
                f"Sec{landed} (cargo lost, credits intact)."
                f"{tow_lost_note}\n{build_sector_info(landed, p['id'])}"
            )
            return message, True

        # Survived the mines -- write back the damage and carry on.
        set_ship_defenses(p["id"], shields_after, fighters_after)
        p = get_player_with_ship(pubkey)
        arrival_line = (
            f"{lead} Sec{sector_id} -- {_plural(hostile, 'mine')} detonate for "
            f"{total_damage} damage! Lost {shields_lost} shields, {fighters_lost} fighters; "
            f"now {shields_after} shields, {fighters_after} fighters.{tow_note}"
        )

    # An offensive station here opens fire on a non-owner who just arrived
    # (whether or not there were mines) -- possibly damaging or destroying
    # them. If it destroys them, that result is returned directly.
    station_line, destroyed_result = _station_offensive_on_entry(ctx, sector_id)
    if destroyed_result is not None:
        msg, was_destroyed = destroyed_result
        return msg + tow_lost_note, was_destroyed
    p = get_player_with_ship(pubkey)
    return f"{arrival_line}{station_line}\n{build_sector_info(sector_id, p['id'])}", False


def _eject_player(p, from_sector, killer_name):
    """
    Mechanics of `p` losing their ship to `killer_name` (a display-name
    string, or None for mines) in `from_sector`, returning a short
    player-facing consequence line (the caller supplies the cause). A pod
    pilot is wiped back to a fresh default ship at the home Stardock; an
    ordinary hull ejects into a pod that drifts to an adjacent sector. The
    public kill is recorded.
    """
    if p["ship_type"] == ESCAPE_POD_SHIP:
        falcon = SHIP_CATALOG[DEFAULT_SHIP_TYPE]
        buy_ship(
            p["id"], DEFAULT_SHIP_TYPE,
            falcon["base_holds"], falcon["base_fighters"], falcon["base_shields"], falcon["base_mines"],
            credit_delta=POD_KILL_RESET_CREDITS - p["credits"],
        )
        move_player_to_sector(p["id"], HOME_SECTOR)
        record_kill(p["name"], killer_name, from_sector, "pod")
        return (
            f"Your escape pod is GONE -- you're reset to a {DEFAULT_SHIP_TYPE} at the "
            f"Stardock with {POD_KILL_RESET_CREDITS}cr."
        )
    pod = SHIP_CATALOG[ESCAPE_POD_SHIP]
    buy_ship(
        p["id"], ESCAPE_POD_SHIP,
        pod["base_holds"], pod["base_fighters"], pod["base_shields"], pod["base_mines"],
        credit_delta=0,
    )
    adjacent = get_adjacent_sectors(from_sector)
    dest = random.choice(adjacent) if adjacent else from_sector
    move_player_to_sector(p["id"], dest)
    record_kill(p["name"], killer_name, from_sector, "ship")
    return (
        f"Your {p['ship_type']} is destroyed -- you eject in an Escape Pod and slip away "
        "(credits intact)."
    )


def _station_offensive_on_entry(ctx, sector_id):
    """
    If an offensive station owned by someone else sits in `sector_id`, it
    fires on the arriving player with engage_pct% of its fighters (the same
    fighter-vs-fighter / fighter-vs-shield math players use). The station
    is brought up to date first (apply_station_upkeep). Returns
    (station_line, destroyed_result): station_line is text to append to the
    arrival message ("" if nothing happened); destroyed_result is None
    unless the player was destroyed, in which case it's the (message, True)
    tuple enter_sector should return directly.
    """
    p = get_player_with_ship(ctx.pubkey)
    station = get_station_in_sector(sector_id)
    if station is None:
        return "", None
    station = apply_station_upkeep(station["id"])
    if station is None:
        return "", None
    if (station["owner_id"] == p["id"]
            or station["posture"] != "offensive"
            or station["fighters"] <= 0):
        return "", None

    engaged = engaged_fighters(station["fighters"], station["engage_pct"])
    if engaged <= 0:
        return "", None

    atk_after, df_after, ds_after, destroyed = resolve_attack(
        engaged, p["fighters"], p["shields"]
    )
    # The station keeps its uncommitted reserve plus the engaged survivors.
    set_station_defenses(
        station["id"], station["shields"], (station["fighters"] - engaged) + atk_after
    )
    owner = station["owner_name"]

    if destroyed:
        consequence = _eject_player(p, sector_id, f"Space Station - {owner}")
        return "", (
            f"Space Station - {owner} opens fire as you arrive! {consequence}", True
        )

    set_ship_defenses(p["id"], ds_after, df_after)  # write back the player's losses
    return (
        f"\nSpace Station - {owner} opens fire! You're left with "
        f"{df_after} fighters, {ds_after} shields."
    ), None


@command("tow", description="tow one of your parked ships (5 turns/sector): 'tow' or 'tow #<id>'")
async def cmd_tow(ctx, args):
    """
    Attach a tow line to one of YOUR OWN unmanned ships in this sector --
    or, if a tow is already engaged, release it (the hull stays put
    wherever you are). While towing, every sector move drags the hull
    along and burns TOW_TURNS_PER_SECTOR turns instead of one; a move is
    refused when fewer remain. Forms:

      tow          -- guided: cycles yes/no through your ships parked here
                      (see cmd_tow_step); releases if already towing
      tow #<id>    -- tow that ship directly (must be yours, must be here)

    Ships belonging to other players can never be towed. An Escape Pod
    has no tow rig. Engaging is free; the cost lands on each move.
    """
    p = ctx.player

    if p.get("towing_ship_id"):
        towed = get_ship(p["towing_ship_id"])
        set_towing(p["id"], None)
        name = ship_label(towed) if towed else "the hull"
        return f"Tow released -- {name} stays parked in Sec{p['sector_id']}."

    if p["ship_type"] == ESCAPE_POD_SHIP:
        return "An escape pod has no tow rig."

    parked_here = get_parked_ships_in_sector(p["sector_id"])
    own = [s for s in parked_here if s["owner_id"] == p["id"]]

    arg = args.strip()
    id_match = re.match(r"^#?(\d+)$", arg)
    if id_match:
        ship_id = int(id_match.group(1))
        ship = next((s for s in parked_here if s["id"] == ship_id), None)
        if ship is None:
            return f"No unmanned ship #{ship_id} here."
        if ship["owner_id"] != p["id"]:
            return f"{ship_label(ship)} isn't yours -- you can only tow your own ships."
        return _engage_tow(ctx, ship)
    if arg:
        return "Try 'tow' to pick from your ships here, or 'tow #<ship id>'."

    if not own:
        if parked_here:
            return "None of the unmanned ships here are yours -- you can only tow your own."
        return "No unmanned ships here to tow."

    PENDING_TOWS[ctx.pubkey] = {"queue": own, "idx": 0}
    return _tow_choose_prompt(own, 0)


def _tow_choose_prompt(queue, idx):
    more = " (no = next ship)" if idx + 1 < len(queue) else ""
    return f"Tow your {queue[idx]['ship_type']} #{queue[idx]['id']}? yes/no{more}"


def _engage_tow(ctx, ship):
    p = ctx.player
    set_towing(p["id"], ship["id"])
    return (
        f"Tow line attached to {ship_label(ship)}. WARNING: towing burns "
        f"{TOW_TURNS_PER_SECTOR} turns per sector moved (you have "
        f"{p['turns_remaining']}). 'tow' again to release."
    )


async def cmd_tow_step(ctx, message):
    """
    Advance a guided tow pick (PENDING_TOWS): 'yes' attaches the line to
    the ship on offer, 'no' moves to the next of the player's own ships
    here, 'cancel' (or running out of ships) calls it off. The ship is
    re-fetched on 'yes' -- it must still exist, still be theirs, and
    still be in this sector.
    """
    pubkey = ctx.pubkey
    p = ctx.player
    text = message.strip().lower()

    state = PENDING_TOWS.get(pubkey)
    if not state:
        PENDING_TOWS.pop(pubkey, None)
        return "No tow in progress."

    queue, idx = state["queue"], state["idx"]
    if text in ("cancel",):
        PENDING_TOWS.pop(pubkey, None)
        return "Tow called off."
    if text in ("n", "no"):
        idx += 1
        if idx >= len(queue):
            PENDING_TOWS.pop(pubkey, None)
            return "Tow called off -- no more of your ships here."
        state["idx"] = idx
        return _tow_choose_prompt(queue, idx)
    if text in ("y", "yes"):
        PENDING_TOWS.pop(pubkey, None)
        ship = get_ship(queue[idx]["id"])
        if (ship is None or ship["owner_id"] != p["id"]
                or ship["sector_id"] != p["sector_id"]):
            return "That ship is no longer here. Tow called off."
        return _engage_tow(ctx, ship)
    return (
        f"Reply 'yes' to tow your {queue[idx]['ship_type']} #{queue[idx]['id']}, "
        "'no' for the next, or 'cancel'."
    )


@command("board", "swap", description="board one of your parked ships here: 'board' or 'board #<id>'")
async def cmd_board(ctx, args):
    """
    Swap into one of YOUR OWN unmanned ships in this sector: your current
    hull is parked here in its place (with everything aboard it), and you
    take the helm of the other -- exactly as it was left. A pilot in an
    Escape Pod can board a ship they own, scuttling the pod (that's the
    cheap way back from a wreck if you kept a spare). A free action, like
    docking; any tow in progress is released. Forms:

      board        -- guided: cycles yes/no through your ships parked here
      board #<id>  -- board that ship directly

    Other players' ships can't be boarded.
    """
    p = ctx.player

    parked_here = get_parked_ships_in_sector(p["sector_id"])
    own = [s for s in parked_here if s["owner_id"] == p["id"]]

    arg = args.strip()
    id_match = re.match(r"^#?(\d+)$", arg)
    if id_match:
        ship_id = int(id_match.group(1))
        ship = next((s for s in parked_here if s["id"] == ship_id), None)
        if ship is None:
            return f"No unmanned ship #{ship_id} here."
        if ship["owner_id"] != p["id"]:
            return f"{ship_label(ship)} isn't yours -- you can't board it."
        return _do_board(ctx, ship)
    if arg:
        return "Try 'board' to pick from your ships here, or 'board #<ship id>'."

    if not own:
        if parked_here:
            return "None of the unmanned ships here are yours -- you can only board your own."
        return "No unmanned ships here to board."

    PENDING_BOARDS[ctx.pubkey] = {"queue": own, "idx": 0}
    return _board_choose_prompt(own, 0)


def _board_choose_prompt(queue, idx):
    more = " (no = next ship)" if idx + 1 < len(queue) else ""
    return f"Board your {queue[idx]['ship_type']} #{queue[idx]['id']}? yes/no{more}"


def _do_board(ctx, ship):
    p = ctx.player
    was_pod = p["ship_type"] == ESCAPE_POD_SHIP
    old_ship_id = p.get("ship_id")
    swap_active_ship(p["id"], ship["id"], p["sector_id"])
    np = get_player_with_ship(ctx.pubkey)
    if was_pod:
        left_behind = "your escape pod is scuttled"
    else:
        left_behind = f"your {p['ship_type']} #{old_ship_id} is parked here"
    return (
        f"You board your {ship['ship_type']} #{ship['id']} -- {left_behind}. "
        f"Aboard: {np['fighters']} ftr, {np['shields']} shd, cargo "
        f"f{np['fuel_ore']}/o{np['organics']}/e{np['equipment']}."
    )


async def cmd_board_step(ctx, message):
    """
    Advance a guided board pick (PENDING_BOARDS): same yes/no/cancel
    cycle as cmd_tow_step, ending in _do_board on a 'yes' (with the ship
    re-fetched and re-validated first).
    """
    pubkey = ctx.pubkey
    p = ctx.player
    text = message.strip().lower()

    state = PENDING_BOARDS.get(pubkey)
    if not state:
        PENDING_BOARDS.pop(pubkey, None)
        return "No boarding in progress."

    queue, idx = state["queue"], state["idx"]
    if text in ("cancel",):
        PENDING_BOARDS.pop(pubkey, None)
        return "Boarding called off."
    if text in ("n", "no"):
        idx += 1
        if idx >= len(queue):
            PENDING_BOARDS.pop(pubkey, None)
            return "Boarding called off -- no more of your ships here."
        state["idx"] = idx
        return _board_choose_prompt(queue, idx)
    if text in ("y", "yes"):
        PENDING_BOARDS.pop(pubkey, None)
        ship = get_ship(queue[idx]["id"])
        if (ship is None or ship["owner_id"] != p["id"]
                or ship["sector_id"] != p["sector_id"]):
            return "That ship is no longer here. Boarding called off."
        return _do_board(ctx, ship)
    return (
        f"Reply 'yes' to board your {queue[idx]['ship_type']} #{queue[idx]['id']}, "
        "'no' for the next, or 'cancel'."
    )


async def cmd_move(ctx, args):
    """
    Handle a number-like message as a move request.
      - Non-integers (e.g. "4.5") are rejected.
      - Sectors outside [MIN_SECTOR_ID, MAX_SECTOR_ID] are rejected.
      - Adjacent sectors are moved to directly (single warp, no confirmation
        needed).
      - Non-adjacent (but valid) sectors are routed via the shortest path
        through the warp network (BFS). The player is NOT moved yet --
        instead the route is plotted and the player is asked to confirm
        the first hop. See cmd_confirm_warp for the rest of the flow.
    """
    p = ctx.player

    try:
        target = int(args)
    except ValueError:
        return f"'{args}' isn't a whole number. Enter a sector number, e.g. 42."

    if target < MIN_SECTOR_ID or target > MAX_SECTOR_ID:
        return f"Sec{target} is out of range. Sectors range from {MIN_SECTOR_ID} to {MAX_SECTOR_ID}."

    if target == p["sector_id"]:
        return f"You're already in Sec{target}."

    adjacent = get_adjacent_sectors(p["sector_id"])
    if target in adjacent:
        block = _tow_move_block(p)
        if block:
            return block
        message, _destroyed = enter_sector(ctx, target, "Moved to")
        return message

    graph = get_all_warps()
    path = find_shortest_path(graph, p["sector_id"], target)
    if path is None:
        return f"No route found to Sec{target}."

    remaining = path[1:]  # hops after the player's current sector
    PENDING_WARPS[ctx.pubkey] = remaining
    hops = len(remaining)
    route = " -> ".join(str(s) for s in path)
    return f"Plotted a {hops}-warp course to Sec{target}.\nWarp to: {route}? {_warp_confirm_options(p['sector_id'])}"


async def cmd_confirm_warp(ctx, message):
    """
    Handle a reply while a multi-hop warp is awaiting confirmation.
    "yes" advances one hop and asks again if more remain, or reports
    arrival if that was the last one. "no"/"cancel" cancels the rest of
    the plotted course and leaves the player where they are.

    The port command ('p'/'port') is also accepted here: it lets the
    player dock at the sector they've just warped into -- regular
    trading, or a Stardock refit if it's Sec1 -- without losing the
    rest of the route. PENDING_WARPS is left untouched while that visit
    runs (cmd_trade starts its own PENDING_TRADES/PENDING_UPGRADES,
    which on_message checks ahead of PENDING_WARPS, so follow-up
    messages go to the visit, not back here). Once the visit ends,
    cmd_trade_step/cmd_stardock_step append this same yes/no prompt via
    _resume_navigation_suffix so the player is dropped straight back
    into the route. If docking didn't actually start a visit (no port,
    or nothing to trade), the prompt is re-shown immediately instead.
    """
    p = ctx.player
    pubkey = ctx.pubkey
    text = message.strip().lower()

    remaining = PENDING_WARPS.get(pubkey)
    if not remaining:
        PENDING_WARPS.pop(pubkey, None)
        return "No warp in progress."

    verb, args = parse(text)
    if verb in COMMANDS and COMMANDS[verb][1] is cmd_trade:
        response = await cmd_trade(ctx, args)
        if pubkey not in PENDING_TRADES and pubkey not in PENDING_UPGRADES:
            # Nothing to dock for -- no port here, or nothing tradeable
            # -- so no visit actually started to resume the prompt
            # later. Re-show it now instead of leaving the player stuck.
            response += _resume_navigation_suffix(pubkey, p["sector_id"])
        return response

    if text in ("y", "yes"):
        block = _tow_move_block(p)
        if block:
            # The hop is NOT consumed -- the route stays plotted so the
            # player can cancel it ('no'), free the tow, and resume.
            return block + " (Reply 'no' to cancel this route first.)"
        next_sector = remaining.pop(0)
        last_hop = not remaining
        message, destroyed = enter_sector(
            ctx, next_sector, "Arrived at" if last_hop else "Warped to"
        )
        if destroyed:
            # Blown out of the plotted course into a pod somewhere else --
            # the rest of the route no longer connects to where we are.
            PENDING_WARPS.pop(pubkey, None)
            return message
        if remaining:
            route = " -> ".join(str(s) for s in [next_sector] + remaining)
            return f"{message}\nWarp to: {route}? {_warp_confirm_options(next_sector)}"
        PENDING_WARPS.pop(pubkey, None)
        return message

    if text in ("n", "no", "cancel"):
        PENDING_WARPS.pop(pubkey, None)
        return f"Navigation cancelled. You remain in Sec{p['sector_id']}."

    if get_port(p["sector_id"]) is not None:
        return "Reply 'yes' to continue warping, 'no' to cancel, or 'p' to dock here."
    return "Reply 'yes' to continue warping or 'no' to cancel."


def _p2p_classify(home_port, adj_port):
    """
    Classify a shuttle between two adjacent commodity ports into the
    commodities that can flow each way. A commodity is only shuttleable
    when one port sells it and the other buys it; commodities pointing the
    same way at both ports (or absent) simply can't be traded around and
    are ignored -- so this works for any pair that shares at least one
    tradeable commodity, not just exact complements.

      forward  = home SELLS / adj BUYS  -> bought at home, sold at adj
                 (carried home->adj)
      backward = home BUYS  / adj SELLS -> bought at adj, sold at home
                 (carried adj->home)

    Returns (forward_keys, backward_keys); either may be empty. An exact
    complement splits 2/1, 1/2, 3/0, or 0/3; a partial overlap (e.g. a
    home port beside a BBB that buys everything) yields a smaller set with
    the non-matching commodities dropped. When both lists come back empty
    there's nothing to shuttle and cmd_p2p declines.
    """
    forward, backward = [], []
    for _, key, _ in _JETTISON_COMMODITIES:
        h = home_port[f"{key}_dir"]
        a = adj_port[f"{key}_dir"]
        if h == "S" and a == "B":
            forward.append(key)
        elif h == "B" and a == "S":
            backward.append(key)
        # Same direction at both ports (or a Stardock's None): not
        # shuttleable between this pair -- skip it.
    return forward, backward


def _commodity_label(key):
    for label, k, _ in _JETTISON_COMMODITIES:
        if k == key:
            return label
    return key


def _commodity_token(key):
    """The short reply token for a commodity ('fuel'/'organics'/
    'equipment') -- its first jettison alias, which _resolve_commodity
    also accepts."""
    for _, k, aliases in _JETTISON_COMMODITIES:
        if k == key:
            return aliases[0]
    return key


def _signed_cr(amount):
    """Format a credit amount with an explicit sign ('+8250cr' / '-10400cr'
    / '+0cr') -- used for both the per-leg and cumulative P2P figures."""
    return f"{'+' if amount >= 0 else ''}{amount}cr"


def _p2p_net(state):
    return _signed_cr(state["profit"])



def _p2p_choice_prompt(state):
    """
    The opening prompt. When one side offers more than one commodity it's a
    pick-and-confirm ("Reply organics/equipment"); when only a single
    shuttle is possible it's a plain confirm ("Confirm? y/n"). Either way it
    states the starting-holds requirement (full of the carried-out
    commodity, or empty for a buy-first one-way run).
    """
    home, adj = state["home"], state["adj"]
    head = f"P2P Sec{home}<->Sec{adj}."
    side = state["choice_side"]
    ff, fb = state["fixed_forward"], state["fixed_backward"]

    if side is not None:
        opts = "/".join(_commodity_token(k) for k in state["choices"])
        if side == "forward":
            if fb is not None:
                body = (
                    f"Carry one commodity to Sec{adj}, swap it for {_commodity_label(fb)}, "
                    f"sell that back here. Carry out which? (holds must be FULL of it)"
                )
            else:
                body = (
                    f"One-way: sell a commodity at Sec{adj}, rebuy it here, repeat. "
                    "Which? (holds must be FULL of it)"
                )
        else:
            if ff is not None:
                body = (
                    f"Shuttle {_commodity_label(ff)} out (holds must be FULL of "
                    f"{_commodity_label(ff)}); buy which back at Sec{adj}?"
                )
            else:
                body = (
                    f"One-way: buy a commodity at Sec{adj}, sell it here, repeat. "
                    "Which? (start with EMPTY holds)"
                )
        return f"{head}\n{body}\nReply {opts}, or 'cancel'."

    # No choice -- exactly one shuttle is possible, so just confirm it.
    if ff is not None and fb is not None:
        body = (
            f"Shuttle {_commodity_label(ff)} to Sec{adj} and {_commodity_label(fb)} back "
            f"(holds must be FULL of {_commodity_label(ff)})."
        )
    elif ff is not None:
        body = (
            f"One-way: sell {_commodity_label(ff)} at Sec{adj}, rebuy it here "
            f"(holds must be FULL of {_commodity_label(ff)})."
        )
    else:
        body = (
            f"One-way: buy {_commodity_label(fb)} at Sec{adj}, sell it here "
            "(start with EMPTY holds)."
        )
    return f"{head}\n{body}\nConfirm? y/n"


@command("p2p", description="auto-shuttle trade with an adjacent port that shares a commodity: 'p2p <sector>'")
async def cmd_p2p(ctx, args):
    """
    Set up a port-to-port shuttle between the player's current commodity
    port and an adjacent one that shares a tradeable commodity, then prompt
    to pick a commodity (when there's a choice) or just confirm (when only
    one shuttle is possible). No movement happens here -- cmd_p2p_step
    validates the player's holds on their reply and runs the first leg. See
    _p2p_classify for the forward/backward model and _p2p_run_leg for a
    leg's move-and-trade mechanics.
    """
    p = ctx.player

    if p.get("towing_ship_id"):
        return "Can't run a P2P shuttle while towing. Release the tow first ('tow')."
    home_sector = p["sector_id"]
    home_port = get_port(home_sector)
    if home_port is None or home_port["port_class"] == "STARDOCK":
        return "P2P needs a commodity port in your current sector."

    arg = args.strip()
    if not re.match(r"^\d+$", arg):
        return "Usage: 'p2p <sector>' -- the adjacent port to shuttle with."
    adj_sector = int(arg)
    if adj_sector == home_sector:
        return "Pick the adjacent sector to shuttle with, not your own."
    if adj_sector not in get_adjacent_sectors(home_sector):
        return f"Sec{adj_sector} isn't adjacent. P2P only runs between two warp-adjacent ports."
    adj_port = get_port(adj_sector)
    if adj_port is None or adj_port["port_class"] == "STARDOCK":
        return f"Sec{adj_sector} has no commodity port to shuttle with."

    forward, backward = _p2p_classify(home_port, adj_port)
    if not forward and not backward:
        return (
            f"Sec{home_sector} ({home_port['port_class']}) and Sec{adj_sector} "
            f"({adj_port['port_class']}) have no commodity to shuttle -- P2P needs one "
            "port to sell what the other buys."
        )

    # At most one side can hold >=2 commodities (there are only three), so
    # there's never more than one choice to make. When a side has >=2 the
    # player picks from it and the other side (if any) is fixed; otherwise
    # exactly one shuttle is possible and the prompt is a plain confirm.
    if len(forward) >= 2:
        choice_side, choices = "forward", forward
        fixed_forward, fixed_backward = None, (backward[0] if backward else None)
    elif len(backward) >= 2:
        choice_side, choices = "backward", backward
        fixed_forward, fixed_backward = (forward[0] if forward else None), None
    else:
        choice_side, choices = None, []
        fixed_forward = forward[0] if forward else None
        fixed_backward = backward[0] if backward else None

    PENDING_P2P[ctx.pubkey] = {
        "stage": "choose",
        "home": home_sector,
        "adj": adj_sector,
        "choice_side": choice_side,
        "choices": choices,
        "fixed_forward": fixed_forward,
        "fixed_backward": fixed_backward,
    }
    return _p2p_choice_prompt(PENDING_P2P[ctx.pubkey])


def _p2p_can_sustain(p, dest_port, sell_key, buy_key):
    """Whether dest_port can take the player's whole held load AND refill
    the holds completely -- the full-load-both-ways requirement. Returns
    (ok, reason)."""
    held = p[sell_key] if sell_key else 0
    cargo_after_sell = (p["fuel_ore"] + p["organics"] + p["equipment"]) - held
    if sell_key:
        room = dest_port[f"{sell_key}_max"] - dest_port[f"{sell_key}_qty"]
        if room < held:
            return False, f"no room for your {_commodity_label(sell_key)}"
    if buy_key:
        buy_target = p["holds_total"] - cargo_after_sell
        price = dest_port[f"{buy_key}_price"]
        proceeds = held * dest_port[f"{sell_key}_price"] if sell_key else 0
        if dest_port[f"{buy_key}_qty"] < buy_target:
            return False, f"low on {_commodity_label(buy_key)}"
        if price > 0 and (p["credits"] + proceeds) < buy_target * price:
            return False, f"can't afford a full hold of {_commodity_label(buy_key)}"
    return True, ""


def _p2p_run_leg(ctx, state):
    """
    Run one shuttle leg, returning (message, ended). When `ended` is True
    the caller clears PENDING_P2P and returns `message` as-is; otherwise it
    appends the "Continue?" prompt and keeps the shuttle going.

    A leg: spend a turn moving into the destination (mines and offensive
    stations resolve exactly as in normal transit), then -- only on a clean
    arrival -- auto-dock and auto-trade at listed prices, max quantity. The
    leg ends the shuttle if the player is out of turns, the destination
    port can't sustain a full load (checked before moving, so no turn is
    wasted), the move destroys the ship, or the destination had hostile
    mines or a non-owner station (in which case normal transit already
    applied and the shuttle is abandoned).
    """
    pubkey = ctx.pubkey
    dest = state["next_dest"]
    home, adj = state["home"], state["adj"]
    origin = adj if dest == home else home

    # Which commodity is sold (the full held load) and bought at `dest`.
    if dest == adj:
        sell_key, buy_key = state["forward"], state["backward"]
    else:
        sell_key, buy_key = state["backward"], state["forward"]

    p = get_player_with_ship(pubkey)
    if p["turns_remaining"] <= 0:
        return f"Out of turns -- P2P ended in Sec{p['sector_id']}. Net {_p2p_net(state)}.", True

    dest_port = get_port(dest)
    if dest_port is None:
        return f"Sec{dest} has no port anymore -- P2P ended. Net {_p2p_net(state)}.", True
    # Bring the destination's stock up to date before checking whether it
    # can sustain a full load, so drift since the last visit counts.
    dest_port = apply_port_restock(dest_port["id"])
    ok, reason = _p2p_can_sustain(p, dest_port, sell_key, buy_key)
    if not ok:
        return (
            f"Sec{dest} can't sustain a full load ({reason}) -- P2P ended. "
            f"You're still in Sec{origin}. Net {_p2p_net(state)}."
        ), True

    # Peek for hostile presence so a clean leg can be told apart from one
    # where normal transit combat fired.
    pre_mines = get_hostile_mine_total(dest, p["id"]) > 0
    station = get_station_in_sector(dest)
    hostile_station = station is not None and station["owner_id"] != p["id"]

    msg, destroyed = enter_sector(ctx, dest, "Entered")
    if destroyed:
        return msg, True
    if pre_mines or hostile_station:
        return msg + f"\n\nP2P shuttle abandoned -- attacked entering Sec{dest}.", True

    # Clean arrival: dock and auto-trade. `leg_net` is this leg's profit on
    # its own (sale proceeds minus purchase cost); state["profit"] is the
    # running cumulative across the whole shuttle.
    report = [f"Entered Sec{dest}, docked."]
    leg_net = 0
    p = get_player_with_ship(pubkey)
    dest_port = get_port(dest)

    if sell_key:
        held = p[sell_key]
        proceeds = held * dest_port[f"{sell_key}_price"]
        execute_trade(p["id"], dest_port["id"], sell_key, held, proceeds, False)
        leg_net += proceeds
        report.append(f"Sold {held} {_commodity_label(sell_key)} for +{proceeds}cr.")
        p = get_player_with_ship(pubkey)
        dest_port = get_port(dest)

    if buy_key:
        price = dest_port[f"{buy_key}_price"]
        free_holds = p["holds_total"] - (p["fuel_ore"] + p["organics"] + p["equipment"])
        affordable = p["credits"] // price if price > 0 else free_holds
        qty = max(0, min(free_holds, dest_port[f"{buy_key}_qty"], affordable))
        cost = qty * price
        execute_trade(p["id"], dest_port["id"], buy_key, qty, cost, True)
        leg_net -= cost
        report.append(f"Bought {qty} {_commodity_label(buy_key)} for -{cost}cr.")
        p = get_player_with_ship(pubkey)

    state["profit"] += leg_net
    state["next_dest"] = home if dest == adj else adj
    report.append(f"Leg {_signed_cr(leg_net)}, total {_signed_cr(state['profit'])}.")

    if p["turns_remaining"] <= 0:
        return "\n".join(report) + "\n\nThat used your last turn -- P2P ended.", True
    return "\n".join(report), False


async def cmd_p2p_step(ctx, message):
    """
    Advance an in-progress p2p shuttle (PENDING_P2P). Two stages:

      "choose"   -- the player either picks a commodity (when a side offers
                    more than one) or confirms with 'y' (when only one
                    shuttle is possible); 'cancel'/'no'/'stop' backs out.
                    The pick is validated against the starting-holds rule
                    (full of the carried-out commodity, or empty when the
                    shuttle buys first), then the first leg runs.
      "continue" -- after each leg, 'y'/'yes' runs the next leg and
                    'n'/'no'/'s'/'stop'/'cancel' ends the shuttle. The
                    prompt advertises just 'y/n'; an unrecognized reply
                    re-asks rather than silently ending the run.
    """
    pubkey = ctx.pubkey
    lower = message.strip().lower()

    state = PENDING_P2P.get(pubkey)
    if not state:
        PENDING_P2P.pop(pubkey, None)
        return "No P2P shuttle in progress."

    if state["stage"] == "choose":
        if lower in ("cancel", "no", "n", "stop", "s"):
            PENDING_P2P.pop(pubkey, None)
            return "P2P cancelled."

        if state["choice_side"] is None:
            # Only one shuttle is possible -> the reply is a plain confirm.
            if lower not in ("y", "yes", "c", "continue"):
                return f"Start the shuttle? Reply y to begin or n to cancel.\n\n{_p2p_choice_prompt(state)}"
            forward, backward = state["fixed_forward"], state["fixed_backward"]
        else:
            resolved = _resolve_commodity(lower)
            if resolved is None or resolved[1] not in state["choices"]:
                opts = ", ".join(_commodity_token(k) for k in state["choices"])
                return f"Pick one of: {opts}, or 'cancel'.\n\n{_p2p_choice_prompt(state)}"
            chosen_key = resolved[1]
            if state["choice_side"] == "forward":
                forward, backward = chosen_key, state["fixed_backward"]
            else:
                forward, backward = state["fixed_forward"], chosen_key

        p = get_player_with_ship(pubkey)
        if forward is not None:
            if p.get("station_core"):
                PENDING_P2P.pop(pubkey, None)
                return "Deploy or sell your Station Core kit first -- it fills your holds."
            if p[forward] != p["holds_total"]:
                PENDING_P2P.pop(pubkey, None)
                lbl = _commodity_label(forward)
                return (
                    f"Holds must be FULL of {lbl} to start ({p[forward]}/{p['holds_total']}). "
                    f"Jettison other cargo and dock (p) to buy {lbl}, then run p2p again."
                )
        else:
            total_cargo = p["fuel_ore"] + p["organics"] + p["equipment"]
            if p.get("station_core") or total_cargo != 0:
                PENDING_P2P.pop(pubkey, None)
                return (
                    "Start with EMPTY holds -- you'll buy at the far port. "
                    "Jettison your cargo, then run p2p again."
                )

        PENDING_P2P[pubkey] = {
            "stage": "continue",
            "home": state["home"],
            "adj": state["adj"],
            "forward": forward,
            "backward": backward,
            "next_dest": state["adj"],
            "profit": 0,
        }
        text, ended = _p2p_run_leg(ctx, PENDING_P2P[pubkey])
        if ended:
            PENDING_P2P.pop(pubkey, None)
            return text
        return text + "\n\nContinue? y/n"

    # stage == "continue"
    if lower in ("n", "no", "s", "stop", "cancel"):
        PENDING_P2P.pop(pubkey, None)
        p = get_player_with_ship(pubkey)
        return f"P2P ended in Sec{p['sector_id']}. Net {_p2p_net(state)}."
    if lower in ("y", "yes", "c", "continue"):
        text, ended = _p2p_run_leg(ctx, state)
        if ended:
            PENDING_P2P.pop(pubkey, None)
            return text
        return text + "\n\nContinue? y/n"
    return "Continue the shuttle? Reply y to continue or n to stop."


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

    print(f"RX from {sender}: {message}")
    log_message("rx", pubkey, sender, message)

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

    mc = await MeshCore.create_serial("/dev/ttyACM0", 115200)
    print("Connected OK")

    result = await mc.commands.get_contacts()
    if result.type == EventType.ERROR:
        print(f"Error getting contacts: {result.payload}")

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

    print("Bot is running...")
    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
