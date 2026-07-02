"""
Player-built space stations: deploying a Station Core kit, and the
owner-only dock flow for arming and managing a station (depositing
materials, transferring fighters, powering shields, setting posture, and
upgrading). The combat itself -- a station shooting an entrant, or a
player attacking a station -- lives in main.py alongside the rest of the
combat code; this module is the construction/management side.
"""

from db import (
    get_player_with_ship,
    get_station_in_sector,
    get_station,
    create_station,
    set_ship_station_core,
    set_ship_cargo,
    set_ship_defenses,
    set_station_defenses,
    set_station_posture,
    set_station_shields,
    deposit_to_station,
    apply_station_upkeep,
    start_station_upgrade,
    adjust_player_credits,
    station_caps,
    station_daily_fuel_burn,
    station_dock_capacity,
    get_parked_ships_in_sector,
    get_ships_docked_at_station,
    get_ship,
    dock_ship_at_station,
    undock_ship,
    transfer_station_credits,
    SAFE_ZONE_MAX_SECTOR,
    STATION_MAX_LEVEL,
    STATION_UPGRADES,
)
from datetime import datetime, timezone
from core import command, PENDING_STATIONS, _resume_navigation_suffix


def engaged_fighters(fighters, engage_pct):
    """How many of `fighters` a station commits at `engage_pct` percent
    (floored) -- the count it throws at a non-owner who enters while it's
    offensive."""
    return fighters * engage_pct // 100


def _shield_state(station):
    max_shields, _ = station_caps(station["level"])
    on = "ON" if station["shields_enabled"] else "OFF"
    return f"{station['shields']}/{max_shields} [{on}]"


def build_station_status(station):
    """The station's current state, shown on docking and after each action."""
    max_shields, max_fighters = station_caps(station["level"])
    lines = [
        f"Space Station - {station['owner_name']} (Lvl {station['level']}) Sec{station['sector_id']}",
        f"Shields: {_shield_state(station)}",
        f"Fighters: {station['fighters']}/{max_fighters}",
        f"Stockpile: {station['fuel']} fuel, {station['organics']} organics, "
        f"{station['equipment']} equipment",
    ]
    docked = get_ships_docked_at_station(station["id"])
    if docked:
        cap = station_dock_capacity(station["level"])
        listed = ", ".join(f"{s['ship_type']} #{s['id']}" for s in docked)
        lines.append(f"Docked ships ({len(docked)}/{cap}): {listed}")
    lines.append(f"Treasury: {station['credits']}cr")
    posture = station["posture"]
    if posture == "offensive":
        lines.append(f"Posture: offensive ({station['engage_pct']}% of fighters engage)")
    else:
        lines.append("Posture: defensive")
    if station["shields_enabled"]:
        lines.append(f"Shield upkeep: {station_daily_fuel_burn(station['level'])} fuel/day")
    if station["upgrade_to"] is not None:
        lines.append(f"Upgrade to Lvl {station['upgrade_to']} in progress.")
    return "\n".join(lines)


def build_station_menu(station):
    lines = [build_station_status(station), "", "Station options:"]
    lines.append("  1) Deposit all ship cargo (fuel/organics/equipment)")
    lines.append("  2) Transfer fighters from ship")
    lines.append("  3) " + ("Power down shields" if station["shields_enabled"] else "Power up shields"))
    lines.append("  4) Set posture (defensive/offensive)")
    if station["level"] < STATION_MAX_LEVEL and station["upgrade_to"] is None:
        lines.append(f"  5) Upgrade to Lvl {station['level'] + 1}")
    docked = get_ships_docked_at_station(station["id"])
    cap = station_dock_capacity(station["level"])
    lines.append(f"  6) Dock a spare ship ({len(docked)}/{cap} bays used)")
    if docked:
        lines.append("  7) Undock a ship")
    lines.append("  8) Treasury -- deposit/withdraw credits")
    lines.append("Reply with a number, or 'exit'.")
    return "\n".join(lines)


@command("deploy", description="deploy a Space Station Core kit in this sector", menu="combat")
async def cmd_deploy(ctx, args):
    """Drop a carried Station Core kit and raise a station in the current
    sector. Not allowed in the Sec1..SAFE_ZONE_MAX_SECTOR safe zone, and
    only one station may exist per sector."""
    p = ctx.player

    if not p.get("station_core"):
        return "You're not carrying a Space Station Core kit. Buy one at the Stardock."
    if p["sector_id"] <= SAFE_ZONE_MAX_SECTOR:
        return f"Can't deploy in the Sec1-{SAFE_ZONE_MAX_SECTOR} safe zone. Haul it further out."
    if get_station_in_sector(p["sector_id"]) is not None:
        return "There's already a space station in this sector -- only one is allowed."

    create_station(p["id"], p["name"], p["sector_id"])
    set_ship_station_core(p["id"], False)
    return (
        f"Deployed Space Station - {p['name']} in Sec{p['sector_id']}! It starts unarmed "
        "(0 shields, 0 fighters). Dock with 'station' to deposit fuel, transfer fighters, "
        "and power up."
    )


