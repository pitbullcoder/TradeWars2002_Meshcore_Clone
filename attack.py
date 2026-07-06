"""
The combat & recon command handlers -- everything filed under the
'combat' help submenu: laying mines, sending probes, and the full attack
flow (target selection, fighter commitment, and resolution against
ships, unmanned hulls, and space stations).

The pure damage/attrition math stays in combat.py; this module is the
db-touching layer that applies it. Escape-pod knockback destinations
come from this module's `random` (attack.random in the test suite's
monkeypatches).
"""

import random
import re

from db import (
    lay_mines,
    consume_probe,
    get_hostile_mine_total,
    detonate_one_hostile_mine,
    get_all_warps,
    get_adjacent_sectors,
    get_ships_in_sector,
    get_parked_ships_in_sector,
    get_station_in_sector,
    apply_station_upkeep,
    get_ships_docked_at_station,
    delete_station,
    delete_ship,
    set_ship_defenses,
    set_parked_ship_defenses,
    set_station_defenses,
    record_attack_event,
    record_kill,
    buy_ship,
    move_player_to_sector,
    SHIP_CATALOG,
    DEFAULT_SHIP_TYPE,
    ESCAPE_POD_SHIP,
    HOME_SECTOR,
    SAFE_ZONE_MAX_SECTOR,
    MIN_SECTOR_ID,
    MAX_SECTOR_ID,
    POD_KILL_RESET_CREDITS,
)
from core import command, PENDING_ATTACKS, ship_label
from display import build_sector_info
from combat import resolve_attack, _plural
from pathfinding import find_shortest_path


# --- Safe zone ---------------------------------------------------------
# Sectors 1..SAFE_ZONE_MAX_SECTOR (imported from db) are a protected zone
# around the Stardock: no mines may be laid there and no ship-to-ship
# combat is allowed, so new players can't be ambushed the moment they
# leave the Stardock. Galaxy generation also fully interconnects these
# sectors, so the Stardock stays reachable and can't be walled off.


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
