"""
Arrival, relocation, and hull-handling: everything that changes where a
player or ship is. enter_sector is the single arrival path (mine
detonation and offensive-station fire resolve here), backed by the move,
multi-hop warp, tow, and board commands.

This module owns the RNG seam: enter_sector, _eject_player, and their
callers default to this module's `random`, which is what the test suite
monkeypatches (movement.random = FakeRandom(...)) for deterministic
mine damage, escape-pod drift, and knockback destinations.
"""

import random
import re

from db import (
    get_ship,
    set_towing,
    move_player_to_sector,
    move_ship_to_sector,
    spend_turn,
    spend_turns,
    get_player_with_ship,
    get_hostile_mine_total,
    clear_hostile_mines,
    buy_ship,
    record_kill,
    set_ship_defenses,
    set_station_defenses,
    get_station_in_sector,
    apply_station_upkeep,
    get_adjacent_sectors,
    get_all_warps,
    get_port,
    get_parked_ships_in_sector,
    swap_active_ship,
    SHIP_CATALOG,
    ESCAPE_POD_SHIP,
    DEFAULT_SHIP_TYPE,
    HOME_SECTOR,
    MIN_SECTOR_ID,
    MAX_SECTOR_ID,
    POD_KILL_RESET_CREDITS,
    TOW_TURNS_PER_SECTOR,
)
from core import (
    command,
    parse,
    COMMANDS,
    PENDING_WARPS,
    PENDING_TRADES,
    PENDING_UPGRADES,
    PENDING_TOWS,
    PENDING_BOARDS,
    ship_label,
    _warp_confirm_options,
    _resume_navigation_suffix,
)
from display import build_sector_info
from combat import roll_mine_damage, apply_mine_damage, resolve_attack, _plural
from pathfinding import find_shortest_path, choose_escape_sector
from station import engaged_fighters
from trading import cmd_trade


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