@command("station", "dock", description="dock at your space station in this sector")
async def cmd_station(ctx, args):
    """Open the management menu for the station in this sector. Owner only
    -- a non-owner can't access another player's station (they can attack
    it with 'a station')."""
    p = ctx.player
    station = get_station_in_sector(p["sector_id"])
    if station is None:
        return "There's no space station in this sector."

    station = apply_station_upkeep(station["id"])  # bring fuel burn / upgrades up to date
    if station["owner_id"] != p["id"]:
        return (
            f"Space Station - {station['owner_name']} isn't yours -- you can't access its "
            "systems. (Attack it with 'a station'.)"
        )

    PENDING_STATIONS[ctx.pubkey] = {"station_id": station["id"], "stage": "menu"}
    return build_station_menu(station)


def _exit(pubkey, p):
    PENDING_STATIONS.pop(pubkey, None)
    return "Undocked from the station." + _resume_navigation_suffix(pubkey, p["sector_id"])


async def cmd_station_step(ctx, message):
    """
    Advance an in-progress station visit (PENDING_STATIONS). Like the
    Stardock, it's menu-driven and open-ended: each action returns to the
    menu until the player replies 'exit'/'cancel'. Stages: "menu",
    "transfer_qty", "posture_choose", "posture_pct", "upgrade_confirm",
    "dock_choose", "undock_choose", "treasury_choose", "treasury_amount".
    """
    pubkey = ctx.pubkey
    text = message.strip()
    lower = text.lower()

    state = PENDING_STATIONS.get(pubkey)
    if not state:
        PENDING_STATIONS.pop(pubkey, None)
        return "No station visit in progress."

    p = get_player_with_ship(pubkey)
    if lower in ("exit", "cancel", "leave", "0"):
        return _exit(pubkey, p)

    station = get_station(state["station_id"])
    if station is None or station["owner_id"] != p["id"]:
        # Station was destroyed (or somehow no longer theirs) mid-visit.
        PENDING_STATIONS.pop(pubkey, None)
        return "This station is no longer here."

    stage = state["stage"]

    if stage == "menu":
        return _handle_menu_choice(ctx, state, station, p, text)
    if stage == "transfer_qty":
        return _handle_transfer_qty(ctx, state, station, p, text)
    if stage == "posture_choose":
        return _handle_posture_choose(ctx, state, station, p, lower)
    if stage == "posture_pct":
        return _handle_posture_pct(ctx, state, station, p, text)
    if stage == "upgrade_confirm":
        return _handle_upgrade_confirm(ctx, state, station, p, lower)
    if stage == "dock_choose":
        return _handle_dock_choose(ctx, state, station, p, lower)
    if stage == "undock_choose":
        return _handle_undock_choose(ctx, state, station, p, lower)
    if stage == "treasury_choose":
        return _handle_treasury_choose(ctx, state, station, p, lower)
    if stage == "treasury_amount":
        return _handle_treasury_amount(ctx, state, station, p, text)

    # Unknown stage -- reset to the menu defensively.
    state["stage"] = "menu"
    return build_station_menu(station)


def _menu(station):
    return "\n\n" + build_station_menu(station)


def _handle_menu_choice(ctx, state, station, p, text):
    if text == "1":
        return _deposit_all(state, station, p)
    if text == "2":
        max_shields, max_fighters = station_caps(station["level"])
        room = max_fighters - station["fighters"]
        if p["fighters"] <= 0:
            return "You have no fighters aboard to transfer." + _menu(station)
        if room <= 0:
            return f"Station fighter bay is full ({max_fighters})." + _menu(station)
        state["stage"] = "transfer_qty"
        return (
            f"Transfer how many fighters? You have {p['fighters']}, station has room for "
            f"{room}. Reply with a number, 'all', or 'cancel'."
        )
    if text == "3":
        return _toggle_shields(state, station, p)
    if text == "4":
        state["stage"] = "posture_choose"
        return "Set posture: reply 'd' for defensive or 'o' for offensive ('cancel' to go back)."
    if text == "5":
        return _begin_upgrade(state, station, p)
    if text == "6":
        return _begin_dock(state, station, p)
    if text == "7":
        return _begin_undock(state, station, p)
    if text == "8":
        state["stage"] = "treasury_choose"
        return (
            f"Treasury holds {station['credits']}cr; you're carrying "
            f"{p['credits']}cr. Reply 'd' to deposit or 'w' to withdraw "
            "('cancel' to go back)."
        )
    return "Reply with a number, or 'exit'." + _menu(station)


def _deposit_all(state, station, p):
    fuel, org, equ = p["fuel_ore"], p["organics"], p["equipment"]
    if fuel + org + equ <= 0:
        return "No cargo aboard to deposit." + _menu(get_station(station["id"]))
    deposit_to_station(station["id"], fuel=fuel, organics=org, equipment=equ)
    set_ship_cargo(p["id"], 0, 0, 0)
    return (
        f"Deposited {fuel} fuel, {org} organics, {equ} equipment into the station."
        + _menu(get_station(station["id"]))
    )


def _handle_transfer_qty(ctx, state, station, p, text):
    lower = text.lower()
    max_shields, max_fighters = station_caps(station["level"])
    room = max_fighters - station["fighters"]
    available = min(p["fighters"], room)
    if lower == "all":
        qty = available
    elif text.isdigit():
        qty = int(text)
    else:
        return f"Reply with a number from 1 to {available}, 'all', or 'cancel'."
    if qty <= 0:
        state["stage"] = "menu"
        return "Transferred nothing." + _menu(station)
    if qty > available:
        return f"Can only transfer {available} (ship fighters / station room)."
    set_ship_defenses(p["id"], p["shields"], p["fighters"] - qty)
    set_station_defenses(station["id"], station["shields"], station["fighters"] + qty)
    state["stage"] = "menu"
    return f"Transferred {qty} fighters to the station." + _menu(get_station(station["id"]))


def _toggle_shields(state, station, p):
    if station["shields_enabled"]:
        set_station_shields(station["id"], enabled=False, shields=0)
        return "Shields powered down." + _menu(get_station(station["id"]))
    # Enabling: need at least one day's worth of fuel in the stockpile.
    daily = station_daily_fuel_burn(station["level"])
    max_shields, _ = station_caps(station["level"])
    if station["fuel"] < daily:
        return (
            f"Not enough fuel to power shields -- need {daily}/day, stockpile has "
            f"{station['fuel']}. Deposit fuel first." + _menu(station)
        )
    set_station_shields(
        station["id"], enabled=True, shields=max_shields,
        last_fuel_burn=datetime.now(timezone.utc).isoformat(),
    )
    return (
        f"Shields online at {max_shields} ({daily} fuel/day)."
        + _menu(get_station(station["id"]))
    )


def _handle_posture_choose(ctx, state, station, p, lower):
    if lower in ("d", "defensive"):
        set_station_posture(station["id"], "defensive")
        state["stage"] = "menu"
        return "Posture set to defensive." + _menu(get_station(station["id"]))
    if lower in ("o", "offensive"):
        state["stage"] = "posture_pct"
        return "What % of fighters should engage entrants? Reply 1-100 ('cancel' to go back)."
    return "Reply 'd' for defensive or 'o' for offensive ('cancel' to go back)."


def _handle_posture_pct(ctx, state, station, p, text):
    if not text.isdigit():
        return "Reply with a number 1-100, or 'cancel'."
    pct = int(text)
    if pct < 1 or pct > 100:
        return "Engage % must be between 1 and 100."
    set_station_posture(station["id"], "offensive", engage_pct=pct)
    state["stage"] = "menu"
    return (
        f"Posture set to offensive, engaging {pct}% of fighters."
        + _menu(get_station(station["id"]))
    )


def _begin_upgrade(state, station, p):
    if station["level"] >= STATION_MAX_LEVEL:
        return f"Station is already at the max level ({STATION_MAX_LEVEL})." + _menu(station)
    if station["upgrade_to"] is not None:
        return "An upgrade is already underway." + _menu(station)
    target = station["level"] + 1
    spec = STATION_UPGRADES[target]
    short = []
    if p["credits"] < spec["credits"]:
        short.append(f"{spec['credits']}cr (have {p['credits']})")
    if station["fuel"] < spec["fuel"]:
        short.append(f"{spec['fuel']} fuel (have {station['fuel']})")
    if station["organics"] < spec["organics"]:
        short.append(f"{spec['organics']} organics (have {station['organics']})")
    if station["equipment"] < spec["equipment"]:
        short.append(f"{spec['equipment']} equipment (have {station['equipment']})")
    if short:
        return "Can't start the upgrade -- short: " + "; ".join(short) + _menu(station)
    state["stage"] = "upgrade_confirm"
    state["upgrade_to"] = target
    return (
        f"Upgrade to Lvl {target}: costs {spec['credits']}cr + {spec['fuel']} fuel / "
        f"{spec['organics']} organics / {spec['equipment']} equipment from the stockpile, "
        f"and takes {spec['days']} days. Confirm? yes/no"
    )


def _handle_upgrade_confirm(ctx, state, station, p, lower):
    target = state.get("upgrade_to")
    if lower not in ("y", "yes"):
        state["stage"] = "menu"
        return "Upgrade cancelled." + _menu(station)
    spec = STATION_UPGRADES[target]
    # Re-check now (materials/credits could have changed within the visit).
    if (p["credits"] < spec["credits"] or station["fuel"] < spec["fuel"]
            or station["organics"] < spec["organics"] or station["equipment"] < spec["equipment"]):
        state["stage"] = "menu"
        return "No longer have what the upgrade needs." + _menu(station)
    adjust_player_credits(p["id"], -spec["credits"])
    start_station_upgrade(station["id"], target)
    state["stage"] = "menu"
    return (
        f"Upgrade to Lvl {target} started -- completes in {spec['days']} days. "
        f"Paid {spec['credits']}cr and drew the materials from the stockpile."
        + _menu(get_station(station["id"]))
    )


def _dock_bay_room(station):
    """(docked_count, capacity, room) for the station's bays."""
    docked = get_ships_docked_at_station(station["id"])
    cap = station_dock_capacity(station["level"])
    return len(docked), cap, cap - len(docked)


def _own_spares_in_sector(p):
    """The player's own unmanned ships sitting out in the open in their
    current sector -- the dockable candidates. (Docked ships are already
    excluded by get_parked_ships_in_sector itself.)"""
    return [s for s in get_parked_ships_in_sector(p["sector_id"])
            if s["owner_id"] == p["id"]]


def _begin_dock(state, station, p):
    used, cap, room = _dock_bay_room(station)
    if room <= 0:
        return f"Docking bays are full ({used}/{cap})." + _menu(station)
    spares = _own_spares_in_sector(p)
    if not spares:
        return ("No spare ships of yours out in this sector to dock."
                + _menu(station))
    state["stage"] = "dock_choose"
    state["queue"] = spares
    state["idx"] = 0
    return _dock_prompt(spares, 0, room)


def _dock_prompt(queue, idx, room):
    ship = queue[idx]
    extras = []
    if idx + 1 < len(queue):
        extras.append("no = next ship")
    if len(queue) > 1 and room > 1:
        extras.append(f"'all' docks up to {room}")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"Dock your {ship['ship_type']} #{ship['id']}? yes/no{suffix}"


def _handle_dock_choose(ctx, state, station, p, lower):
    queue, idx = state["queue"], state["idx"]
    used, cap, room = _dock_bay_room(station)

    if lower in ("cancel", "0"):
        state["stage"] = "menu"
        return "Docking cancelled." + _menu(station)

    if lower in ("n", "no"):
        idx += 1
        if idx >= len(queue):
            state["stage"] = "menu"
            return "No more spare ships here." + _menu(station)
        state["idx"] = idx
        return _dock_prompt(queue, idx, room)

    if lower == "all":
        docked_now = []
        for ship in queue[idx:]:
            if room <= 0:
                break
            if _dock_one(ship, station, p):
                docked_now.append(f"{ship['ship_type']} #{ship['id']}")
                room -= 1
        state["stage"] = "menu"
        if not docked_now:
            return "Nothing docked." + _menu(get_station(station["id"]))
        note = " Bays are now full." if room <= 0 else ""
        return (f"Docked {', '.join(docked_now)} at the station.{note}"
                + _menu(get_station(station["id"])))

    if lower in ("y", "yes"):
        ship = queue[idx]
        if room <= 0:
            state["stage"] = "menu"
            return f"Docking bays are full ({used}/{cap})." + _menu(station)
        if not _dock_one(ship, station, p):
            state["stage"] = "menu"
            return "That ship is no longer here." + _menu(station)
        docked_line = (f"Docked your {ship['ship_type']} #{ship['id']} -- it's "
                       "sheltered from attack while the station stands.")
        idx += 1
        room -= 1
        if idx >= len(queue) or room <= 0:
            state["stage"] = "menu"
            return docked_line + _menu(get_station(station["id"]))
        state["idx"] = idx
        return docked_line + "\n" + _dock_prompt(queue, idx, room)

    return "Reply 'yes' to dock it, 'no' for the next ship, 'all', or 'cancel'."


def _dock_one(ship, station, p):
    """Re-validate and dock a single spare; False if it's no longer a
    dockable candidate (moved, sold, destroyed, or already docked)."""
    live = get_ship(ship["id"])
    if (live is None or live["owner_id"] != p["id"]
            or live["sector_id"] != station["sector_id"]
            or live.get("docked_station_id") is not None):
        return False
    dock_ship_at_station(ship["id"], station["id"])
    return True


def _begin_undock(state, station, p):
    docked = get_ships_docked_at_station(station["id"])
    if not docked:
        return "No ships are docked here." + _menu(station)
    state["stage"] = "undock_choose"
    state["queue"] = docked
    state["idx"] = 0
    return _undock_prompt(docked, 0)


def _undock_prompt(queue, idx):
    ship = queue[idx]
    extras = ["no = next ship"] if idx + 1 < len(queue) else []
    if len(queue) > 1:
        extras.append("'all' undocks every ship")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"Undock your {ship['ship_type']} #{ship['id']}? yes/no{suffix}"


def _handle_undock_choose(ctx, state, station, p, lower):
    queue, idx = state["queue"], state["idx"]

    if lower in ("cancel", "0"):
        state["stage"] = "menu"
        return "Undocking cancelled." + _menu(station)

    if lower in ("n", "no"):
        idx += 1
        if idx >= len(queue):
            state["stage"] = "menu"
            return "No more docked ships." + _menu(station)
        state["idx"] = idx
        return _undock_prompt(queue, idx)

    if lower == "all":
        for ship in queue[idx:]:
            undock_ship(ship["id"])
        names = ", ".join(f"{s['ship_type']} #{s['id']}" for s in queue[idx:])
        state["stage"] = "menu"
        return (f"Undocked {names} -- parked out in Sec{station['sector_id']} "
                "(attackable again)." + _menu(get_station(station["id"])))

    if lower in ("y", "yes"):
        ship = queue[idx]
        undock_ship(ship["id"])
        line = (f"Undocked your {ship['ship_type']} #{ship['id']} -- parked out in "
                f"Sec{station['sector_id']} (attackable again).")
        idx += 1
        if idx >= len(queue):
            state["stage"] = "menu"
            return line + _menu(get_station(station["id"]))
        state["idx"] = idx
        return line + "\n" + _undock_prompt(queue, idx)

    return "Reply 'yes' to undock it, 'no' for the next ship, 'all', or 'cancel'."


def _handle_treasury_choose(ctx, state, station, p, lower):
    if lower in ("cancel", "0"):
        state["stage"] = "menu"
        return "Treasury closed." + _menu(station)
    if lower in ("d", "deposit"):
        if p["credits"] <= 0:
            state["stage"] = "menu"
            return "You're not carrying any credits to deposit." + _menu(station)
        state["stage"] = "treasury_amount"
        state["direction"] = "deposit"
        return (
            f"Deposit how much? You're carrying {p['credits']}cr. "
            "Reply with a number, 'all', or 'cancel'."
        )
    if lower in ("w", "withdraw"):
        if station["credits"] <= 0:
            state["stage"] = "menu"
            return "The treasury is empty." + _menu(station)
        state["stage"] = "treasury_amount"
        state["direction"] = "withdraw"
        return (
            f"Withdraw how much? The treasury holds {station['credits']}cr. "
            "Reply with a number, 'all', or 'cancel'."
        )
    return "Reply 'd' to deposit, 'w' to withdraw, or 'cancel'."


def _handle_treasury_amount(ctx, state, station, p, text):
    lower = text.lower()
    depositing = state.get("direction") == "deposit"
    available = p["credits"] if depositing else station["credits"]

    if lower == "cancel":
        state["stage"] = "menu"
        return "Treasury closed." + _menu(station)
    if lower == "all":
        qty = available
    elif text.isdigit():
        qty = int(text)
    else:
        return f"Reply with a number from 1 to {available}, 'all', or 'cancel'."
    if qty <= 0:
        state["stage"] = "menu"
        return "Nothing moved." + _menu(station)
    if qty > available:
        where = "You're only carrying" if depositing else "The treasury only holds"
        return f"{where} {available}cr."

    transfer_station_credits(p["id"], station["id"], qty if depositing else -qty)
    state["stage"] = "menu"
    fresh = get_station(station["id"])
    if depositing:
        line = (f"Deposited {qty}cr into the treasury -- it's safe there even "
                "if you're blown up (but goes down with the station).")
    else:
        line = f"Withdrew {qty}cr from the treasury."
    return line + _menu(fresh)
