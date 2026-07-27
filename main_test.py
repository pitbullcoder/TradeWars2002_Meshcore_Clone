"""
Unit tests for the guided port-trading flow in main.py.

Centerpiece scenario (matches the worked example from the design
discussion): a trader docks at an SSB port carrying 15 units of
Equipment, with 5 free cargo holds and 10,000cr.

    1. Port offers to sell Equipment (it buys what they're carrying) --
       player sells 5 units, freeing up cargo space.
    2. Port then offers to buy Fuel Ore, now that more holds are free --
       player buys 5 units.
    3. Port offers to buy Organics with the remaining free holds --
       player buys 5 units, filling the hold completely.
    4. The port visit ends on its own once nothing's left to offer.

A couple of supporting tests cover the related behaviors discussed
alongside it: replying with a quantity of 0 (or 'no' at the price-confirm
step) skips to the next item rather than ending the whole visit, while
'cancel' ends it immediately.

This stubs out the `db` and `meshcore` modules main.py imports from, so
it runs standalone -- no real database, radio, or network needed. Run it
with either of:

    python3 main_test.py
    python3 -m unittest main_test -v

It expects main.py to be importable from the same directory.
"""

import importlib
import itertools
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Stub out `db` and `meshcore` *before* main.py is imported, since main.py
# does `from db import (...)` and `from meshcore import MeshCore, EventType`
# at module load time.
# ---------------------------------------------------------------------------

# Mutable fixture state the stub db functions read/write. Tests reset its
# *contents* in setUp() (rather than rebinding it) so the stub functions
# defined once below always see the current test's data.
STATE = {
    "player": {},
    "port": {},
    "trade_log": [],
    "upgrade_log": [],
    "ship_log": [],
    # Per-sector fixtures for the navigation/docking tests below. Left
    # empty by the port-trade and Stardock tests above, which only ever
    # care about one port (whichever sector the player is currently
    # standing in) and don't exercise movement at all.
    "ports": {},
    "warps": {},
    # Mine fixtures: sector_id -> {owner_player_id: qty deployed there}.
    # mine_log/defense_log record calls the way trade_log/ship_log do, so
    # tests can assert on what the lay/detonation paths did.
    "sector_mines": {},
    "mine_log": [],
    "defense_log": [],
    "probe_log": [],
    # Player presence fixture: sector_id -> [{"id":, "name":}, ...]. Empty
    # by default, so build_sector_info shows no "Ships here" line unless a
    # test populates it (keeping every other suite's output unchanged).
    "sector_players": {},
    # Attack fixtures. players_by_id lets the mutation stubs target a
    # SECOND player (the defender) instead of the single STATE["player"];
    # move_log/attack_events record relocation and victim-notice calls.
    "players_by_id": {},
    "move_log": [],
    "attack_events": [],
    # Public kill-log fixtures. "kills" is the global append-only log (the
    # record_kill stub appends here); "kill_log_cutoff" maps player_id ->
    # the timestamp through which they've seen the log (their last sign-in).
    "kills": [],
    "kill_log_cutoff": {},
    # Space-station fixtures: id -> station dict. _stub_create_station
    # assigns ids from next_station_id. Empty by default so the info screen
    # and enter_sector show/run no station logic unless a test sets one.
    "stations": {},
    "next_station_id": 1,
    # Multi-ship fixtures: unmanned (parked/towed) hulls, keyed by ship id.
    # Each is a dict shaped like db.get_parked_ships_in_sector's rows
    # (id, owner_id, owner_name, ship_type, fighters, shields, sector_id),
    # plus any full-ship stats a board/swap test needs. The *_log lists
    # record mutations the way ship_log/defense_log do. next_ship_id feeds
    # _stub_park_and_buy_ship. Reset by _reset_multiship_state() in every
    # suite's setUp.
    "parked_ships": {},
    "parked_defense_log": [],
    # (pubkey_prefix, text) -> ISO timestamp of the last logged tx; feeds
    # _stub_get_last_tx_time for the channel-advertisement scheduler.
    "last_tx_times": {},
    "ship_move_log": [],
    "ship_delete_log": [],
    "park_buy_log": [],
    "next_ship_id": 100,
}


def _reset_multiship_state():
    STATE["parked_ships"] = {}
    STATE["parked_defense_log"] = []
    STATE["ship_move_log"] = []
    STATE["ship_delete_log"] = []
    STATE["park_buy_log"] = []
    STATE["next_ship_id"] = 100


def _stub_get_port(sector_id):
    # Navigation tests populate "ports" per sector_id; everything else
    # uses the single "port" fixture regardless of which sector_id is
    # asked for, since those tests only ever have the player docked in
    # one place. Either way, mirror the real get_port's contract of
    # returning None (not an empty dict) when there's nothing there --
    # callers like _warp_confirm_options rely on `is not None`.
    if STATE["ports"]:
        port = STATE["ports"].get(sector_id)
        return dict(port) if port is not None else None
    return dict(STATE["port"]) if STATE["port"] else None


def _stub_get_adjacent_sectors(sector_id):
    return list(STATE["warps"].get(sector_id, []))


def _stub_get_all_warps():
    return {k: list(v) for k, v in STATE["warps"].items()}


def _player_by_id(player_id):
    """Resolve a player dict by id: the single STATE['player'] (the usual
    case), or an entry in the players_by_id registry (a second player,
    e.g. an attack's defender). None if neither knows the id."""
    if STATE["player"].get("id") == player_id:
        return STATE["player"]
    return STATE["players_by_id"].get(player_id)


def _stub_move_player_to_sector(player_id, sector_id):
    STATE["move_log"].append((player_id, sector_id))
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["sector_id"] = sector_id


def _stub_spend_turn(player_id):
    pl = _player_by_id(player_id)
    if pl is not None and pl.get("turns_remaining", 0) > 0:
        pl["turns_remaining"] -= 1


def _stub_get_player_with_ship(pubkey):
    return dict(STATE["player"])


def _stub_get_players_in_sector(sector_id, exclude_player_id=None):
    # Mirrors the real db.get_players_in_sector's new contract: a list of
    # {"name", "fighters"} dicts. Presence fixtures carry "fighters" (and
    # may carry "shields", which this deliberately drops -- shields never
    # surface on the sector-info screen).
    here = STATE["sector_players"].get(sector_id, [])
    return [{"name": pl["name"], "fighters": pl.get("fighters", 0)} for pl in here
            if exclude_player_id is None or pl["id"] != exclude_player_id]


def _stub_get_ships_in_sector(sector_id, exclude_player_id=None):
    return [
        dict(pl) for pid, pl in STATE["players_by_id"].items()
        if pl.get("sector_id") == sector_id and pid != exclude_player_id
    ]


def _stub_record_attack_event(victim_id, attacker_name, sector_id, outcome):
    STATE["attack_events"].append({
        "victim_id": victim_id,
        "attacker_name": attacker_name,
        "sector_id": sector_id,
        "outcome": outcome,
        "created_at": "2026-06-27T12:00:00+00:00",
    })


def _stub_pop_attack_events(player_id):
    pending = [e for e in STATE["attack_events"]
               if e["victim_id"] == player_id and not e.get("delivered")]
    for e in pending:
        e["delivered"] = True
    return pending


# Shared monotonic fake clock for the kill-log stubs. record_kill and
# mark_kill_log_seen both pull from it, so their timestamps order by call
# order exactly as the real wall-clock now() would -- a kill recorded
# after a sign-in lands *after* that sign-in's cutoff, etc. Fixed-width
# and zero-padded so the strings sort lexicographically.
_kill_clock = itertools.count(1)


def _kill_ts():
    return f"2026-06-27T12:00:00.{next(_kill_clock):09d}+00:00"


def _stub_record_kill(victim_name, killer_name, sector_id, kind):
    STATE["kills"].append({
        "victim_name": victim_name,
        "killer_name": killer_name,
        "sector_id": sector_id,
        "kind": kind,
        "created_at": _kill_ts(),
    })


def _stub_get_kills_since(cutoff_iso, limit=None):
    if cutoff_iso is None:
        return []
    res = sorted((k for k in STATE["kills"] if k["created_at"] > cutoff_iso),
                 key=lambda k: k["created_at"])
    if limit is not None:
        res = res[:limit]
    return [dict(k) for k in res]


def _stub_get_kill_log_cutoff(player_id):
    return STATE["kill_log_cutoff"].get(player_id)


def _stub_mark_kill_log_seen(player_id):
    STATE["kill_log_cutoff"][player_id] = _kill_ts()


def _stub_get_or_create_player(pubkey, sender):
    return dict(STATE["player"]), False


def _stub_execute_trade(player_id, port_id, key, qty, total_price, player_is_buying):
    STATE["trade_log"].append((key, qty, total_price, player_is_buying))
    player = STATE["player"]
    # Mutate the actual backing dict (not a get_port()-returned copy) so
    # the change is visible on the next get_port() call. The per-sector
    # "ports" fixture (navigation tests) is looked up by id; everything
    # else just has the one "port" fixture regardless of port_id.
    if STATE["ports"]:
        port = next(p for p in STATE["ports"].values() if p["id"] == port_id)
    else:
        port = STATE["port"]
    if player_is_buying:
        player[key] += qty
        player["credits"] -= total_price
        port[f"{key}_qty"] -= qty
    else:
        player[key] -= qty
        player["credits"] += total_price
        port[f"{key}_qty"] += qty


def _stub_apply_port_restock(port_id, now=None):
    # Unit tests set port stock explicitly, so restock is a no-op here: it
    # just returns the live port row (looked up by id like _stub_execute_trade).
    if STATE["ports"]:
        port = next((p for p in STATE["ports"].values() if p["id"] == port_id), None)
        return dict(port) if port is not None else None
    return dict(STATE["port"]) if STATE["port"] else None


def _stub_upgrade_ship_stat(player_id, stat_column, qty, total_price):
    STATE["upgrade_log"].append((stat_column, qty, total_price))
    player = STATE["player"]
    player[stat_column] += qty
    player["credits"] -= total_price


def _stub_lay_mines(player_id, sector_id, qty):
    STATE["mine_log"].append((sector_id, player_id, qty))
    # Only decrement the aboard count if this is the active player (it
    # always is for the lay-command tests, which is the only path that
    # calls this).
    if STATE["player"].get("id") == player_id:
        STATE["player"]["mines"] -= qty
    sec = STATE["sector_mines"].setdefault(sector_id, {})
    sec[player_id] = sec.get(player_id, 0) + qty


def _stub_get_hostile_mine_total(sector_id, player_id):
    sec = STATE["sector_mines"].get(sector_id, {})
    return sum(q for owner, q in sec.items() if owner != player_id)


def _stub_clear_hostile_mines(sector_id, player_id):
    sec = STATE["sector_mines"].get(sector_id, {})
    for owner in list(sec):
        if owner != player_id:
            del sec[owner]


def _stub_consume_probe(player_id):
    STATE["probe_log"].append(player_id)
    if STATE["player"].get("id") == player_id:
        STATE["player"]["probes"] -= 1


def _stub_detonate_one_hostile_mine(sector_id, player_id):
    sec = STATE["sector_mines"].get(sector_id, {})
    for owner in list(sec):
        if owner != player_id and sec[owner] > 0:
            sec[owner] -= 1
            if sec[owner] <= 0:
                del sec[owner]
            return  # only one mine is spent on a probe


def _stub_set_ship_defenses(player_id, shields, fighters):
    STATE["defense_log"].append((player_id, shields, fighters))
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["shields"] = shields
        pl["fighters"] = fighters


# --- Multi-ship stubs ---------------------------------------------------
# Parked/towed hulls live in STATE["parked_ships"]; the active ship's
# stats stay flattened onto the player dict, exactly as the real
# get_player_with_ship join presents them.

_SHIP_STAT_KEYS = ("ship_type", "holds_total", "fighters", "shields", "mines",
                   "probes", "fuel_ore", "organics", "equipment", "station_core")


def _stub_get_parked_ships_in_sector(sector_id):
    # Stable id order, mirroring the real query's ORDER BY ships.id --
    # and, like the real query, ships docked inside a station are
    # excluded (that exclusion is what shelters them).
    return sorted(
        (dict(s) for s in STATE["parked_ships"].values()
         if s.get("sector_id") == sector_id
         and s.get("docked_station_id") is None),
        key=lambda s: s["id"],
    )


def _stub_get_ships_docked_at_station(station_id):
    return sorted(
        (dict(s) for s in STATE["parked_ships"].values()
         if s.get("docked_station_id") == station_id),
        key=lambda s: s["id"],
    )


def _stub_get_last_tx_time(pubkey_prefix, text):
    return STATE["last_tx_times"].get((pubkey_prefix, text))


def _stub_transfer_station_credits(player_id, station_id, amount):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["credits"] -= amount
    st = STATE["stations"].get(station_id)
    if st is not None:
        st["credits"] = st.get("credits", 0) + amount


def _stub_dock_ship_at_station(ship_id, station_id):
    s = STATE["parked_ships"].get(ship_id)
    if s is not None:
        s["docked_station_id"] = station_id
    # Mirror the real function: docking releases any tow on the hull.
    for pl in [STATE["player"], *STATE["players_by_id"].values()]:
        if pl.get("towing_ship_id") == ship_id:
            pl["towing_ship_id"] = None


def _stub_undock_ship(ship_id):
    s = STATE["parked_ships"].get(ship_id)
    if s is not None:
        s["docked_station_id"] = None


# Mirrors db.station_dock_capacity (pure).
DOCKED_SHIPS_PER_LEVEL = 2


def station_dock_capacity(level):
    return DOCKED_SHIPS_PER_LEVEL * level


def _stub_get_ship(ship_id):
    s = STATE["parked_ships"].get(ship_id)
    return dict(s) if s else None


def _stub_set_parked_ship_defenses(ship_id, shields, fighters):
    STATE["parked_defense_log"].append((ship_id, shields, fighters))
    s = STATE["parked_ships"].get(ship_id)
    if s is not None:
        s["shields"] = shields
        s["fighters"] = fighters


def _stub_move_ship_to_sector(ship_id, sector_id):
    STATE["ship_move_log"].append((ship_id, sector_id))
    s = STATE["parked_ships"].get(ship_id)
    if s is not None:
        s["sector_id"] = sector_id


def _stub_delete_ship(ship_id):
    STATE["ship_delete_log"].append(ship_id)
    STATE["parked_ships"].pop(ship_id, None)
    # Mirror the real delete_ship: anyone towing the deleted hull lets go.
    for pl in [STATE["player"], *STATE["players_by_id"].values()]:
        if pl.get("towing_ship_id") == ship_id:
            pl["towing_ship_id"] = None


def _stub_set_towing(player_id, ship_id):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["towing_ship_id"] = ship_id


def _stub_spend_turns(player_id, n):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["turns_remaining"] = max(0, pl.get("turns_remaining", 0) - n)


def _stub_swap_active_ship(player_id, ship_id, park_sector_id):
    pl = _player_by_id(player_id)
    target = STATE["parked_ships"].pop(ship_id)
    if pl["ship_type"] != ESCAPE_POD_SHIP:
        old = {"id": pl.get("ship_id"), "owner_id": pl["id"],
               "owner_name": pl["name"], "sector_id": park_sector_id}
        for k in _SHIP_STAT_KEYS:
            old[k] = pl.get(k, 0)
        STATE["parked_ships"][old["id"]] = old
    for k in _SHIP_STAT_KEYS:
        pl[k] = target.get(k, 0)
    pl["ship_id"] = ship_id
    pl["towing_ship_id"] = None


def _stub_park_and_buy_ship(player_id, park_sector_id, ship_type, holds_total,
                            fighters, shields, mines, credit_delta):
    STATE["park_buy_log"].append(
        (ship_type, holds_total, fighters, shields, mines, credit_delta, park_sector_id)
    )
    pl = _player_by_id(player_id)
    old = {"id": pl.get("ship_id"), "owner_id": pl["id"],
           "owner_name": pl["name"], "sector_id": park_sector_id}
    for k in _SHIP_STAT_KEYS:
        old[k] = pl.get(k, 0)
    STATE["parked_ships"][old["id"]] = old
    new_id = STATE["next_ship_id"]
    STATE["next_ship_id"] += 1
    pl["ship_type"] = ship_type
    pl["holds_total"] = holds_total
    pl["fighters"] = fighters
    pl["shields"] = shields
    pl["mines"] = mines
    pl["probes"] = 0
    pl["fuel_ore"] = 0
    pl["organics"] = 0
    pl["equipment"] = 0
    pl["station_core"] = 0
    pl["ship_id"] = new_id
    pl["towing_ship_id"] = None
    pl["credits"] += credit_delta
    return new_id


# Mirrors db.SHIP_CATALOG -- kept as a separate copy here (rather than
# importing the real db module) since this whole file exists to test
# main.py without a real db module loaded at all.
SHIP_CATALOG = {
    "Falcon": {
        "classification": "Frigate",
        "price": 0,
        "base_holds": 20,
        "base_fighters": 10,
        "base_shields": 10,
        "base_mines": 0,
        "max_holds": 75,
        "max_fighters": 50,
        "max_shields": 200,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 10,
    },
    "Kestrel": {
        "classification": "Corvette",
        "price": 8000,
        "base_holds": 15,
        "base_fighters": 20,
        "base_shields": 20,
        "base_mines": 0,
        "max_holds": 40,
        "max_fighters": 120,
        "max_shields": 150,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 25,
    },
    "Mule": {
        "classification": "Fleet Tender",
        "price": 40000,
        "base_holds": 40,
        "base_fighters": 0,
        "base_shields": 30,
        "base_mines": 0,
        "max_holds": 120,
        "max_fighters": 5,
        "max_shields": 250,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 10,
    },
    "Barracuda": {
        "classification": "Destroyer",
        "price": 120000,
        "base_holds": 20,
        "base_fighters": 120,
        "base_shields": 150,
        "base_mines": 0,
        "max_holds": 60,
        "max_fighters": 900,
        "max_shields": 1200,
        "max_mines": 20,
        "base_probes": 0,
        "max_probes": 15,
    },
    "Nautilus": {
        "classification": "Minelayer",
        "price": 180000,
        "base_holds": 25,
        "base_fighters": 40,
        "base_shields": 120,
        "base_mines": 10,
        "max_holds": 80,
        "max_fighters": 400,
        "max_shields": 1000,
        "max_mines": 150,
        "base_probes": 0,
        "max_probes": 15,
    },
    "SS Endeavour": {
        "classification": "Merchant Freighter",
        "price": 200000,
        "base_holds": 50,
        "base_fighters": 0,
        "base_shields": 50,
        "base_mines": 0,
        "max_holds": 200,
        "max_fighters": 10,
        "max_shields": 400,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 10,
    },
    "Hornet": {
        "classification": "Fleet Carrier",
        "price": 350000,
        "base_holds": 15,
        "base_fighters": 400,
        "base_shields": 100,
        "base_mines": 0,
        "max_holds": 50,
        "max_fighters": 3000,
        "max_shields": 800,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 20,
    },
    "Vanguard": {
        "classification": "Battlecruiser",
        "price": 450000,
        "base_holds": 20,
        "base_fighters": 150,
        "base_shields": 600,
        "base_mines": 0,
        "max_holds": 70,
        "max_fighters": 1500,
        "max_shields": 5000,
        "max_mines": 30,
        "base_probes": 0,
        "max_probes": 20,
    },
    "Bismark": {
        "classification": "Capital Ship",
        "price": 500000,
        "base_holds": 30,
        "base_fighters": 200,
        "base_shields": 500,
        "base_mines": 0,
        "max_holds": 125,
        "max_fighters": 2000,
        "max_shields": 3500,
        "max_mines": 50,
        "base_probes": 0,
        "max_probes": 20,
    },
    "Escape Pod": {
        "classification": "Escape Pod",
        "price": 0,
        "base_holds": 0,
        "base_fighters": 0,
        "base_shields": 0,
        "base_mines": 0,
        "max_holds": 0,
        "max_fighters": 0,
        "max_shields": 0,
        "max_mines": 0,
        "base_probes": 0,
        "max_probes": 0,
        "purchasable": False,
    },
}
DEFAULT_SHIP_TYPE = "Falcon"
ESCAPE_POD_SHIP = "Escape Pod"
SHIP_RESALE_FRACTION = 0.5
HOME_SECTOR = 1
SAFE_ZONE_MAX_SECTOR = 10
MIN_SECTOR_ID = 1
MAX_SECTOR_ID = 1000
POD_KILL_RESET_CREDITS = 20000
TOW_TURNS_PER_SECTOR = 5

# Station constants/helpers mirrored from db (pure -- no persistence).
STATION_CORE_PRICE = 5_000_000
STATION_CORE_HOLDS = 150
STATION_MAX_LEVEL = 4
STATION_LEVEL_CAPS = {
    1: {"max_shields": 1000, "max_fighters": 1000},
    2: {"max_shields": 2500, "max_fighters": 2500},
    3: {"max_shields": 5000, "max_fighters": 5000},
    4: {"max_shields": 10000, "max_fighters": 10000},
}
STATION_UPGRADES = {
    2: {"credits": 10_000_000, "fuel": 2500, "organics": 2000, "equipment": 1000, "days": 5},
    3: {"credits": 12_500_000, "fuel": 3500, "organics": 2500, "equipment": 1750, "days": 8},
    4: {"credits": 15_000_000, "fuel": 5000, "organics": 3500, "equipment": 2000, "days": 12},
}
SHIELD_FUEL_BURN_PER_SHIELD = 0.1


def station_caps(level):
    caps = STATION_LEVEL_CAPS[level]
    return caps["max_shields"], caps["max_fighters"]


def station_daily_fuel_burn(level):
    return round(SHIELD_FUEL_BURN_PER_SHIELD * station_caps(level)[0])


def _stub_sell_value(ship_type):
    return round(SHIP_CATALOG[ship_type]["price"] * SHIP_RESALE_FRACTION)


def _stub_buy_ship(player_id, ship_type, holds_total, fighters, shields, mines, credit_delta):
    STATE["ship_log"].append((ship_type, holds_total, fighters, shields, mines, credit_delta))
    player = _player_by_id(player_id)
    if player is None:
        return
    player["ship_type"] = ship_type
    player["holds_total"] = holds_total
    player["fighters"] = fighters
    player["shields"] = shields
    player["mines"] = mines
    player["probes"] = 0
    player["fuel_ore"] = 0
    player["organics"] = 0
    player["equipment"] = 0
    player["credits"] += credit_delta
    player["towing_ship_id"] = None  # a hull swap releases any tow


# --- Space-station stubs ----------------------------------------------
# apply_station_upkeep is a no-op here (just returns the live row): the
# real fuel-burn / upgrade-completion math is exercised against real
# SQLite in db_test. These stubs back the command-flow tests.

def _stub_set_ship_station_core(player_id, has_core):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["station_core"] = 1 if has_core else 0


def _stub_set_ship_cargo(player_id, fuel_ore, organics, equipment):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["fuel_ore"] = fuel_ore
        pl["organics"] = organics
        pl["equipment"] = equipment


def _stub_adjust_player_credits(player_id, delta):
    pl = _player_by_id(player_id)
    if pl is not None:
        pl["credits"] += delta


def _stub_get_station_in_sector(sector_id):
    for st in STATE["stations"].values():
        if st["sector_id"] == sector_id:
            return dict(st)
    return None


def _stub_get_station(station_id):
    st = STATE["stations"].get(station_id)
    return dict(st) if st else None


def _stub_get_stations_by_owner(owner_id):
    return [dict(st) for st in STATE["stations"].values() if st["owner_id"] == owner_id]


def _stub_create_station(owner_id, owner_name, sector_id):
    sid = STATE["next_station_id"]
    STATE["next_station_id"] += 1
    STATE["stations"][sid] = {
        "id": sid, "sector_id": sector_id, "owner_id": owner_id,
        "owner_name": owner_name, "level": 1, "shields": 0, "fighters": 0,
        "shields_enabled": 0, "fuel": 0, "organics": 0, "equipment": 0,
        "posture": "defensive", "engage_pct": 100,
        "last_fuel_burn": "2026-06-27T12:00:00+00:00",
        "upgrade_to": None, "upgrade_started_at": None,
        "credits": 0,
    }
    return dict(STATE["stations"][sid])


def _stub_delete_station(station_id):
    STATE["stations"].pop(station_id, None)
    # Mirror the real delete_station: docked spares go down with it (and
    # any tow reference to one is cleared).
    doomed = [sid for sid, s in STATE["parked_ships"].items()
              if s.get("docked_station_id") == station_id]
    for sid in doomed:
        STATE["parked_ships"].pop(sid, None)
        for pl in [STATE["player"], *STATE["players_by_id"].values()]:
            if pl.get("towing_ship_id") == sid:
                pl["towing_ship_id"] = None


def _stub_deposit_to_station(station_id, fuel=0, organics=0, equipment=0):
    st = STATE["stations"][station_id]
    st["fuel"] += fuel
    st["organics"] += organics
    st["equipment"] += equipment


def _stub_set_station_defenses(station_id, shields, fighters):
    st = STATE["stations"][station_id]
    st["shields"] = shields
    st["fighters"] = fighters


def _stub_set_station_posture(station_id, posture, engage_pct=None):
    st = STATE["stations"][station_id]
    st["posture"] = posture
    if engage_pct is not None:
        st["engage_pct"] = engage_pct


def _stub_set_station_shields(station_id, enabled, shields, last_fuel_burn=None):
    st = STATE["stations"][station_id]
    st["shields_enabled"] = 1 if enabled else 0
    st["shields"] = shields
    if last_fuel_burn is not None:
        st["last_fuel_burn"] = last_fuel_burn


def _stub_apply_station_upkeep(station_id, now=None):
    return _stub_get_station(station_id)


def _stub_start_station_upgrade(station_id, target_level, now=None):
    spec = STATION_UPGRADES[target_level]
    st = STATE["stations"][station_id]
    st["fuel"] -= spec["fuel"]
    st["organics"] -= spec["organics"]
    st["equipment"] -= spec["equipment"]
    st["upgrade_to"] = target_level
    st["upgrade_started_at"] = "2026-06-27T12:00:00+00:00"
    return dict(st)


def _install_stub_modules():
    db_stub = types.ModuleType("db")
    db_stub.init_db = lambda: None
    db_stub.log_message = lambda *a, **k: None
    db_stub.get_or_create_player = _stub_get_or_create_player
    db_stub.reset_turns_if_needed = lambda *a, **k: None
    db_stub.get_player_with_ship = _stub_get_player_with_ship
    db_stub.get_players_in_sector = _stub_get_players_in_sector
    db_stub.get_ships_in_sector = _stub_get_ships_in_sector
    db_stub.record_attack_event = _stub_record_attack_event
    db_stub.pop_attack_events = _stub_pop_attack_events
    db_stub.record_kill = _stub_record_kill
    db_stub.get_kills_since = _stub_get_kills_since
    db_stub.get_kill_log_cutoff = _stub_get_kill_log_cutoff
    db_stub.mark_kill_log_seen = _stub_mark_kill_log_seen
    db_stub.get_adjacent_sectors = _stub_get_adjacent_sectors
    db_stub.get_all_warps = _stub_get_all_warps
    db_stub.get_port = _stub_get_port
    db_stub.move_player_to_sector = _stub_move_player_to_sector
    db_stub.spend_turn = _stub_spend_turn
    db_stub.execute_trade = _stub_execute_trade
    db_stub.apply_port_restock = _stub_apply_port_restock
    db_stub.upgrade_ship_stat = _stub_upgrade_ship_stat
    db_stub.buy_ship = _stub_buy_ship
    db_stub.lay_mines = _stub_lay_mines
    db_stub.get_hostile_mine_total = _stub_get_hostile_mine_total
    db_stub.clear_hostile_mines = _stub_clear_hostile_mines
    db_stub.consume_probe = _stub_consume_probe
    db_stub.detonate_one_hostile_mine = _stub_detonate_one_hostile_mine
    db_stub.set_ship_defenses = _stub_set_ship_defenses
    db_stub.get_parked_ships_in_sector = _stub_get_parked_ships_in_sector
    db_stub.get_ship = _stub_get_ship
    db_stub.set_parked_ship_defenses = _stub_set_parked_ship_defenses
    db_stub.move_ship_to_sector = _stub_move_ship_to_sector
    db_stub.delete_ship = _stub_delete_ship
    db_stub.set_towing = _stub_set_towing
    db_stub.spend_turns = _stub_spend_turns
    db_stub.swap_active_ship = _stub_swap_active_ship
    db_stub.park_and_buy_ship = _stub_park_and_buy_ship
    db_stub.get_ships_docked_at_station = _stub_get_ships_docked_at_station
    db_stub.dock_ship_at_station = _stub_dock_ship_at_station
    db_stub.undock_ship = _stub_undock_ship
    db_stub.station_dock_capacity = station_dock_capacity
    db_stub.DOCKED_SHIPS_PER_LEVEL = DOCKED_SHIPS_PER_LEVEL
    db_stub.transfer_station_credits = _stub_transfer_station_credits
    db_stub.get_last_tx_time = _stub_get_last_tx_time
    db_stub.sell_value = _stub_sell_value
    db_stub.SHIP_CATALOG = SHIP_CATALOG
    db_stub.DEFAULT_SHIP_TYPE = DEFAULT_SHIP_TYPE
    db_stub.ESCAPE_POD_SHIP = ESCAPE_POD_SHIP
    db_stub.HOME_SECTOR = HOME_SECTOR
    db_stub.SAFE_ZONE_MAX_SECTOR = SAFE_ZONE_MAX_SECTOR
    db_stub.MIN_SECTOR_ID = MIN_SECTOR_ID
    db_stub.MAX_SECTOR_ID = MAX_SECTOR_ID
    db_stub.POD_KILL_RESET_CREDITS = POD_KILL_RESET_CREDITS
    db_stub.TOW_TURNS_PER_SECTOR = TOW_TURNS_PER_SECTOR
    db_stub.set_ship_station_core = _stub_set_ship_station_core
    db_stub.set_ship_cargo = _stub_set_ship_cargo
    db_stub.adjust_player_credits = _stub_adjust_player_credits
    db_stub.get_station_in_sector = _stub_get_station_in_sector
    db_stub.get_station = _stub_get_station
    db_stub.get_stations_by_owner = _stub_get_stations_by_owner
    db_stub.create_station = _stub_create_station
    db_stub.delete_station = _stub_delete_station
    db_stub.deposit_to_station = _stub_deposit_to_station
    db_stub.set_station_defenses = _stub_set_station_defenses
    db_stub.set_station_posture = _stub_set_station_posture
    db_stub.set_station_shields = _stub_set_station_shields
    db_stub.apply_station_upkeep = _stub_apply_station_upkeep
    db_stub.start_station_upgrade = _stub_start_station_upgrade
    db_stub.station_caps = station_caps
    db_stub.station_daily_fuel_burn = station_daily_fuel_burn
    db_stub.STATION_CORE_PRICE = STATION_CORE_PRICE
    db_stub.STATION_CORE_HOLDS = STATION_CORE_HOLDS
    db_stub.STATION_MAX_LEVEL = STATION_MAX_LEVEL
    db_stub.STATION_LEVEL_CAPS = STATION_LEVEL_CAPS
    db_stub.STATION_UPGRADES = STATION_UPGRADES
    db_stub.SHIELD_FUEL_BURN_PER_SHIELD = SHIELD_FUEL_BURN_PER_SHIELD
    db_stub.STARDOCK_PRICES = {"holds_total": 500, "fighters": 50, "shields": 25, "mines": 1000, "probes": 100}
    sys.modules["db"] = db_stub

    meshcore_stub = types.ModuleType("meshcore")

    class MeshCore:
        pass

    class EventType:
        ERROR = "ERROR"

    meshcore_stub.MeshCore = MeshCore
    meshcore_stub.EventType = EventType
    sys.modules["meshcore"] = meshcore_stub


_install_stub_modules()
import main  # noqa: E402  (must come after the stubs are installed)
import attack  # noqa: E402  (rng seam for combat knockback -- attack.random)
import movement  # noqa: E402  (rng seam for mines/pod drift -- movement.random)


class FakeCtx:
    """Minimal stand-in for main.Ctx -- only the attributes cmd_trade and
    cmd_trade_step actually read."""

    def __init__(self, pubkey, player):
        self.pubkey = pubkey
        self.sender = "Tester"
        self.player = player


PUBKEY = "test-pubkey"


def fresh_player(**overrides):
    base = {
        "id": 1,
        "name": "Tester",
        "sector_id": 1,
        "credits": 10000,
        "turns_remaining": 50,
        "ship_type": "Falcon",
        "holds_total": 20,
        "fighters": 0,
        "shields": 0,
        "mines": 0,
        "probes": 0,
        "fuel_ore": 0,
        "organics": 0,
        "equipment": 0,
        "station_core": 0,
        "ship_id": 50,          # the active ship's row id
        "towing_ship_id": None,
    }
    base.update(overrides)
    return base


def fresh_port(port_class, **overrides):
    base = {"id": 1, "port_class": port_class}
    for key in ("fuel_ore", "organics", "equipment"):
        base[f"{key}_dir"] = None
        base[f"{key}_price"] = 0
        base[f"{key}_qty"] = 0
        base[f"{key}_max"] = 0
    base.update(overrides)
    return base


class PortTradeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reload main.py so module-level state (PENDING_TRADES, etc.)
        # starts empty for every test, independent of test order. Its
        # one-time startup print() fires again on every reload, so it's
        # muted here to keep test output clean.
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        """A fresh Ctx wired to whatever STATE['player'] currently holds --
        mirrors how on_message re-fetches the player on every turn."""
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_trade_step(self.ctx(), message)

    async def test_sell_then_buy_fills_holds_in_order(self):
        """The worked example: sell Equipment, then buy Fuel Ore, then
        buy Organics, using the cargo space freed up by each sale."""
        STATE["player"] = fresh_player(equipment=15, holds_total=20, credits=10000)
        STATE["port"] = fresh_port(
            "SSB",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        # 1. Docking offers to sell the 15 carried Equipment first.
        prompt = await self.dock()
        self.assertIn("Equipment", prompt)
        self.assertIn("Sell how many? (0-15", prompt)

        # 2. Sell 5 of them at the listed price.
        prompt = await self.say("5")
        self.assertIn("Sell 5 Equipment for 100cr (20cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Sold 5 Equipment for +100cr (20cr/unit).", prompt)

        # 3. Selling freed 5 holds (5 already free + 5 just freed = 10),
        # so the next offer is to buy up to 10 Fuel Ore.
        self.assertIn("Buy how many Fuel Ore? (0-10", prompt)
        prompt = await self.say("5")
        self.assertIn("Buy 5 Fuel Ore for 50cr (10cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Bought 5 Fuel Ore for -50cr (10cr/unit).", prompt)

        # 4. 5 holds remain free, so the Organics offer is capped at 5.
        self.assertIn("Buy how many Organics? (0-5", prompt)
        prompt = await self.say("5")
        self.assertIn("Buy 5 Organics for 75cr (15cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Bought 5 Organics for -75cr (15cr/unit).", prompt)

        # 5. Holds are now full and nothing else is queued -- the visit
        # closes itself out without the player needing to say 'cancel'.
        self.assertIn("Nothing more to trade here.", prompt)

        final = STATE["player"]
        self.assertEqual(final["equipment"], 10)   # 15 - 5 sold
        self.assertEqual(final["fuel_ore"], 5)
        self.assertEqual(final["organics"], 5)
        self.assertEqual(final["credits"], 10000 + 100 - 50 - 75)
        self.assertEqual(
            STATE["trade_log"],
            [
                ("equipment", 5, 100, False),
                ("fuel_ore", 5, 50, True),
                ("organics", 5, 75, True),
            ],
        )
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)  # session cleaned up

    async def test_quantity_zero_skips_to_next_item(self):
        """Empty holds at an all-selling (SSS) port: replying 0 to an
        offer moves straight to the next one instead of ending the visit."""
        STATE["player"] = fresh_player(holds_total=20, credits=200)
        STATE["port"] = fresh_port(
            "SSS",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="S", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        prompt = await self.dock()
        self.assertIn("Fuel Ore", prompt)

        prompt = await self.say("0")   # skip fuel
        self.assertIn("Organics", prompt)

        prompt = await self.say("0")   # skip organics
        self.assertIn("Equipment", prompt)

        prompt = await self.say("2")
        prompt = await self.say("yes")
        self.assertIn("Bought 2 Equipment for -40cr (20cr/unit).", prompt)
        self.assertIn("Nothing more to trade here.", prompt)

        self.assertEqual(STATE["player"]["fuel_ore"], 0)
        self.assertEqual(STATE["player"]["organics"], 0)
        self.assertEqual(STATE["player"]["equipment"], 2)

    async def test_no_at_confirm_skips_rather_than_cancels(self):
        """'no' at the price-confirm step behaves like a quantity of 0 --
        it moves on to the next queued item, not the whole port visit."""
        STATE["player"] = fresh_player(equipment=4, holds_total=10, credits=1000)
        STATE["port"] = fresh_port(
            "BSS",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        await self.dock()                 # offers to sell Equipment
        await self.say("4")               # quote a sale of all 4 units
        prompt = await self.say("no")     # decline -- should move on, not cancel

        self.assertIn("Fuel Ore", prompt)
        self.assertIn(PUBKEY, main.PENDING_TRADES)  # visit is still active
        self.assertEqual(STATE["player"]["equipment"], 4)  # nothing sold

    async def test_cancel_ends_the_whole_visit(self):
        STATE["player"] = fresh_player(equipment=15, holds_total=20, credits=10000)
        STATE["port"] = fresh_port(
            "SSB",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        await self.dock()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Trade cancelled.")
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(STATE["player"]["equipment"], 15)  # unchanged
        self.assertEqual(STATE["trade_log"], [])


class StardockRefitFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the Stardock refit flow added to cmd_trade/cmd_stardock_step:
    docking at a STARDOCK port opens an open-ended menu (buy cargo holds,
    fighters, or shields) rather than the commodity-trade queue, and the
    menu reappears after every purchase/skip so multiple stats can be
    upgraded in one visit.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def test_dock_shows_refit_menu_with_current_max_and_price(self):
        STATE["player"] = fresh_player(credits=5000, holds_total=20, fighters=10, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertIn("Stardock refits:", prompt)
        self.assertIn("Cargo Holds 20/75 @ 500cr each", prompt)
        self.assertIn("Fighters 10/50 @ 50cr each", prompt)
        self.assertIn("Shields 10/200 @ 25cr each", prompt)
        self.assertIn("5000cr available", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_buy_fighters_full_flow_then_returns_to_menu(self):
        STATE["player"] = fresh_player(credits=5000, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()

        # Choose option 2 (Fighters). Room to cap is 40 (50-10) and
        # affordability is 100 (5000/50cr) -- room is the binding limit.
        prompt = await self.say("2")
        self.assertIn("Fighters: 10/50, 50cr each.", prompt)
        self.assertIn("Buy how many? (0-40, or 'cancel')", prompt)

        prompt = await self.say("10")
        self.assertIn("Buy 10 Fighters for 500cr (50cr/unit)? yes/no", prompt)

        prompt = await self.say("yes")
        self.assertIn("Installed 10 Fighters for -500cr.", prompt)
        # Back at the menu, reflecting the purchase, ready for another.
        self.assertIn("Stardock refits:", prompt)
        self.assertIn("Fighters 20/50 @ 50cr each", prompt)

        final = STATE["player"]
        self.assertEqual(final["fighters"], 20)
        self.assertEqual(final["credits"], 5000 - 500)
        self.assertEqual(STATE["upgrade_log"], [("fighters", 10, 500)])
        # Visit stays open after a purchase, so more can be bought.
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_quantity_capped_by_affordability_not_just_ship_cap(self):
        """100cr only buys 2 fighters at 50cr each, even though there's
        plenty of room left under the 50-fighter cap."""
        STATE["player"] = fresh_player(credits=100, fighters=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("2")  # Fighters

        self.assertIn("Buy how many? (0-2, or 'cancel')", prompt)

    async def test_quantity_above_max_is_rejected(self):
        STATE["player"] = fresh_player(credits=5000, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("3")  # Shields: room=190, afford=200 -> max 190
        prompt = await self.say("9999")

        self.assertIn("Max is 190. Enter a smaller quantity, or 'cancel'.", prompt)
        self.assertEqual(STATE["player"]["shields"], 10)  # nothing bought yet

    async def test_quantity_zero_returns_to_menu_without_buying(self):
        STATE["player"] = fresh_player(credits=5000, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("3")  # Shields
        prompt = await self.say("0")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(STATE["player"]["shields"], 10)
        self.assertEqual(STATE["upgrade_log"], [])
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)  # visit still open

    async def test_no_at_confirm_returns_to_menu_without_buying(self):
        STATE["player"] = fresh_player(credits=5000, holds_total=20)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("1")    # Cargo Holds
        await self.say("5")    # quote 5 holds
        prompt = await self.say("no")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(STATE["player"]["holds_total"], 20)  # unchanged
        self.assertEqual(STATE["upgrade_log"], [])
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_cancel_ends_the_visit_from_the_menu(self):
        STATE["player"] = fresh_player(credits=5000)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_cancel_ends_the_visit_mid_purchase(self):
        """'cancel' works at the quantity/confirm stages too, not just
        from the top-level menu."""
        STATE["player"] = fresh_player(credits=5000, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("2")     # Fighters
        await self.say("5")     # quote 5
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(STATE["player"]["fighters"], 10)  # unchanged
        self.assertEqual(STATE["upgrade_log"], [])

    async def test_stat_already_at_cap_is_rejected(self):
        STATE["player"] = fresh_player(credits=100000, holds_total=75)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("1")  # Cargo Holds, already at the 75 cap

        self.assertIn("Already at max Cargo Holds (75).", prompt)
        self.assertIn("Stardock refits:", prompt)  # re-shows the menu
        # Still on the menu stage -- the rejected pick never advanced state.
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_cant_afford_even_one_unit_is_rejected(self):
        STATE["player"] = fresh_player(credits=10, fighters=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("2")  # Fighters @ 50cr each, only 10cr on hand

        self.assertIn("Can't afford even 1 Fighters (50cr each).", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_invalid_menu_choice_is_rejected(self):
        STATE["player"] = fresh_player(credits=5000)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()

        prompt = await self.say("9")  # out of range -- only 3 options exist
        self.assertIn("Not a valid option.", prompt)

        prompt = await self.say("not-a-number")
        self.assertIn("Reply with a number, or 'cancel'.", prompt)

    async def test_multiple_purchases_in_one_visit(self):
        """Buying holds, then fighters, in the same visit -- the menu
        loop should let a player upgrade more than one stat per dock."""
        STATE["player"] = fresh_player(credits=5000, holds_total=20, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("1")     # Cargo Holds
        await self.say("2")     # buy 2 holds @ 500cr = 1000cr
        await self.say("yes")
        await self.say("2")     # Fighters
        await self.say("4")     # buy 4 fighters @ 50cr = 200cr
        prompt = await self.say("yes")

        final = STATE["player"]
        self.assertEqual(final["holds_total"], 22)
        self.assertEqual(final["fighters"], 14)
        self.assertEqual(final["credits"], 5000 - 1000 - 200)
        self.assertEqual(
            STATE["upgrade_log"],
            [("holds_total", 2, 1000), ("fighters", 4, 200)],
        )
        self.assertIn("Stardock refits:", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)  # visit still open


class NavigationDockingFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers docking partway through a multi-hop plotted course (the
    cmd_confirm_warp 'p'/'port' branch, and the _resume_navigation_suffix
    helper it relies on). Scenario mirrors the worked example from the
    design discussion: starting in Sec1, with Sec2 and Sec3 adjacent but
    Sec4 two hops away via Sec3, a player routes to Sec4, gets routed
    through Sec3 first, and docks there before continuing on.

    Warp graph used throughout: 1<->2, 1<->3, 3<->4 (so Sec4 is only
    reachable via Sec3, making the BFS route deterministic).
    """

    WARPS = {1: [2, 3], 2: [1], 3: [1, 4], 4: [3]}

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = dict(self.WARPS)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def plot_route_to_4(self):
        """Sec1 -> Sec4 isn't a direct warp, so this always plots the
        2-hop Sec1->Sec3->Sec4 route and leaves it awaiting confirmation."""
        return await main.cmd_move(self.ctx(), "4")

    async def test_docking_at_intermediate_sector_then_resuming_route(self):
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {
            3: fresh_port(
                "SSS",
                fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            )
        }

        # 1. Plot the route -- Sec4 isn't adjacent to Sec1, so this goes
        # through the BFS/confirmation path rather than moving directly.
        prompt = await self.plot_route_to_4()
        self.assertIn("Plotted a 2-warp course to Sec4.", prompt)
        self.assertIn("Warp to: 1 -> 3 -> 4? (yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [3, 4])

        # 2. Confirm the first hop -- arrives at Sec3, which has a port,
        # and is asked to confirm the next (final) hop to Sec4.
        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 3)
        self.assertIn("Warped to Sec3.", prompt)
        self.assertIn("Port: SSS", prompt)
        self.assertIn("Warps: 1, 4", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        # 3. Instead of yes/no, dock at Sec3's port -- the route to Sec4
        # must stay queued while this happens.
        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("Fuel Ore", prompt)
        self.assertIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # untouched

        # 4. Buy some fuel ore -- the only item in this port's queue, so
        # the visit closes itself out, and the dropped-back-into route
        # confirmation should be appended automatically.
        await main.cmd_trade_step(self.ctx(), "5")
        prompt = await main.cmd_trade_step(self.ctx(), "yes")
        self.assertIn("Bought 5 Fuel Ore for -50cr (10cr/unit).", prompt)
        self.assertIn("Nothing more to trade here.", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # route still open

        # 5. Pick the route back up -- arrives at the original Sec4
        # destination and the route is finally cleared.
        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 4)
        self.assertIn("Arrived at Sec4.", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_cancelling_the_dock_mid_route_resumes_navigation_prompt(self):
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {
            3: fresh_port(
                "SSS",
                fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            )
        }

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3
        await main.cmd_confirm_warp(self.ctx(), "p")    # dock

        prompt = await main.cmd_trade_step(self.ctx(), "cancel")

        self.assertEqual(prompt, "Trade cancelled.\n\nWarp to: 3 -> 4? (p/yes/no)")
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

    async def test_docking_with_nothing_to_trade_reshows_prompt_immediately(self):
        """If the port at the stopover has nothing to offer, no trade
        visit actually starts -- the route prompt should still come
        back right away rather than leaving the player stuck."""
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {3: fresh_port("SSS")}  # no commodity directions set

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3

        prompt = await main.cmd_confirm_warp(self.ctx(), "port")

        self.assertIn("Nothing to trade with this port.", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

    async def test_docking_at_a_stardock_stopover_then_resuming_route(self):
        """Same idea, but the stopover is a Stardock -- refit purchases
        should be able to happen mid-route too, with the same resume
        behavior once the visit is left via 'cancel'."""
        STATE["player"] = fresh_player(sector_id=1, credits=5000, fighters=10)
        STATE["ports"] = {3: fresh_port("STARDOCK")}

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3

        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("Stardock refits:", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # untouched

        await main.cmd_stardock_step(self.ctx(), "2")   # Fighters
        await main.cmd_stardock_step(self.ctx(), "5")   # qty
        prompt = await main.cmd_stardock_step(self.ctx(), "yes")
        self.assertIn("Installed 5 Fighters for -250cr.", prompt)
        self.assertIn("Stardock refits:", prompt)  # visit stays open
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        prompt = await main.cmd_stardock_step(self.ctx(), "cancel")
        self.assertEqual(prompt, "Left the Stardock.\n\nWarp to: 3 -> 4? (p/yes/no)")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 4)
        self.assertIn("Arrived at Sec4.", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_no_port_at_stopover_omits_p_from_the_prompt(self):
        """The 'p' option should only show up when there's actually a
        port to dock at -- with none at either Sec1 or Sec3, every
        prompt along the route stays plain (yes/no)."""
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {}  # no port anywhere on the route

        prompt = await self.plot_route_to_4()
        self.assertIn("Warp to: 1 -> 3 -> 4? (yes/no)", prompt)
        self.assertNotIn("(p/yes/no)", prompt)

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3
        self.assertIn("Warp to: 3 -> 4? (yes/no)", prompt)
        self.assertNotIn("(p/yes/no)", prompt)

        # 'p' is still a no-op here, not a route-breaking error -- it's
        # just that cmd_trade has nothing to dock at.
        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("No port in current sector.", prompt)
        self.assertIn("Warp to: 3 -> 4? (yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])


class ShipyardFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the shipyard sub-menu added to the Stardock visit
    (cmd_stardock_step's "shipyard_menu"/"shipyard_confirm" stages):
    buying a different hull (with the current one automatically traded
    in, since a player can only ever have one ship) or selling the
    current hull back to the free default Falcon.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def enter_shipyard(self):
        await self.dock()
        # Shipyard is always one past the refit options. For a Falcon
        # that's now Holds/Fighters/Shields/Probes -> Shipyard at 5.
        return await self.say("5")

    async def test_shipyard_entry_shows_catalog_with_current_ship_tagged(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.enter_shipyard()

        self.assertIn("Shipyard:", prompt)
        self.assertIn(
            "1) Falcon (Frigate): 75 holds / 50 fighters / 200 shields -- (current ship)",
            prompt,
        )
        self.assertIn(
            "2) Kestrel (Corvette): 40 holds / 120 fighters / 150 shields -- 8000cr",
            prompt,
        )
        self.assertIn(
            "6) SS Endeavour (Merchant Freighter): 200 holds / 10 fighters / 400 shields -- 200000cr",
            prompt,
        )
        # Any hull with a mine bay shows mine capacity, not just the Bismark.
        self.assertIn(
            "5) Nautilus (Minelayer): 80 holds / 400 fighters / 1000 shields / 150 mines -- 180000cr",
            prompt,
        )
        self.assertIn(
            "9) Bismark (Capital Ship): 125 holds / 2000 fighters / 3500 shields / 50 mines -- 500000cr",
            prompt,
        )
        # Flying the free default ship -- nothing to trade in, so no sell line.
        self.assertNotIn("Sell your", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_buying_the_bismark_full_flow(self):
        STATE["player"] = fresh_player(credits=600000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("9")  # Bismark
        self.assertIn(
            "Buying a Bismark (500000cr). Sell your Falcon as trade-in (+0cr, net "
            "500000cr), or keep it parked here in Sec1 (full 500000cr)? "
            "Reply sell/keep (or cancel).",
            prompt,
        )

        prompt = await self.say("sell")
        self.assertIn(
            "Trade in your Falcon (0cr) for a Bismark (500000cr)? Net cost: 500000cr. yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Welcome aboard the Bismark! -500000cr (net).", prompt)
        # Back at the top-level menu, now with Mines and Probes refit
        # options, so Shipyard sits at #6 (Falcon only reached #5).
        self.assertIn("Cargo Holds 30/125 @ 500cr each", prompt)
        self.assertIn("Fighters 200/2000 @ 50cr each", prompt)
        self.assertIn("Shields 500/3500 @ 25cr each", prompt)
        self.assertIn("Mines 0/50 @ 1000cr each", prompt)
        self.assertIn("Probes 0/20 @ 100cr each", prompt)
        self.assertIn("6) Shipyard", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Bismark")
        self.assertEqual(final["holds_total"], 30)
        self.assertEqual(final["fighters"], 200)
        self.assertEqual(final["shields"], 500)
        self.assertEqual(final["mines"], 0)
        self.assertEqual(final["credits"], 600000 - 500000)
        self.assertEqual(STATE["ship_log"], [("Bismark", 30, 200, 500, 0, -500000)])

    async def test_buying_a_new_ship_full_flow(self):
        STATE["player"] = fresh_player(credits=250000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("6")  # SS Endeavour -> the sell-or-keep question
        prompt = await self.say("sell")
        self.assertIn(
            "Trade in your Falcon (0cr) for a SS Endeavour (200000cr)? Net cost: 200000cr. yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Welcome aboard the SS Endeavour! -200000cr (net).", prompt)
        self.assertIn("Stardock refits:", prompt)  # back at the top-level menu
        self.assertIn("Cargo Holds 50/200 @ 500cr each", prompt)  # new ship's caps

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "SS Endeavour")
        self.assertEqual(final["holds_total"], 50)
        self.assertEqual(final["fighters"], 0)
        self.assertEqual(final["shields"], 50)
        self.assertEqual(final["credits"], 250000 - 200000)
        self.assertEqual(STATE["ship_log"], [("SS Endeavour", 50, 0, 50, 0, -200000)])
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")  # visit stays open

    async def test_selling_current_ship_returns_to_falcon(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="SS Endeavour",
                                        holds_total=50, fighters=0, shields=50)
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("s")
        self.assertIn(
            "Sell your SS Endeavour and return to the Falcon for 100000cr? yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Sold your old ship. Welcome back to the Falcon. +100000cr.", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Falcon")
        self.assertEqual(final["holds_total"], 20)
        self.assertEqual(final["fighters"], 10)
        self.assertEqual(final["shields"], 10)
        self.assertEqual(final["credits"], 5000 + 100000)
        self.assertEqual(STATE["ship_log"], [("Falcon", 20, 10, 10, 0, 100000)])

    async def test_ship_swap_clears_cargo(self):
        """Cargo doesn't transfer between hulls -- swapping (buy or
        sell) empties whatever was in the hold."""
        STATE["player"] = fresh_player(
            credits=250000, ship_type="Falcon",
            fuel_ore=5, organics=3, equipment=2,
        )
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("6")     # SS Endeavour
        await self.say("sell")  # trade the Falcon in
        await self.say("yes")

        final = STATE["player"]
        self.assertEqual(final["fuel_ore"], 0)
        self.assertEqual(final["organics"], 0)
        self.assertEqual(final["equipment"], 0)

    async def test_cannot_sell_while_flying_the_falcon(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("sell")

        self.assertIn("You're already flying the Falcon -- nothing to trade in.", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")  # unchanged

    async def test_cannot_buy_a_ship_already_owned(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="SS Endeavour")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("6")  # SS Endeavour, which they already fly

        self.assertIn("You already own the SS Endeavour.", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_cannot_afford_ship_even_after_trade_in(self):
        STATE["player"] = fresh_player(credits=100, ship_type="Falcon")  # trade-in is 0cr
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("6")  # SS Endeavour @ 200000cr net

        self.assertIn(
            "Can't afford the SS Endeavour -- net cost 200000cr (200000cr less a 0cr trade-in), "
            "you have 100cr.",
            prompt,
        )
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")
        self.assertEqual(STATE["ship_log"], [])

    async def test_declining_purchase_confirm_returns_to_shipyard_menu_unchanged(self):
        STATE["player"] = fresh_player(credits=250000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("6")
        await self.say("sell")
        prompt = await self.say("no")

        self.assertIn("Shipyard:", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")
        self.assertEqual(STATE["ship_log"], [])
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_zero_returns_to_the_main_stardock_menu(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("0")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_cancel_from_shipyard_ends_the_whole_visit(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)


class MinesRefitTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the Mines refit stat and its per-ship visibility: only a hull
    with a mine bay (max_mines > 0 -- the Bismark, Barracuda, Nautilus,
    and Vanguard) offers it in the Stardock menu. Hulls with none (e.g.
    Falcon/SS Endeavour) don't, so the menu -- and the Shipyard option's
    numbering -- should never mention it for them.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def test_mines_hidden_for_ships_without_a_mine_bay(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertNotIn("Mines", prompt)
        self.assertIn("3) Shields", prompt)
        self.assertIn("4) Probes 0/10 @ 100cr each", prompt)  # universal, fills the Mines gap
        self.assertIn("5) Shipyard", prompt)  # right after the 4 refits

    async def test_mines_appears_for_the_bismark_and_shifts_shipyard(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=0)
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertIn("4) Mines 0/50 @ 1000cr each", prompt)
        self.assertIn("5) Probes 0/20 @ 100cr each", prompt)
        self.assertIn("6) Shipyard", prompt)

    async def test_buy_mines_full_flow(self):
        STATE["player"] = fresh_player(credits=10000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines
        self.assertIn("Mines: 0/50, 1000cr each.", prompt)
        self.assertIn("Buy how many? (0-10, or 'cancel')", prompt)  # 10000cr / 1000cr each

        prompt = await self.say("5")
        self.assertIn("Buy 5 Mines for 5000cr (1000cr/unit)? yes/no", prompt)

        prompt = await self.say("yes")
        self.assertIn("Installed 5 Mines for -5000cr.", prompt)
        self.assertIn("Mines 5/50 @ 1000cr each", prompt)

        final = STATE["player"]
        self.assertEqual(final["mines"], 5)
        self.assertEqual(final["credits"], 10000 - 5000)
        self.assertEqual(STATE["upgrade_log"], [("mines", 5, 5000)])

    async def test_mines_capped_at_ship_max(self):
        STATE["player"] = fresh_player(credits=1000000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=48)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines: only 2 units of room left under the 50 cap

        self.assertIn("Buy how many? (0-2, or 'cancel')", prompt)

    async def test_mines_already_at_cap_is_rejected(self):
        STATE["player"] = fresh_player(credits=1000000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=50)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines, already at the 50 cap

        self.assertIn("Already at max Mines (50).", prompt)


class FakeRandom:
    """Deterministic stand-in for the `random` module the feature modules
    use. `randints` is a queue popped by randint() (falling back to the
    high end once exhausted); choice() returns the element at
    `choice_index`. Install per-test with `movement.random = FakeRandom(...)`
    (mine damage, pod drift) or `attack.random = FakeRandom(...)` (combat
    knockback) after setUp's reload(main) has restored the real module in
    both."""

    def __init__(self, randints=None, choice_index=0):
        self.randints = list(randints or [])
        self.choice_index = choice_index

    def randint(self, a, b):
        return self.randints.pop(0) if self.randints else b

    def choice(self, seq):
        seq = list(seq)
        return seq[self.choice_index % len(seq)]


def chain_warps(n):
    """A simple line graph 1-2-3-...-n, so hop distance equals the
    difference in sector numbers -- handy for asserting escape-pod
    distance ranges precisely."""
    warps = {}
    for i in range(1, n + 1):
        nbrs = []
        if i > 1:
            nbrs.append(i - 1)
        if i < n:
            nbrs.append(i + 1)
        warps[i] = nbrs
    return warps


class MineDamageMathTests(unittest.TestCase):
    """Pure unit tests for apply_mine_damage -- the shields -> fighters ->
    hull cascade, with no db or async in the way."""

    def test_shields_absorb_one_for_one(self):
        # 9 damage, 20 shields: shields take it all, fighters untouched.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(20, 5, 9)
        self.assertEqual((s_after, f_after), (11, 5))
        self.assertEqual((s_lost, f_lost), (9, 0))
        self.assertFalse(destroyed)

    def test_overflow_spills_into_fighters_at_two_per(self):
        # 5 shields gone, 5 damage left -> 3 fighters (ceil(5/2)) lost.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(5, 10, 10)
        self.assertEqual(s_after, 0)
        self.assertEqual(s_lost, 5)
        self.assertEqual(f_lost, 3)        # ceil(5 / 2)
        self.assertEqual(f_after, 7)
        self.assertFalse(destroyed)

    def test_exact_absorption_survives_at_zero_zero(self):
        # 2 shields + 2 fighters (worth 4) = 6 capacity vs 5 damage: the
        # ship is stripped to 0/0 but NOT destroyed -- damage ran out
        # before the defenses did.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(2, 2, 5)
        self.assertEqual((s_after, f_after), (0, 0))
        self.assertFalse(destroyed)

    def test_destroyed_when_damage_outlasts_both(self):
        # 2 shields + 1 fighter (worth 2) = 4 capacity vs 5 damage: 1
        # point punches through with nothing left -> destroyed.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(2, 1, 5)
        self.assertEqual((s_after, f_after), (0, 0))
        self.assertTrue(destroyed)

    def test_no_defenses_any_damage_destroys(self):
        _, _, _, _, destroyed = main.apply_mine_damage(0, 0, 1)
        self.assertTrue(destroyed)

    def test_zero_damage_is_harmless(self):
        s_after, f_after, _, _, destroyed = main.apply_mine_damage(7, 3, 0)
        self.assertEqual((s_after, f_after), (7, 3))
        self.assertFalse(destroyed)


class EscapeSectorTests(unittest.TestCase):
    """choose_escape_sector / sectors_within_hop_range against a known
    line graph where hop distance is exact."""

    def test_picks_a_sector_in_the_4_to_6_hop_band(self):
        graph = chain_warps(40)
        for _ in range(20):
            dest = main.choose_escape_sector(graph, 20)  # real randomness
            self.assertIn(dest, {14, 15, 16, 24, 25, 26})

    def test_falls_back_to_farthest_when_band_is_empty(self):
        # A 3-sector line: from the end, nothing is 4-6 hops out, so it
        # falls back to the farthest reachable sector.
        graph = chain_warps(3)
        self.assertEqual(main.choose_escape_sector(graph, 1), 3)

    def test_returns_none_when_nowhere_to_go(self):
        self.assertIsNone(main.choose_escape_sector({5: []}, 5))


class LayMinesCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["mine_log"] = []
        STATE["defense_log"] = []
        STATE["sector_mines"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def lay(self, args):
        return await main.cmd_lay_mines(self.ctx(), args)

    async def test_lay_mines_deploys_and_decrements_aboard(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=10)
        prompt = await self.lay("4")
        self.assertIn("Laid 4 mines in Sec42; 6 still aboard.", prompt)
        self.assertEqual(STATE["player"]["mines"], 6)
        self.assertEqual(STATE["mine_log"], [(42, 1, 4)])
        self.assertEqual(STATE["sector_mines"][42], {1: 4})

    async def test_laying_again_accumulates_in_the_same_sector(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=10)
        await self.lay("4")
        STATE["player"]["mines"] = 6  # mirror the decrement for the 2nd ctx
        await self.lay("2")
        self.assertEqual(STATE["sector_mines"][42], {1: 6})

    async def test_safe_zone_sectors_reject_laying(self):
        STATE["player"] = fresh_player(sector_id=10, ship_type="Bismark", mines=10)
        prompt = await self.lay("1")
        self.assertIn("safe zone", prompt)
        self.assertEqual(STATE["mine_log"], [])         # nothing deployed
        self.assertEqual(STATE["player"]["mines"], 10)  # nothing spent

    async def test_first_sector_outside_safe_zone_is_allowed(self):
        STATE["player"] = fresh_player(sector_id=11, ship_type="Bismark", mines=5)
        prompt = await self.lay("1")
        self.assertIn("Laid 1 mine in Sec11", prompt)

    async def test_no_mines_aboard_is_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Falcon", mines=0)
        prompt = await self.lay("3")
        self.assertIn("No mines aboard", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_missing_count_prompts_for_one(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=5)
        prompt = await self.lay("")
        self.assertIn("Lay how many mines?", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_more_than_aboard_is_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=3)
        prompt = await self.lay("4")
        self.assertIn("only have 3 mines aboard", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_non_numeric_and_zero_are_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=5)
        self.assertIn("whole number", await self.lay("lots"))
        self.assertIn("from 1 up", await self.lay("0"))
        self.assertEqual(STATE["mine_log"], [])


class JettisonCommandTests(unittest.IsolatedAsyncioTestCase):
    """The 'jettison' command: dumping commodity cargo overboard in its
    several forms (all / one commodity / a specific amount), the
    no-accidental-dump bare form, and the rejection paths."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def jettison(self, args):
        return await main.cmd_jettison(self.ctx(), args)

    def _cargo(self):
        return (
            STATE["player"]["fuel_ore"],
            STATE["player"]["organics"],
            STATE["player"]["equipment"],
        )

    async def test_jettison_all_clears_every_hold(self):
        STATE["player"] = fresh_player(fuel_ore=10, organics=5, equipment=3)
        msg = await self.jettison("all")
        self.assertIn("Jettisoned all cargo", msg)
        # The summary lists what went overboard, in order, non-zero only.
        self.assertIn("10 fuel ore", msg)
        self.assertIn("5 organics", msg)
        self.assertIn("3 equipment", msg)
        self.assertEqual(self._cargo(), (0, 0, 0))

    async def test_jettison_all_omits_empty_commodities_from_summary(self):
        STATE["player"] = fresh_player(fuel_ore=4, organics=0, equipment=0)
        msg = await self.jettison("all")
        self.assertIn("4 fuel ore", msg)
        self.assertNotIn("organics", msg)   # zero holds aren't listed
        self.assertEqual(self._cargo(), (0, 0, 0))

    async def test_jettison_single_commodity_dumps_all_of_it(self):
        STATE["player"] = fresh_player(fuel_ore=10, organics=5, equipment=3)
        msg = await self.jettison("organics")
        self.assertIn("Jettisoned 5 organics into space; 0 still aboard.", msg)
        self.assertEqual(self._cargo(), (10, 0, 3))   # only organics cleared

    async def test_jettison_partial_amount_leaves_the_rest(self):
        STATE["player"] = fresh_player(equipment=8)
        msg = await self.jettison("equipment 3")
        self.assertIn("Jettisoned 3 equipment into space; 5 still aboard.", msg)
        self.assertEqual(self._cargo(), (0, 0, 5))

    async def test_commodity_aliases_resolve(self):
        STATE["player"] = fresh_player(fuel_ore=4)
        msg = await self.jettison("fuel")        # 'fuel' -> fuel_ore
        self.assertIn("Jettisoned 4 fuel ore", msg)
        self.assertEqual(self._cargo(), (0, 0, 0))

    async def test_bare_jettison_shows_manifest_and_dumps_nothing(self):
        STATE["player"] = fresh_player(fuel_ore=2, organics=1, equipment=0)
        msg = await self.jettison("")
        self.assertIn("Aboard:", msg)
        self.assertIn("fuel ore 2", msg)
        self.assertIn("equipment 0", msg)        # manifest shows zeros too
        self.assertEqual(self._cargo(), (2, 1, 0))   # nothing spaced

    async def test_bare_jettison_with_empty_holds(self):
        STATE["player"] = fresh_player()
        self.assertIn("holds are empty", await self.jettison(""))

    async def test_jettison_all_with_empty_holds(self):
        STATE["player"] = fresh_player()
        self.assertIn("holds are empty", await self.jettison("all"))
        self.assertEqual(self._cargo(), (0, 0, 0))

    async def test_jettison_commodity_not_aboard(self):
        STATE["player"] = fresh_player(fuel_ore=5)
        msg = await self.jettison("organics")
        self.assertIn("No organics aboard", msg)
        self.assertEqual(self._cargo(), (5, 0, 0))   # untouched

    async def test_more_than_aboard_is_rejected(self):
        STATE["player"] = fresh_player(fuel_ore=3)
        msg = await self.jettison("fuel 5")
        self.assertIn("only have 3 fuel ore aboard", msg)
        self.assertEqual(self._cargo(), (3, 0, 0))

    async def test_non_numeric_amount_is_rejected(self):
        STATE["player"] = fresh_player(fuel_ore=3)
        msg = await self.jettison("fuel lots")
        self.assertIn("whole number", msg)
        self.assertEqual(self._cargo(), (3, 0, 0))

    async def test_zero_amount_is_rejected(self):
        STATE["player"] = fresh_player(fuel_ore=3)
        msg = await self.jettison("fuel 0")
        self.assertIn("from 1 up", msg)
        self.assertEqual(self._cargo(), (3, 0, 0))

    async def test_unknown_commodity_is_rejected(self):
        STATE["player"] = fresh_player(fuel_ore=3)
        msg = await self.jettison("widgets")
        self.assertIn("fuel/organics/equipment", msg)
        self.assertEqual(self._cargo(), (3, 0, 0))

    async def test_jettison_does_not_touch_a_station_core_kit(self):
        # The kit is a separate fixture, not commodity cargo -- 'all' clears
        # the holds but leaves station_core set.
        STATE["player"] = fresh_player(fuel_ore=4, station_core=1)
        await self.jettison("all")
        self.assertEqual(self._cargo(), (0, 0, 0))
        self.assertEqual(STATE["player"]["station_core"], 1)


def _p2p_port(sector, pid, klass, spec):
    """Build a port fixture for p2p tests. `spec` maps each commodity to
    (direction, price). A selling commodity ('S') starts fully stocked; a
    buying commodity ('B') starts empty with plenty of room."""
    port = fresh_port(klass, id=pid)
    port["sector_id"] = sector
    for key, (direction, price) in spec.items():
        port[f"{key}_dir"] = direction
        port[f"{key}_price"] = price
        port[f"{key}_max"] = 10000
        port[f"{key}_qty"] = 10000 if direction == "S" else 0
    return port


# Sec100 BSS  <->  Sec101 SBB, priced with a real buy/sell spread so a
# round trip nets a profit: organics bought at 100 @160 and sold at 101
# @200 (+40/unit); fuel bought at 101 @90 and sold at 100 @100 (+10/unit).
_BSS_100 = {"fuel_ore": ("B", 100), "organics": ("S", 160), "equipment": ("S", 260)}
_SBB_101 = {"fuel_ore": ("S", 90), "organics": ("B", 200), "equipment": ("B", 320)}

# Sec100 BSB  <->  Sec101 SBS: forward is forced to organics (the only
# commodity home sells); the player chooses fuel or equipment to buy back.
_BSB_100 = {"fuel_ore": ("B", 100), "organics": ("S", 160), "equipment": ("B", 300)}
_SBS_101 = {"fuel_ore": ("S", 90), "organics": ("B", 200), "equipment": ("S", 280)}

# Degenerate one-way pairs (all three commodities flow one direction).
_SSS_100 = {"fuel_ore": ("S", 90), "organics": ("S", 160), "equipment": ("S", 260)}
_BBB_101 = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
_BBB_100 = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
_SSS_101 = {"fuel_ore": ("S", 90), "organics": ("S", 160), "equipment": ("S", 260)}


class P2PCommandTests(unittest.IsolatedAsyncioTestCase):
    """The 'p2p' port-to-port auto-shuttle: the commodity prompt, the
    starting-holds rules, the move-and-auto-trade legs, profit, and every
    way a shuttle ends (stop, out of turns, port can't sustain, attacked)."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["warps"] = {100: [101], 101: [100]}
        STATE["ports"] = {}
        STATE["sector_mines"] = {}
        STATE["stations"] = {}
        STATE["trade_log"] = []
        STATE["move_log"] = []
        STATE["kills"] = []

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _pair(self, home_spec, adj_spec, home_class="BSS", adj_class="SBB"):
        STATE["ports"] = {
            100: _p2p_port(100, 1, home_class, home_spec),
            101: _p2p_port(101, 2, adj_class, adj_spec),
        }

    async def p2p(self, args):
        return await main.cmd_p2p(self.ctx(), args)

    async def step(self, msg):
        return await main.cmd_p2p_step(self.ctx(), msg)

    # --- setup / prompt ---------------------------------------------------

    async def test_prompt_offers_forward_choice_in_bss_sbb(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=75)
        prompt = await self.p2p("101")
        self.assertIn("P2P Sec100<->Sec101", prompt)
        self.assertIn("organics", prompt)
        self.assertIn("equipment", prompt)
        self.assertIn("FULL", prompt)
        self.assertTrue(main.PENDING_P2P[PUBKEY]["stage"] == "choose")

    async def test_usage_and_geometry_rejections(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=75)
        self.assertIn("Usage", await self.p2p(""))
        self.assertIn("isn't adjacent", await self.p2p("500"))
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_no_shuttleable_commodity_rejected(self):
        # Two identical BSS ports share no commodity that one buys and the
        # other sells, so there's nothing to shuttle.
        self._pair(_BSS_100, _BSS_100, adj_class="BSS")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=75)
        msg = await self.p2p("101")
        self.assertIn("no commodity to shuttle", msg)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_current_sector_needs_a_commodity_port(self):
        STATE["ports"] = {101: _p2p_port(101, 2, "SBB", _SBB_101)}  # none at 100
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=75)
        self.assertIn("needs a commodity port", await self.p2p("101"))

    # --- happy path -------------------------------------------------------

    async def test_full_round_trip_profits_and_restores_cargo(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        await self.p2p("101")
        leg1 = await self.step("organics")
        self.assertIn("Entered Sec101, docked.", leg1)
        self.assertIn("Sold 75 organics for +15000cr", leg1)   # 75 * 200
        self.assertIn("Bought 75 fuel ore for -6750cr", leg1)   # 75 * 90
        self.assertIn("Leg +8250cr, total +8250cr", leg1)        # 75*(200-90)
        self.assertEqual(STATE["player"]["sector_id"], 101)
        self.assertEqual(STATE["player"]["fuel_ore"], 75)
        self.assertEqual(STATE["player"]["organics"], 0)
        self.assertEqual(STATE["player"]["turns_remaining"], 9)

        leg2 = await self.step("y")
        self.assertIn("Entered Sec100, docked.", leg2)
        self.assertIn("Sold 75 fuel ore for +7500cr", leg2)     # 75 * 100
        self.assertIn("Bought 75 organics for -12000cr", leg2)  # 75 * 160
        self.assertIn("Leg -4500cr, total +3750cr", leg2)       # leg loss, run profit
        # Back to the start holding organics, one full round-trip of profit.
        self.assertEqual(STATE["player"]["sector_id"], 100)
        self.assertEqual(STATE["player"]["organics"], 75)
        self.assertEqual(STATE["player"]["credits"], 100000 + 3750)  # 75*40 + 75*10

    async def test_auto_max_quantity_and_listed_price(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        await self.p2p("101")
        await self.step("organics")
        # First two trades: sell 75 organics @200, buy 75 fuel @90.
        self.assertEqual(STATE["trade_log"][0], ("organics", 75, 15000, False))
        self.assertEqual(STATE["trade_log"][1], ("fuel_ore", 75, 6750, True))

    async def test_bsb_sbs_forces_organics_and_chooses_backward(self):
        self._pair(_BSB_100, _SBS_101, home_class="BSB", adj_class="SBS")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=50,
                                       organics=50, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("organics", prompt)   # forced forward named
        self.assertIn("fuel", prompt)        # backward choices
        self.assertIn("equipment", prompt)
        leg1 = await self.step("fuel")       # buy fuel back
        self.assertIn("Sold 50 organics", leg1)
        self.assertIn("Bought 50 fuel ore", leg1)

    # --- degenerate one-way shuttles -------------------------------------

    async def test_one_way_forward_only_sss_bbb(self):
        # Home sells all (SSS), adj buys all (BBB): a one-way organics run.
        self._pair(_SSS_100, _BBB_101, home_class="SSS", adj_class="BBB")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40,
                                       organics=40, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("One-way", prompt)
        self.assertIn("FULL", prompt)
        leg1 = await self.step("organics")
        self.assertIn("Sold 40 organics for +8000cr", leg1)   # 40*200
        self.assertNotIn("Bought", leg1)                       # nothing to buy at adj
        self.assertEqual(STATE["player"]["organics"], 0)
        leg2 = await self.step("y")                            # back home, rebuy
        self.assertIn("Bought 40 organics for -6400cr", leg2)  # 40*160
        self.assertNotIn("Sold", leg2)
        self.assertEqual(STATE["player"]["organics"], 40)

    async def test_one_way_backward_only_requires_empty_holds(self):
        self._pair(_BBB_100, _SSS_101, home_class="BBB", adj_class="SSS")
        # Carrying cargo -> rejected with the empty-holds rule.
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40, organics=10)
        await self.p2p("101")
        reject = await self.step("fuel")
        self.assertIn("EMPTY holds", reject)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)
        # Empty holds -> first leg buys at the far port.
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40,
                                       credits=100000, turns_remaining=10)
        await self.p2p("101")
        leg1 = await self.step("fuel")
        self.assertIn("Bought 40 fuel ore for -3600cr", leg1)  # 40*90
        self.assertNotIn("Sold", leg1)

    # --- starting-holds rules --------------------------------------------

    async def test_partial_holds_rejected(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=50)
        await self.p2p("101")
        reject = await self.step("organics")
        self.assertIn("FULL of organics", reject)
        self.assertIn("50/75", reject)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)
        self.assertEqual(STATE["trade_log"], [])

    async def test_holds_full_of_wrong_commodity_rejected(self):
        self._pair(_BSS_100, _SBB_101)
        # Full of equipment but the player picks organics.
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, equipment=75)
        await self.p2p("101")
        reject = await self.step("organics")
        self.assertIn("FULL of organics", reject)

    async def test_station_core_blocks_start(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, station_core=1)
        await self.p2p("101")
        reject = await self.step("organics")
        self.assertIn("Station Core", reject)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    # --- ending conditions -----------------------------------------------

    async def test_stop_ends_the_shuttle(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        await self.p2p("101")
        await self.step("organics")
        for stop in ("n", "no", "s", "stop", "cancel"):
            STATE["player"]["sector_id"] = 101
            main.PENDING_P2P[PUBKEY] = {
                "stage": "continue", "home": 100, "adj": 101,
                "forward": "organics", "backward": "fuel_ore",
                "next_dest": 100, "profit": 999,
            }
            msg = await self.step(stop)
            self.assertIn("P2P ended", msg)
            self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_unrecognized_continue_reply_reprompts(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=101, holds_total=75,
                                       fuel_ore=75, credits=100000, turns_remaining=10)
        main.PENDING_P2P[PUBKEY] = {
            "stage": "continue", "home": 100, "adj": 101,
            "forward": "organics", "backward": "fuel_ore",
            "next_dest": 100, "profit": 0,
        }
        msg = await self.step("huh?")
        self.assertIn("y to continue", msg)
        self.assertIn(PUBKEY, main.PENDING_P2P)   # still running

    async def test_out_of_turns_ends_without_moving(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=0)
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("Out of turns", msg)
        self.assertEqual(STATE["move_log"], [])    # never moved
        self.assertEqual(STATE["trade_log"], [])
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_cant_sustain_low_stock_skips_and_ends(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["ports"][101]["fuel_ore_qty"] = 10   # far port can't fill 75 holds
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("can't sustain", msg)
        self.assertIn("still in Sec100", msg)
        self.assertEqual(STATE["move_log"], [])    # no move, no turn spent
        self.assertEqual(STATE["trade_log"], [])
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_cant_sustain_no_room_skips_and_ends(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["ports"][101]["organics_qty"] = 9960  # room only 40 < 75 load
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("can't sustain", msg)
        self.assertEqual(STATE["trade_log"], [])

    # --- abandon on hostile presence -------------------------------------

    async def test_abandon_on_hostile_mines(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, shields=50, fighters=0,
                                       credits=100000, turns_remaining=10)
        STATE["sector_mines"] = {101: {2: 1}}      # someone else's mine
        movement.random = FakeRandom([3])          # 3 damage, survivable
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("detonate", msg)
        self.assertIn("P2P shuttle abandoned", msg)
        self.assertEqual(STATE["player"]["sector_id"], 101)  # moved, attacked
        self.assertEqual(STATE["trade_log"], [])             # but did not trade
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_abandon_on_non_owner_station(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, credits=100000, turns_remaining=10)
        STATE["stations"] = {9: {
            "id": 9, "sector_id": 101, "owner_id": 2, "owner_name": "Rival",
            "level": 1, "shields": 100, "fighters": 0, "shields_enabled": 1,
            "fuel": 0, "organics": 0, "equipment": 0,
            "posture": "defensive", "engage_pct": 100,
            "last_fuel_burn": "2026-06-27T12:00:00+00:00",
            "upgrade_to": None, "upgrade_started_at": None,
            "credits": 0,
        }}
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("P2P shuttle abandoned", msg)
        self.assertEqual(STATE["trade_log"], [])
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_destroyed_mid_leg_ends_shuttle(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["warps"] = {100: [101], 101: [100]}
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75,
                                       organics=75, shields=0, fighters=0,
                                       credits=100000, turns_remaining=10)
        STATE["sector_mines"] = {101: {2: 1}}
        movement.random = FakeRandom([10])         # lethal to a 0/0 ship
        await self.p2p("101")
        msg = await self.step("organics")
        self.assertIn("DESTROYED", msg)
        self.assertEqual(STATE["trade_log"], [])
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    async def test_cancel_at_choose_stage(self):
        self._pair(_BSS_100, _SBB_101)
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=75, organics=75)
        await self.p2p("101")
        msg = await self.step("cancel")
        self.assertIn("cancelled", msg)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)

    # --- partial-overlap pairs (adjacent BBB, and a two-way overlap) -----

    async def test_bss_beside_bbb_one_way_with_forward_choice(self):
        # BSS sells organics+equipment; BBB buys everything, sells nothing.
        # A one-way forward run with a choice of organics or equipment.
        adj_bbb = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
        self._pair(_BSS_100, adj_bbb, adj_class="BBB")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=60,
                                       organics=60, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("One-way", prompt)
        self.assertIn("organics", prompt)
        self.assertIn("equipment", prompt)
        leg1 = await self.step("organics")
        self.assertIn("Sold 60 organics for +12000cr", leg1)   # 60*200 at BBB
        self.assertNotIn("Bought", leg1)                        # BBB sells nothing
        self.assertEqual(STATE["player"]["organics"], 0)
        leg2 = await self.step("y")                             # back home, rebuy
        self.assertIn("Bought 60 organics for -9600cr", leg2)   # 60*160 at BSS
        self.assertEqual(STATE["player"]["organics"], 60)

    async def test_ssb_beside_bbb_offers_fuel_or_organics(self):
        ssb = {"fuel_ore": ("S", 90), "organics": ("S", 160), "equipment": ("B", 300)}
        adj_bbb = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
        self._pair(ssb, adj_bbb, home_class="SSB", adj_class="BBB")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=50,
                                       fuel_ore=50, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("fuel", prompt)
        self.assertIn("organics", prompt)
        self.assertNotIn("equipment", prompt)   # equipment is B/B -> not shuttleable
        leg1 = await self.step("fuel")
        self.assertIn("Sold 50 fuel ore for +5000cr", leg1)     # 50*100 at BBB

    async def test_sbb_beside_bbb_is_a_forced_confirm(self):
        # SBB sells only fuel; beside BBB just one commodity qualifies, so
        # the prompt is a confirm, not a choice.
        sbb = {"fuel_ore": ("S", 90), "organics": ("B", 200), "equipment": ("B", 320)}
        adj_bbb = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
        self._pair(sbb, adj_bbb, home_class="SBB", adj_class="BBB")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40,
                                       fuel_ore=40, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("Confirm? y/n", prompt)
        self.assertIn("fuel ore", prompt)
        # A commodity reply isn't needed -- 'y' starts it.
        leg1 = await self.step("y")
        self.assertIn("Sold 40 fuel ore for +4000cr", leg1)     # 40*100 at BBB
        self.assertNotIn("Bought", leg1)

    async def test_bbs_beside_bbb_forced_confirm_needs_full_holds(self):
        # BBS sells only equipment; forced confirm, and holds must be full
        # of equipment to start.
        bbs = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("S", 260)}
        adj_bbb = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
        self._pair(bbs, adj_bbb, home_class="BBS", adj_class="BBB")
        # Wrong cargo -> rejected on confirm.
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40, organics=40)
        await self.p2p("101")
        reject = await self.step("y")
        self.assertIn("FULL of equipment", reject)
        self.assertNotIn(PUBKEY, main.PENDING_P2P)
        # Correct cargo -> runs.
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40,
                                       equipment=40, credits=100000, turns_remaining=10)
        await self.p2p("101")
        leg1 = await self.step("y")
        self.assertIn("Sold 40 equipment for +12800cr", leg1)   # 40*320 at BBB

    async def test_forced_confirm_reprompts_on_unrecognized_reply(self):
        sbb = {"fuel_ore": ("S", 90), "organics": ("B", 200), "equipment": ("B", 320)}
        adj_bbb = {"fuel_ore": ("B", 100), "organics": ("B", 200), "equipment": ("B", 320)}
        self._pair(sbb, adj_bbb, home_class="SBB", adj_class="BBB")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=40,
                                       fuel_ore=40, credits=100000, turns_remaining=10)
        await self.p2p("101")
        msg = await self.step("wat")
        self.assertIn("y to begin", msg)
        self.assertIn(PUBKEY, main.PENDING_P2P)   # still awaiting confirm

    async def test_two_way_partial_overlap_is_a_forced_confirm(self):
        # BSS beside SBS: fuel(B/S)=backward, organics(S/B)=forward,
        # equipment(S/S)=ignored -> a forced two-way organics<->fuel shuttle.
        sbs = {"fuel_ore": ("S", 90), "organics": ("B", 200), "equipment": ("S", 280)}
        self._pair(_BSS_100, sbs, adj_class="SBS")
        STATE["player"] = fresh_player(id=1, sector_id=100, holds_total=50,
                                       organics=50, credits=100000, turns_remaining=10)
        prompt = await self.p2p("101")
        self.assertIn("Confirm? y/n", prompt)
        leg1 = await self.step("y")
        self.assertIn("Sold 50 organics for +10000cr", leg1)    # 50*200
        self.assertIn("Bought 50 fuel ore for -4500cr", leg1)   # 50*90
        leg2 = await self.step("y")
        self.assertIn("Sold 50 fuel ore for +5000cr", leg2)     # 50*100
        self.assertIn("Bought 50 organics for -8000cr", leg2)   # 50*160
        self.assertEqual(STATE["player"]["organics"], 50)       # back to start


class MineDetonationTests(unittest.IsolatedAsyncioTestCase):
    """Entering a sector that holds someone else's mines: damage, survival,
    own-mine safety, and the destruction -> escape-pod path."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["ship_log"] = []
        STATE["mine_log"] = []
        STATE["defense_log"] = []
        STATE["kills"] = []
        STATE["kill_log_cutoff"] = {}
        STATE["sector_mines"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_surviving_a_hit_records_reduced_defenses(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=20, fighters=10)
        STATE["sector_mines"] = {13: {2: 2}}   # player 2 laid 2 mines
        movement.random = FakeRandom([5, 4])    # 9 total damage

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("2 mines detonate for 9 damage", prompt)
        self.assertIn("Lost 9 shields, 0 fighters", prompt)
        self.assertIn("now 11 shields, 10 fighters", prompt)
        self.assertEqual(STATE["defense_log"], [(1, 11, 10)])
        self.assertEqual(STATE["player"]["sector_id"], 13)
        self.assertEqual(STATE["sector_mines"].get(13), {})  # detonated, cleared

    async def test_own_mines_do_not_detonate(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=20, fighters=10)
        STATE["sector_mines"] = {13: {1: 5}}    # the entering player's own mines

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Moved to Sec13.", prompt)
        self.assertNotIn("detonate", prompt)
        self.assertEqual(STATE["defense_log"], [])
        self.assertEqual(STATE["sector_mines"][13], {1: 5})  # left in place

    async def test_destruction_ejects_into_an_escape_pod_far_away(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=5, fighters=2,
                                       fuel_ore=10, organics=5, equipment=3)
        STATE["sector_mines"] = {13: {2: 10}}
        movement.random = FakeRandom([10] * 10)  # 100 damage -- lethal

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Bismark is DESTROYED", prompt)
        self.assertIn("Escape Pod", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Escape Pod")
        self.assertEqual((final["shields"], final["fighters"], final["holds_total"]), (0, 0, 0))
        self.assertEqual((final["fuel_ore"], final["organics"], final["equipment"]), (0, 0, 0))
        # Landed somewhere 4-6 hops from the blast (Sec13) on the line graph.
        landed = final["sector_id"]
        self.assertIn(landed, set(main.sectors_within_hop_range(STATE["warps"], 13, 4, 6)))
        self.assertEqual(len(STATE["ship_log"]), 1)
        self.assertEqual(STATE["ship_log"][0][0], "Escape Pod")
        # A public kill is logged, credited to mines (no killer name).
        self.assertEqual(STATE["kills"][-1], {
            "victim_name": "Tester", "killer_name": None,
            "sector_id": 13, "kind": "ship",
            "created_at": STATE["kills"][-1]["created_at"],
        })

    async def test_death_mid_route_cancels_the_rest_of_the_course(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=0, fighters=0)
        STATE["sector_mines"] = {13: {2: 3}}
        movement.random = FakeRandom([10, 10, 10])

        # Plot 12 -> 13 -> 14 -> 15; the first hop lands on the mines.
        prompt = await main.cmd_move(self.ctx(), "15")
        self.assertIn("Warp to: 12 -> 13 -> 14 -> 15?", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [13, 14, 15])

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertIn("DESTROYED", prompt)
        self.assertNotIn("Warp to:", prompt)              # route abandoned
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_pod_into_mines_is_a_total_reset_not_another_pod(self):
        # A pilot already flying an Escape Pod has nothing to eject into,
        # so hitting mines wipes them out exactly like having their pod
        # shot in combat: a fresh Falcon, credits reset, back at the home
        # Stardock -- NOT another drifting pod.
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Escape Pod",
                                       shields=0, fighters=0, credits=8000)
        STATE["port"] = fresh_port("STARDOCK")    # so Sec1 renders as the Stardock
        STATE["sector_mines"] = {13: {2: 3}}
        movement.random = FakeRandom([10, 10, 10])  # any damage is lethal to a pod

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Escape Pod is GONE", prompt)
        self.assertIn("restart with 20000cr in a Falcon", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Falcon")     # not another Escape Pod
        self.assertEqual(final["credits"], 20000)          # reset to 20k
        self.assertEqual(final["sector_id"], 1)            # back at the home Stardock
        self.assertEqual(final["holds_total"], SHIP_CATALOG["Falcon"]["base_holds"])
        self.assertEqual(STATE["ship_log"][-1][0], "Falcon")
        # Logged as a public mine kill of the pod (kind 'pod', no killer).
        self.assertEqual(STATE["kills"][-1]["killer_name"], None)
        self.assertEqual(STATE["kills"][-1]["victim_name"], "Tester")
        self.assertEqual(STATE["kills"][-1]["kind"], "pod")
        self.assertEqual(STATE["kills"][-1]["sector_id"], 13)

    async def test_pod_reset_mid_route_drops_the_rest_of_the_course(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Escape Pod",
                                       shields=0, fighters=0, credits=8000)
        STATE["sector_mines"] = {13: {2: 3}}
        movement.random = FakeRandom([10, 10, 10])

        # Plot 12 -> 13 -> 14 -> 15; the first hop lands on the mines.
        prompt = await main.cmd_move(self.ctx(), "15")
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [13, 14, 15])

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertIn("Escape Pod is GONE", prompt)
        self.assertNotIn("Warp to:", prompt)              # route abandoned
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_escape_pod_is_not_offered_for_sale_in_the_shipyard(self):
        STATE["player"] = fresh_player(id=1, sector_id=1, ship_type="Escape Pod")
        STATE["port"] = fresh_port("STARDOCK")

        main.PENDING_UPGRADES[PUBKEY] = {"stage": "shipyard_menu"}
        prompt = await main.cmd_stardock_step(self.ctx(), "")  # re-show shipyard menu

        self.assertIn("1) Falcon", prompt)
        self.assertIn("9) Bismark", prompt)                # all 9 hulls listed
        self.assertNotIn("10)", prompt)                    # ...and nothing past them
        self.assertNotIn(") Escape Pod (", prompt)         # pod is never a buy option
        self.assertIn("Sell your Escape Pod", prompt)      # but can be traded back in


class CombatMenuTests(unittest.IsolatedAsyncioTestCase):
    """The combat submenu under help: combat commands are hidden from the
    top-level menu and listed only by 'combat' / 'help combat'."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["player"] = fresh_player()

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_main_menu_lists_combat_and_hides_combat_commands(self):
        prompt = await main.cmd_menu(self.ctx(), "")

        self.assertIn("combat -", prompt)            # pointer into the submenu
        self.assertIn("status -", prompt)            # ordinary command still shown
        # The combat commands themselves don't clutter the top-level menu.
        self.assertNotIn("lay mines in this sector", prompt)
        self.assertNotIn("send a recon probe", prompt)

    async def test_combat_command_lists_lay_and_probe(self):
        prompt = await main.cmd_combat(self.ctx(), "")

        self.assertIn("Combat commands:", prompt)
        self.assertIn("lay - lay mines in this sector", prompt)
        self.assertIn("probe - send a recon probe", prompt)
        self.assertNotIn("status -", prompt)         # not a combat command

    async def test_help_combat_argument_shows_the_submenu(self):
        prompt = await main.cmd_menu(self.ctx(), "combat")

        self.assertIn("Combat commands:", prompt)
        self.assertIn("lay -", prompt)
        self.assertIn("probe -", prompt)

    async def test_help_unknown_submenu_is_friendly(self):
        prompt = await main.cmd_menu(self.ctx(), "bogus")

        self.assertIn("(no bogus commands)", prompt)


class ProbeRefitTests(unittest.IsolatedAsyncioTestCase):
    """Buying probes at the Stardock -- a universal refit (100cr each),
    available to every hull, unlike mines."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["upgrade_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def test_buy_probes_full_flow_on_a_falcon(self):
        STATE["player"] = fresh_player(credits=1000, ship_type="Falcon", probes=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Probes (option 4 for a Falcon)
        self.assertIn("Probes: 0/10, 100cr each.", prompt)
        self.assertIn("Buy how many? (0-10, or 'cancel')", prompt)  # 1000cr / 100cr each

        prompt = await self.say("3")
        self.assertIn("Buy 3 Probes for 300cr (100cr/unit)? yes/no", prompt)

        prompt = await self.say("yes")
        self.assertIn("Installed 3 Probes for -300cr.", prompt)
        self.assertIn("Probes 3/10 @ 100cr each", prompt)

        final = STATE["player"]
        self.assertEqual(final["probes"], 3)
        self.assertEqual(final["credits"], 1000 - 300)
        self.assertEqual(STATE["upgrade_log"], [("probes", 3, 300)])


class ProbeCommandTests(unittest.IsolatedAsyncioTestCase):
    """Launching a recon probe: it scouts a route the player stays out of,
    is consumed on launch, and dies to a single hostile mine."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["probe_log"] = []
        STATE["sector_mines"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_no_probes_aboard_is_rejected(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=0)

        prompt = await main.cmd_probe(self.ctx(), "15")

        self.assertIn("No probes aboard", prompt)
        self.assertEqual(STATE["probe_log"], [])

    async def test_bad_targets_are_rejected_without_spending_a_probe(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=3)

        self.assertIn("Send a probe where?", await main.cmd_probe(self.ctx(), ""))
        self.assertIn("isn't a sector number", await main.cmd_probe(self.ctx(), "abc"))
        self.assertIn("out of range", await main.cmd_probe(self.ctx(), "9999"))
        self.assertIn("already in your sector", await main.cmd_probe(self.ctx(), "12"))

        self.assertEqual(STATE["probe_log"], [])      # nothing launched
        self.assertEqual(STATE["player"]["probes"], 3)

    async def test_no_route_found_does_not_spend_a_probe(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=3)
        STATE["warps"] = {12: [13], 13: [12], 20: []}  # 20 is unreachable

        prompt = await main.cmd_probe(self.ctx(), "20")

        self.assertIn("No route found to Sec20.", prompt)
        self.assertEqual(STATE["probe_log"], [])
        self.assertEqual(STATE["player"]["probes"], 3)

    async def test_probe_reports_each_sector_and_reaches_destination(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=3)

        prompt = await main.cmd_probe(self.ctx(), "15")

        self.assertIn("Probe away to Sec15 (3 hops); 2 left aboard.", prompt)
        # Reports each sector it passes through, as the player would see it.
        self.assertIn("Sec13", prompt)
        self.assertIn("Sec14", prompt)
        self.assertIn("Sec15", prompt)
        self.assertIn("Probe reached Sec15 and signs off.", prompt)
        # Consumed exactly one probe; the player never moved.
        self.assertEqual(STATE["probe_log"], [1])
        self.assertEqual(STATE["player"]["probes"], 2)
        self.assertEqual(STATE["player"]["sector_id"], 12)

    async def test_a_single_hostile_mine_destroys_the_probe_and_is_spent(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=2)
        STATE["sector_mines"] = {14: {2: 3}}  # player 2 laid 3 mines at Sec14

        prompt = await main.cmd_probe(self.ctx(), "16")

        self.assertIn("Sec13", prompt)                      # scouted before the field
        self.assertIn("Sec14: a mine detonates -- PROBE DESTROYED here.", prompt)
        self.assertNotIn("Sec15", prompt)                   # route stopped at the mine
        self.assertNotIn("Probe reached", prompt)
        # Only ONE mine is spent; the rest of the field remains for real ships.
        self.assertEqual(STATE["sector_mines"][14], {2: 2})
        self.assertEqual(STATE["probe_log"], [1])

    async def test_own_mines_do_not_destroy_the_probe(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=2)
        STATE["sector_mines"] = {14: {1: 5}}  # the probe owner's own mines

        prompt = await main.cmd_probe(self.ctx(), "15")

        self.assertIn("Probe reached Sec15 and signs off.", prompt)
        self.assertNotIn("DESTROYED", prompt)
        self.assertEqual(STATE["sector_mines"][14], {1: 5})  # left untouched


class SectorPresenceTests(unittest.IsolatedAsyncioTestCase):
    """Other pilots parked in a sector show up on the info screen; the
    viewer is left out, and a sector empty of others reads exactly as it
    did before the feature."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["warps"] = chain_warps(30)
        STATE["sector_mines"] = {}
        STATE["sector_players"] = {}

    def tearDown(self):
        # Don't let a presence fixture leak into other suites' sector info.
        STATE["sector_players"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_info_lists_other_ships_and_excludes_self(self):
        STATE["player"] = fresh_player(id=1, sector_id=1)
        STATE["sector_players"] = {1: [{"id": 1, "name": "Alice", "fighters": 5},
                                       {"id": 2, "name": "Bob", "fighters": 1000},
                                       {"id": 3, "name": "Cleo", "fighters": 3}]}

        prompt = await main.cmd_info(self.ctx(), "")

        self.assertIn("Ships here: Bob (1000 ftr), Cleo (3 ftr)", prompt)
        self.assertNotIn("Alice", prompt)   # the viewer isn't listed among them

    async def test_opponent_fighters_shown_but_shields_hidden(self):
        # A nosy pilot can read fighter strength off the info screen but
        # never an opponent's shields -- those stay secret until combat.
        STATE["player"] = fresh_player(id=1, sector_id=1)
        STATE["sector_players"] = {1: [{"id": 1, "name": "Alice", "fighters": 0},
                                       {"id": 2, "name": "Bob",
                                        "fighters": 7, "shields": 999}]}

        prompt = await main.cmd_info(self.ctx(), "")

        self.assertIn("Bob (7 ftr)", prompt)
        self.assertNotIn("999", prompt)               # shield count never leaks
        self.assertNotIn("shield", prompt.lower())     # nor even the word

    async def test_solo_sector_has_no_ships_line(self):
        STATE["player"] = fresh_player(id=1, sector_id=1)
        STATE["sector_players"] = {1: [{"id": 1, "name": "Alice", "fighters": 0}]}  # only the viewer

        prompt = await main.cmd_info(self.ctx(), "")

        self.assertNotIn("Ships here", prompt)

    async def test_arriving_shows_who_is_parked_there(self):
        STATE["player"] = fresh_player(id=1, sector_id=12)
        # Zane is parked in Sec13; Alice (the viewer) is about to arrive.
        STATE["sector_players"] = {13: [{"id": 1, "name": "Alice", "fighters": 0},
                                        {"id": 9, "name": "Zane", "fighters": 42}]}

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Moved to Sec13.", prompt)
        self.assertIn("Ships here: Zane (42 ftr)", prompt)
        self.assertNotIn("Alice", prompt)

    async def test_probe_reports_ships_in_scouted_sectors(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, probes=2)
        STATE["sector_players"] = {14: [{"id": 7, "name": "Mara", "fighters": 8}]}

        prompt = await main.cmd_probe(self.ctx(), "15")

        self.assertIn("Ships here: Mara (8 ftr)", prompt)   # spotted at Sec14 en route


class AttackMathTests(unittest.TestCase):
    """The pure attack cascade: fighters at 0.75:1, then shields at 10:1."""

    def test_fighters_trade_at_three_quarters(self):
        # 1000 vs 1000 fighters, no shields: all defenders die, 250 attackers remain.
        self.assertEqual(main.resolve_attack(1000, 1000, 0), (250, 0, 0, True))

    def test_clearing_shields_with_last_fighter_leaves_target_alive(self):
        # 20 fighters vs 0 fighters / 200 shields: shields gone, all 20 spent,
        # attacker now at 0 so the defender survives at 0/0 (not destroyed).
        self.assertEqual(main.resolve_attack(20, 0, 200), (0, 0, 0, False))

    def test_too_few_fighters_only_dents_the_defenders(self):
        # 20 attackers vs 1000 defenders: floor(20/0.75)=26 killed, attacker wiped.
        self.assertEqual(main.resolve_attack(20, 1000, 0), (0, 974, 0, False))

    def test_overwhelming_force_destroys(self):
        # Clear 100 fighters (cost 75), then 200 shields (cost 20): survive with 905.
        self.assertEqual(main.resolve_attack(1000, 100, 200), (905, 0, 0, True))

    def test_partial_shield_damage(self):
        # 5 fighters vs 0 / 200: strip 50 shields, attacker wiped, target lives.
        self.assertEqual(main.resolve_attack(5, 0, 200), (0, 0, 150, False))

    def test_one_fighter_pops_a_defenseless_pod(self):
        self.assertEqual(main.resolve_attack(1, 0, 0), (1, 0, 0, True))

    def test_spending_last_fighter_on_fighters_leaves_shields_standing(self):
        # 75 attackers exactly clear 100 defender fighters; none left for the
        # 200 shields, so the target survives.
        self.assertEqual(main.resolve_attack(75, 100, 200), (0, 0, 200, False))


class AttackCommandTests(unittest.IsolatedAsyncioTestCase):
    """The `a`/attack command: targeting, fighter write-back, and kills."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["defense_log"] = []
        STATE["ship_log"] = []
        STATE["move_log"] = []
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["kill_log_cutoff"] = {}
        STATE["players_by_id"] = {}
        # Sec15 is the staging sector for these fights -- outside the
        # Sec1-10 no-combat safe zone. In chain_warps(30) its neighbors
        # are [14, 16].
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def _aim_and_commit(self, args, reply):
        """Drive the two-step attack: aim with cmd_attack, then answer the
        'how many fighters?' prompt with `reply` (a number, 'all', or
        'cancel') via cmd_attack_step. Returns the step's reply."""
        await main.cmd_attack(self.ctx(), args)
        return await main.cmd_attack_step(self.ctx(), reply)

    def _defender(self, **over):
        d = {"id": 2, "name": "Bob", "ship_type": "Bismark",
             "fighters": 1000, "shields": 200, "sector_id": 15, "credits": 5000}
        d.update(over)
        return d

    async def test_combat_is_banned_in_the_safe_zone(self):
        # Same setup as a normal fight, but inside the Sec1-10 safe zone:
        # the attack is refused outright, nothing fires, nothing pends.
        STATE["player"] = fresh_player(id=1, sector_id=5, fighters=500)
        STATE["players_by_id"] = {2: self._defender(sector_id=5)}

        prompt = await main.cmd_attack(self.ctx(), "Bob")

        self.assertIn("safe zone", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)   # no attack queued
        self.assertEqual(STATE["defense_log"], [])       # no shots fired
        self.assertEqual(STATE["attack_events"], [])
        self.assertEqual(STATE["players_by_id"][2]["fighters"], 1000)

    async def test_no_target_present(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("No other ships here", prompt)

    async def test_no_fighters_to_attack_with(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=0)
        STATE["players_by_id"] = {2: self._defender()}
        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("no fighters", prompt)
        self.assertEqual(STATE["attack_events"], [])

    async def test_unknown_named_target_is_rejected(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(name="Bob")}
        prompt = await main.cmd_attack(self.ctx(), "Zara")
        self.assertIn("No ship named 'Zara'", prompt)
        self.assertEqual(STATE["attack_events"], [])

    async def test_bare_attack_cycles_targets_one_at_a_time(self):
        """Bare 'a' walks the targets with yes/no prompts: 'no' moves to
        the next pilot, 'yes' locks on and asks for a fighter count."""
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(id=2, name="Bob"),
                                  3: self._defender(id=3, name="Cleo")}

        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("Attack Bob (1000 ftr)? yes/no", prompt)

        prompt = await main.cmd_attack_step(self.ctx(), "no")
        self.assertIn("Attack Cleo (1000 ftr)? yes/no", prompt)

        prompt = await main.cmd_attack_step(self.ctx(), "yes")
        self.assertIn("Attack Cleo with how many fighters", prompt)
        self.assertEqual(STATE["defense_log"], [])   # still nothing fired

    async def test_bare_attack_declining_every_target_calls_it_off(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(id=2, name="Bob")}

        await main.cmd_attack(self.ctx(), "")
        prompt = await main.cmd_attack_step(self.ctx(), "no")

        self.assertIn("no more targets", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)
        self.assertEqual(STATE["defense_log"], [])

    async def test_aiming_prompts_for_a_fighter_count_without_firing(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        prompt = await main.cmd_attack(self.ctx(), "Bob")

        self.assertIn("how many fighters", prompt)
        self.assertIn("500", prompt)                       # advertises what's aboard
        self.assertIn(PUBKEY, main.PENDING_ATTACKS)        # attack is now pending
        self.assertEqual(STATE["defense_log"], [])         # nothing fired yet
        self.assertEqual(STATE["attack_events"], [])
        self.assertEqual(STATE["players_by_id"][2]["fighters"], 1000)  # defender untouched

    async def test_committing_a_subset_keeps_the_reserve(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500, shields=10)
        STATE["players_by_id"] = {2: self._defender(fighters=100, shields=0)}

        prompt = await self._aim_and_commit("Bob", "75")

        # 75 fighters exactly clear 100 defenders (cost ceil(0.75*100)=75),
        # none left over -> defender lives at 0; the 425 reserve is kept.
        self.assertIn("You have 425 fighters", prompt)
        self.assertEqual(STATE["player"]["fighters"], 425)   # 500 total - 75 engaged
        self.assertEqual(STATE["players_by_id"][2]["fighters"], 0)
        self.assertEqual(STATE["attack_events"][0]["outcome"], "attacked")
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)       # flow is finished
        self.assertEqual(STATE["kills"], [])                 # a mere hit isn't a kill

    async def test_cancel_aborts_the_attack(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        await main.cmd_attack(self.ctx(), "Bob")
        prompt = await main.cmd_attack_step(self.ctx(), "cancel")

        self.assertIn("called off", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)
        self.assertEqual(STATE["defense_log"], [])           # no shots fired
        self.assertEqual(STATE["attack_events"], [])
        self.assertEqual(STATE["players_by_id"][2]["fighters"], 1000)

    async def test_committing_more_than_aboard_is_rejected(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=50)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        await main.cmd_attack(self.ctx(), "Bob")
        prompt = await main.cmd_attack_step(self.ctx(), "9999")

        self.assertIn("only have 50", prompt)
        self.assertIn(PUBKEY, main.PENDING_ATTACKS)          # still pending, retryable
        self.assertEqual(STATE["defense_log"], [])

    async def test_non_numeric_commit_reprompts_without_firing(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=50)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        await main.cmd_attack(self.ctx(), "Bob")
        prompt = await main.cmd_attack_step(self.ctx(), "lots")

        self.assertIn("how many", prompt.lower())
        self.assertIn(PUBKEY, main.PENDING_ATTACKS)
        self.assertEqual(STATE["attack_events"], [])

    async def test_target_that_left_before_commit_is_handled(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        await main.cmd_attack(self.ctx(), "Bob")
        STATE["players_by_id"][2]["sector_id"] = 16   # Bob warps off mid-prompt

        prompt = await main.cmd_attack_step(self.ctx(), "100")

        self.assertIn("no longer", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)
        self.assertEqual(STATE["attack_events"], [])

    async def test_step_with_nothing_pending_is_a_noop(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        prompt = await main.cmd_attack_step(self.ctx(), "100")
        self.assertIn("No attack in progress", prompt)

    async def test_hit_but_not_destroyed_writes_back_both_ships(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=300, shields=10)
        STATE["players_by_id"] = {2: self._defender(fighters=1000, shields=200)}

        prompt = await self._aim_and_commit("Bob", "all")

        # 300 attackers kill floor(300/0.75)=400 defenders -> 600 left, attacker wiped.
        self.assertIn("You have 0 fighters", prompt)
        self.assertEqual(STATE["player"]["fighters"], 0)   # attacker spent all
        self.assertEqual(STATE["player"]["shields"], 10)   # attacker shields untouched
        self.assertEqual(STATE["players_by_id"][2]["fighters"], 600)
        self.assertEqual(len(STATE["attack_events"]), 1)
        self.assertEqual(STATE["attack_events"][0]["outcome"], "attacked")
        self.assertEqual(STATE["attack_events"][0]["victim_id"], 2)

    async def test_destroying_an_ordinary_ship_ejects_to_an_adjacent_pod(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=2000, shields=10)
        STATE["players_by_id"] = {2: self._defender(ship_type="Bismark",
                                                    fighters=1000, shields=200, credits=5000)}
        attack.random = FakeRandom(choice_index=1)  # Sec15 adjacency [14, 16] -> pick 16

        prompt = await self._aim_and_commit("Bob", "all")

        self.assertIn("destroyed Bob's Bismark", prompt)
        self.assertIn("slip away", prompt)
        self.assertNotIn("Sec", prompt)            # pod's destination is NOT revealed
        d = STATE["players_by_id"][2]
        self.assertEqual(d["ship_type"], "Escape Pod")
        self.assertEqual(d["sector_id"], 16)       # they really did drift to Sec16...
        self.assertEqual(d["credits"], 5000)       # ...credits surviving
        self.assertEqual(STATE["attack_events"][0]["outcome"], "destroyed")
        self.assertEqual(STATE["kills"][-1], {                      # public kill logged
            "victim_name": "Bob", "killer_name": "Tester",
            "sector_id": 15, "kind": "ship",
            "created_at": STATE["kills"][-1]["created_at"],
        })

    async def test_destroying_a_pod_wipes_the_player_back_to_default(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=50, shields=10)
        STATE["players_by_id"] = {2: self._defender(ship_type="Escape Pod",
                                                    fighters=0, shields=0, credits=8000)}

        prompt = await self._aim_and_commit("Bob", "all")

        self.assertIn("blew apart Bob's escape pod", prompt)
        d = STATE["players_by_id"][2]
        self.assertEqual(d["ship_type"], "Falcon")          # back to the default hull
        self.assertEqual(d["credits"], 20000)               # reset to 20k
        self.assertEqual(d["sector_id"], 1)                 # back at the home sector
        self.assertEqual(STATE["attack_events"][0]["outcome"], "pod_destroyed")
        self.assertEqual(STATE["kills"][-1]["killer_name"], "Tester")
        self.assertEqual(STATE["kills"][-1]["victim_name"], "Bob")
        self.assertEqual(STATE["kills"][-1]["kind"], "pod")


class AttackNoticeTests(unittest.IsolatedAsyncioTestCase):
    """Victims get a briefing of attacks against them on their next sign-in."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["kill_log_cutoff"] = {}

    def test_format_lists_each_event_by_outcome_and_time(self):
        events = [
            {"attacker_name": "Bob", "sector_id": 5, "outcome": "attacked",
             "created_at": "2026-06-27T12:00:00+00:00"},
            {"attacker_name": "Cleo", "sector_id": 9, "outcome": "destroyed",
             "created_at": "2026-06-27T13:30:00+00:00"},
            {"attacker_name": "Dax", "sector_id": 1, "outcome": "pod_destroyed",
             "created_at": "2026-06-27T14:00:00+00:00"},
        ]
        out = main.format_attack_notices(events)
        self.assertIn("While you were away:", out)
        self.assertIn("Bob attacked you in Sec5", out)
        self.assertIn("Cleo destroyed your ship in Sec9", out)
        self.assertIn("Dax blew up your escape pod in Sec1", out)
        self.assertIn("2026-06-27 12:00 UTC", out)

    async def test_pending_notices_are_delivered_on_signin(self):
        import types as _types
        import contextlib
        import io

        STATE["player"] = fresh_player(id=1, sector_id=5, turns_remaining=50)
        STATE["attack_events"] = [{
            "victim_id": 1, "attacker_name": "Raider", "sector_id": 5,
            "outcome": "destroyed", "created_at": "2026-06-27T12:00:00+00:00",
        }]

        sent = []

        async def fake_send_reply(mc, pubkey, sender, text):
            sent.append(text)

        main.send_reply = fake_send_reply  # capture instead of hitting the radio

        class _MC:
            def get_contact_by_key_prefix(self, pubkey):
                return {"adv_name": "Victim"}

        event = _types.SimpleNamespace(
            payload={"pubkey_prefix": PUBKEY, "text": "status"}  # no timestamp -> not stale
        )
        with contextlib.redirect_stdout(io.StringIO()):  # mute on_message's debug prints
            await main.on_message(_MC(), event)

        self.assertTrue(sent)
        self.assertIn("While you were away:", sent[0])
        self.assertIn("Raider destroyed your ship in Sec5", sent[0])
        # The notice rides in front of the actual command's reply.
        self.assertIn("Sec5", sent[0])


class KillLogTests(unittest.IsolatedAsyncioTestCase):
    """The public kill log: formatting, the display cap, and sign-in
    delivery scoped to everything since the player last signed in."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["kill_log_cutoff"] = {}

    def _kill(self, victim, killer, sector, kind, when):
        return {"victim_name": victim, "killer_name": killer,
                "sector_id": sector, "kind": kind, "created_at": when}

    def test_format_renders_combat_and_mine_kills(self):
        kills = [
            self._kill("Cleo", "Alice", 9, "pod", "2026-06-27T13:00:00+00:00"),
            self._kill("Dax", None, 20, "ship", "2026-06-27T14:00:00+00:00"),
        ]
        out = main.format_kill_log(kills)
        self.assertIn("Kills since you last played:", out)
        self.assertIn("Alice wiped Cleo's escape pod in Sec9", out)
        self.assertIn("Mines destroyed Dax's ship in Sec20", out)   # None killer -> "Mines"
        self.assertIn("2026-06-27 14:00 UTC", out)

    def test_format_empty_is_blank(self):
        self.assertEqual(main.format_kill_log([]), "")

    def test_format_caps_and_notes_the_overflow(self):
        # One more than the cap: the oldest is dropped and counted.
        n = main.KILL_LOG_MAX_ENTRIES + 3
        kills = [self._kill(f"V{i}", "K", i, "ship", f"2026-06-27T12:00:{i:02d}+00:00")
                 for i in range(n)]
        out = main.format_kill_log(kills)
        self.assertIn("(+3 earlier not shown)", out)
        self.assertNotIn("V0's", out)        # the three oldest are omitted
        self.assertNotIn("V2's", out)
        self.assertIn(f"V{n - 1}'s", out)     # the newest is shown
        # Header + note + exactly the cap's worth of kill lines.
        self.assertEqual(out.count("\n- "), main.KILL_LOG_MAX_ENTRIES)

    async def _signin(self, text="status"):
        """Run on_message as a sign-in, returning the first reply sent."""
        import types as _types
        import contextlib
        import io

        sent = []

        async def fake_send_reply(mc, pubkey, sender, text):
            sent.append(text)

        main.send_reply = fake_send_reply

        class _MC:
            def get_contact_by_key_prefix(self, pubkey):
                return {"adv_name": "Tester"}

        event = _types.SimpleNamespace(payload={"pubkey_prefix": PUBKEY, "text": text})
        with contextlib.redirect_stdout(io.StringIO()):
            await main.on_message(_MC(), event)
        return sent[0] if sent else ""

    async def test_signin_shows_only_kills_since_last_signin(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, turns_remaining=50)

        # Two kills happen, then the player establishes a baseline.
        _stub_record_kill("Bob", "Alice", 42, "ship")
        _stub_record_kill("Eve", "Frank", 7, "ship")
        _stub_mark_kill_log_seen(1)            # cutoff now sits after both
        # Two more happen while they're away.
        _stub_record_kill("Cleo", "Alice", 9, "pod")
        _stub_record_kill("Dax", None, 20, "ship")

        reply = await self._signin()

        self.assertIn("Kills since you last played:", reply)
        self.assertIn("Alice wiped Cleo's escape pod in Sec9", reply)
        self.assertIn("Mines destroyed Dax's ship in Sec20", reply)
        self.assertNotIn("Bob", reply)         # pre-baseline kills excluded
        self.assertNotIn("Eve", reply)

    async def test_signin_advances_cutoff_so_kills_are_not_repeated(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, turns_remaining=50)
        _stub_record_kill("Cleo", "Alice", 9, "pod")
        _stub_mark_kill_log_seen(1)            # baseline after the kill -> nothing new

        first = await self._signin()
        self.assertNotIn("Kills since you last played:", first)  # nothing since baseline

        # A kill lands during their session; next sign-in shows it once...
        _stub_record_kill("Dax", None, 20, "ship")
        main.session.ACTIVE_SESSION = None     # they log off
        second = await self._signin()
        self.assertIn("Mines destroyed Dax's ship in Sec20", second)

        # ...and not again on a later sign-in with nothing new in between.
        main.session.ACTIVE_SESSION = None
        third = await self._signin()
        self.assertNotIn("Kills since you last played:", third)

    async def test_new_player_baseline_hides_kills_from_before_they_joined(self):
        # A brand-new player's cutoff is their join time, so kills logged
        # before that never appear -- the log "begins from when they
        # signed on". Model that: kills exist, THEN the player's baseline
        # is set (as create_player does), then they sign in.
        _stub_record_kill("Bob", "Alice", 42, "ship")   # happened before they joined
        STATE["player"] = fresh_player(id=1, sector_id=15, turns_remaining=50)
        _stub_mark_kill_log_seen(1)                      # join baseline (after the kill)

        reply = await self._signin()
        self.assertNotIn("Kills since you last played:", reply)
        self.assertNotIn("Bob", reply)


class TurnCostTests(unittest.IsolatedAsyncioTestCase):
    """Moving between sectors costs a turn; nothing else does, and a
    player relocated by someone else's attack isn't charged."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["warps"] = chain_warps(30)
        STATE["sector_mines"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["move_log"] = []
        STATE["players_by_id"] = {}
        STATE["attack_events"] = []

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_direct_adjacent_move_costs_one_turn(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, turns_remaining=100)
        await main.cmd_move(self.ctx(), "13")  # 13 is adjacent to 12
        self.assertEqual(STATE["player"]["sector_id"], 13)
        self.assertEqual(STATE["player"]["turns_remaining"], 99)

    async def test_plotting_is_free_but_each_warp_hop_costs_a_turn(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, turns_remaining=100)

        prompt = await main.cmd_move(self.ctx(), "15")  # 3-hop course
        self.assertIn("Plotted", prompt)
        self.assertEqual(STATE["player"]["turns_remaining"], 100)  # plotting costs nothing

        for expected_turns, expected_sector in [(99, 13), (98, 14), (97, 15)]:
            await main.cmd_confirm_warp(self.ctx(), "yes")
            self.assertEqual(STATE["player"]["sector_id"], expected_sector)
            self.assertEqual(STATE["player"]["turns_remaining"], expected_turns)

    async def test_non_move_commands_are_free(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, turns_remaining=100)
        await main.cmd_status(self.ctx(), "")
        await main.cmd_info(self.ctx(), "")
        self.assertEqual(STATE["player"]["turns_remaining"], 100)

    async def test_being_knocked_into_a_pod_by_an_attack_costs_no_turn(self):
        STATE["player"] = fresh_player(id=1, sector_id=5, fighters=2000, turns_remaining=100)
        STATE["players_by_id"] = {2: {
            "id": 2, "name": "Bob", "ship_type": "Bismark", "fighters": 10,
            "shields": 0, "sector_id": 5, "credits": 5000, "turns_remaining": 100,
        }}
        attack.random = FakeRandom(choice_index=0)

        await main.cmd_attack(self.ctx(), "Bob")

        self.assertEqual(STATE["player"]["turns_remaining"], 100)            # attacker didn't move
        self.assertEqual(STATE["players_by_id"][2]["turns_remaining"], 100)  # victim's knockback is free


class StationCommandTests(unittest.IsolatedAsyncioTestCase):
    """Deploying, docking/managing, and fighting space stations."""

    def setUp(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["stations"] = {}
        STATE["next_station_id"] = 1
        STATE["players_by_id"] = {}
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["move_log"] = []
        STATE["defense_log"] = []
        STATE["ship_log"] = []
        STATE["warps"] = {19: [20], 20: [19], 21: [20]}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["sector_mines"] = {}
        STATE["sector_players"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _station(self, owner_id=1, owner_name="Tester", sector=20, **over):
        sid = STATE["next_station_id"]
        STATE["next_station_id"] += 1
        st = {
            "id": sid, "sector_id": sector, "owner_id": owner_id,
            "owner_name": owner_name, "level": 1, "shields": 0, "fighters": 0,
            "shields_enabled": 0, "fuel": 0, "organics": 0, "equipment": 0,
            "posture": "defensive", "engage_pct": 100,
            "last_fuel_burn": "2026-06-27T12:00:00+00:00",
            "upgrade_to": None, "upgrade_started_at": None,
            "credits": 0,
        }
        st.update(over)
        STATE["stations"][sid] = st
        return st

    async def dock(self):
        return await main.cmd_station(self.ctx(), "")

    async def say(self, text):
        return await main.cmd_station_step(self.ctx(), text)

    # --- deploy ---
    async def test_deploy_requires_a_kit(self):
        STATE["player"] = fresh_player(sector_id=20, station_core=0)
        self.assertIn("not carrying", await main.cmd_deploy(self.ctx(), ""))

    async def test_deploy_blocked_in_safe_zone(self):
        STATE["player"] = fresh_player(sector_id=5, station_core=1)
        self.assertIn("safe zone", await main.cmd_deploy(self.ctx(), ""))

    async def test_deploy_one_per_sector(self):
        STATE["player"] = fresh_player(sector_id=20, station_core=1)
        self._station(owner_id=2, owner_name="Zara", sector=20)
        self.assertIn("already a space station", await main.cmd_deploy(self.ctx(), ""))

    async def test_deploy_success_consumes_kit(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, station_core=1)
        prompt = await main.cmd_deploy(self.ctx(), "")
        self.assertIn("Deployed Space Station - Tester in Sec20", prompt)
        self.assertEqual(STATE["player"]["station_core"], 0)        # kit consumed
        st = main.get_station_in_sector(20)
        self.assertEqual((st["owner_id"], st["level"]), (1, 1))

    # --- info screen ---
    async def test_info_shows_station_with_fighters_not_shields(self):
        STATE["player"] = fresh_player(id=1, sector_id=20)
        self._station(owner_id=2, owner_name="Zara", sector=20, fighters=7, shields=999)
        prompt = await main.cmd_info(self.ctx(), "")
        self.assertIn("Space Station - Zara (7 ftr)", prompt)
        self.assertNotIn("999", prompt)

    # --- docking access ---
    async def test_dock_nonowner_is_denied(self):
        STATE["player"] = fresh_player(id=1, sector_id=20)
        self._station(owner_id=2, owner_name="Zara", sector=20)
        prompt = await self.dock()
        self.assertIn("isn't yours", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_STATIONS)

    async def test_dock_owner_opens_menu(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station(owner_id=1, owner_name="Tester", sector=20)
        prompt = await self.dock()
        self.assertIn("Station options:", prompt)
        self.assertIn(PUBKEY, main.PENDING_STATIONS)

    async def test_dock_no_station_here(self):
        STATE["player"] = fresh_player(id=1, sector_id=20)
        self.assertIn("no space station", await self.dock())

    # --- management ---
    async def test_deposit_all_cargo(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       fuel_ore=100, organics=50, equipment=25)
        st = self._station(owner_id=1, owner_name="Tester", sector=20)
        await self.dock()
        prompt = await self.say("1")
        self.assertIn("Deposited 100 fuel, 50 organics, 25 equipment", prompt)
        self.assertEqual((st["fuel"], st["organics"], st["equipment"]), (100, 50, 25))
        self.assertEqual((STATE["player"]["fuel_ore"], STATE["player"]["organics"]), (0, 0))

    async def test_transfer_fighters(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, fighters=200)
        st = self._station(owner_id=1, owner_name="Tester", sector=20)
        await self.dock()
        await self.say("2")
        prompt = await self.say("150")
        self.assertIn("Transferred 150 fighters", prompt)
        self.assertEqual(st["fighters"], 150)
        self.assertEqual(STATE["player"]["fighters"], 50)

    async def test_enable_shields_needs_fuel_then_powers_up(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        st = self._station(owner_id=1, owner_name="Tester", sector=20, fuel=50)
        await self.dock()
        self.assertIn("Not enough fuel", await self.say("3"))   # 50 < 100/day
        st["fuel"] = 100
        prompt = await self.say("3")
        self.assertIn("Shields online at 1000 (100 fuel/day)", prompt)
        self.assertEqual((st["shields_enabled"], st["shields"]), (1, 1000))

    async def test_disable_shields(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        st = self._station(owner_id=1, owner_name="Tester", sector=20,
                           shields_enabled=1, shields=1000, fuel=500)
        await self.dock()
        prompt = await self.say("3")
        self.assertIn("powered down", prompt)
        self.assertEqual((st["shields_enabled"], st["shields"]), (0, 0))

    async def test_set_posture_offensive_with_pct(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        st = self._station(owner_id=1, owner_name="Tester", sector=20)
        await self.dock()
        await self.say("4")
        await self.say("o")
        prompt = await self.say("50")
        self.assertIn("offensive, engaging 50%", prompt)
        self.assertEqual((st["posture"], st["engage_pct"]), ("offensive", 50))

    async def test_upgrade_short_on_resources(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, credits=0)
        self._station(owner_id=1, owner_name="Tester", sector=20)
        await self.dock()
        prompt = await self.say("5")
        self.assertIn("short:", prompt)

    async def test_upgrade_starts_when_affordable(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, credits=10_000_000)
        st = self._station(owner_id=1, owner_name="Tester", sector=20,
                           fuel=2500, organics=2000, equipment=1000)
        await self.dock()
        self.assertIn("Confirm? yes/no", await self.say("5"))
        prompt = await self.say("yes")
        self.assertIn("Upgrade to Lvl 2 started", prompt)
        self.assertEqual(STATE["player"]["credits"], 0)
        self.assertEqual(st["upgrade_to"], 2)
        self.assertEqual((st["fuel"], st["organics"], st["equipment"]), (0, 0, 0))

    # --- attacking a station ---
    async def test_attack_station_hit_not_destroyed(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, fighters=100)
        st = self._station(owner_id=2, owner_name="Zara", sector=20, fighters=50, shields=2000)
        await main.cmd_attack(self.ctx(), "station")
        prompt = await main.cmd_attack_step(self.ctx(), "100")
        self.assertIn("You hit Space Station - Zara", prompt)
        self.assertEqual(st["fighters"], 0)
        self.assertEqual(st["shields"], 1380)        # 2000 - 62*10
        self.assertEqual(STATE["attack_events"], [])  # only a hit, no destroy notice

    async def test_attack_station_destroyed_notifies_owner(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, fighters=100)
        self._station(owner_id=2, owner_name="Zara", sector=20, fighters=50, shields=0)
        await main.cmd_attack(self.ctx(), "station")
        prompt = await main.cmd_attack_step(self.ctx(), "100")
        self.assertIn("You destroyed Space Station - Zara", prompt)
        self.assertEqual(main.get_station_in_sector(20), None)   # removed
        self.assertEqual(STATE["attack_events"][0]["victim_id"], 2)
        self.assertEqual(STATE["attack_events"][0]["outcome"], "station_destroyed")
        self.assertEqual(STATE["kills"], [])                     # stations aren't kill-logged

    async def test_cannot_attack_your_own_station(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20, fighters=100)
        self._station(owner_id=1, owner_name="Tester", sector=20, fighters=50)
        self.assertIn("No other ships here", await main.cmd_attack(self.ctx(), ""))
        self.assertIn("no enemy station", await main.cmd_attack(self.ctx(), "station"))

    # --- offensive station fires on entry ---
    async def test_offensive_station_damages_entrant(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=19,
                                       fighters=2000, shields=5000, turns_remaining=50)
        st = self._station(owner_id=2, owner_name="Zara", sector=20,
                           posture="offensive", engage_pct=100, fighters=100, shields=0)
        prompt = await main.cmd_move(self.ctx(), "20")
        self.assertIn("Space Station - Zara opens fire!", prompt)
        self.assertIn("You're left with 1867 fighters, 5000 shields", prompt)
        self.assertEqual(st["fighters"], 0)            # committed 100, none survived
        self.assertEqual(STATE["player"]["sector_id"], 20)  # survived, still there

    async def test_offensive_station_can_destroy_entrant(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=19,
                                       fighters=10, shields=0, turns_remaining=50)
        self._station(owner_id=2, owner_name="Zara", sector=20,
                      posture="offensive", engage_pct=100, fighters=1000, shields=0)
        prompt = await main.cmd_move(self.ctx(), "20")
        self.assertIn("Space Station - Zara opens fire as you arrive!", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Escape Pod")   # ejected
        self.assertEqual(STATE["kills"][-1]["killer_name"], "Space Station - Zara")
        self.assertEqual(STATE["kills"][-1]["kind"], "ship")

    async def test_defensive_station_ignores_entrant(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=19,
                                       fighters=10, turns_remaining=50)
        self._station(owner_id=2, owner_name="Zara", sector=20,
                      posture="defensive", fighters=1000)
        prompt = await main.cmd_move(self.ctx(), "20")
        self.assertIn("Moved to Sec20", prompt)
        self.assertNotIn("opens fire", prompt)

    async def test_own_offensive_station_never_fires_on_owner(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=19,
                                       fighters=10, turns_remaining=50)
        self._station(owner_id=1, owner_name="Tester", sector=20,
                      posture="offensive", fighters=1000)
        prompt = await main.cmd_move(self.ctx(), "20")
        self.assertNotIn("opens fire", prompt)
        self.assertEqual(STATE["player"]["fighters"], 10)  # untouched


def _parked(ship_id, owner_id, owner_name, sector_id, ship_type="Kestrel",
            fighters=0, shields=0, **extra):
    """A parked-ship fixture shaped like db.get_parked_ships_in_sector's
    rows, with any full-ship stats a board test needs mixed in."""
    ship = {
        "id": ship_id, "owner_id": owner_id, "owner_name": owner_name,
        "sector_id": sector_id, "ship_type": ship_type,
        "fighters": fighters, "shields": shields,
        "docked_station_id": None,
    }
    ship.update(extra)
    return ship


class UnmannedSectorInfoTests(unittest.IsolatedAsyncioTestCase):
    """The sector-info "Unmanned:" line for parked ships."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["sector_players"] = {}
        STATE["stations"] = {}
        STATE["warps"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_parked_ships_listed_with_owner_type_id_and_fighters(self):
        STATE["player"] = fresh_player(id=1, sector_id=15)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=15,
                        ship_type="Kestrel", fighters=5),
            7: _parked(7, owner_id=3, owner_name="Cleo", sector_id=15,
                       ship_type="Mule", fighters=0),
        }
        info = await main.cmd_info(self.ctx(), "")
        self.assertIn("Unmanned: Cleo's Mule #7 (0 ftr), Bob's Kestrel #12 (5 ftr)", info)

    async def test_viewers_own_parked_ship_is_listed_too(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=1, owner_name="Tester", sector_id=15),
        }
        info = await main.cmd_info(self.ctx(), "")
        self.assertIn("Unmanned: Tester's Kestrel #12 (0 ftr)", info)

    async def test_no_unmanned_line_when_sector_has_no_parked_ships(self):
        STATE["player"] = fresh_player(id=1, sector_id=15)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=16),  # elsewhere
        }
        info = await main.cmd_info(self.ctx(), "")
        self.assertNotIn("Unmanned:", info)


class AttackUnmannedShipTests(unittest.IsolatedAsyncioTestCase):
    """Attacking parked (unmanned) ships: targeting by #id, the guided
    cycle offering them after pilots, and resolution (same shield/fighter
    math; a kill deletes the hull and notifies the owner)."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["defense_log"] = []
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["players_by_id"] = {}
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_attack_parked_ship_by_id(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=15,
                        fighters=10, shields=20),
        }
        prompt = await main.cmd_attack(self.ctx(), "#12")
        self.assertIn("Attack Bob's Kestrel #12 with how many fighters", prompt)
        self.assertIn(PUBKEY, main.PENDING_ATTACKS)

    async def test_cannot_attack_your_own_parked_ship(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15, fighters=500)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=1, owner_name="Tester", sector_id=15),
        }
        prompt = await main.cmd_attack(self.ctx(), "#12")
        self.assertIn("your own ship", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)

    async def test_bare_attack_offers_unmanned_ship_after_pilots(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, fighters=500)
        STATE["players_by_id"] = {
            2: {"id": 2, "name": "Bob", "ship_type": "Bismark",
                "fighters": 1000, "shields": 200, "sector_id": 15, "credits": 0},
        }
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=3, owner_name="Cleo", sector_id=15,
                        fighters=5, shields=0),
        }
        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("Attack Bob (1000 ftr)? yes/no", prompt)
        prompt = await main.cmd_attack_step(self.ctx(), "no")
        self.assertIn("Attack Cleo's Kestrel #12 (5 ftr)? yes/no", prompt)
        prompt = await main.cmd_attack_step(self.ctx(), "yes")
        self.assertIn("Attack Cleo's Kestrel #12 with how many fighters", prompt)

    async def test_own_parked_ships_never_offered_in_the_cycle(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15, fighters=500)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=1, owner_name="Tester", sector_id=15),
        }
        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("No other ships here to attack", prompt)

    async def test_destroying_an_unmanned_ship_deletes_it_and_notifies_owner(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15, fighters=500)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=15,
                        fighters=10, shields=20),
        }
        await main.cmd_attack(self.ctx(), "#12")
        report = await main.cmd_attack_step(self.ctx(), "all")

        # ceil(0.75*10)=8 to clear fighters, ceil(20/10)=2 for shields ->
        # 10 engaged fighters spent, 490 survive.
        self.assertIn("You destroyed the unmanned Bob's Kestrel #12!", report)
        self.assertIn("You have 490 fighters", report)
        self.assertNotIn(12, STATE["parked_ships"])          # hull is gone
        self.assertEqual(STATE["ship_delete_log"], [12])
        self.assertEqual(STATE["attack_events"][0]["victim_id"], 2)
        self.assertEqual(STATE["attack_events"][0]["outcome"], "unmanned_destroyed")
        self.assertEqual(STATE["kills"], [])                 # not in the public log

    async def test_damaging_an_unmanned_ship_writes_back_its_defenses(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15, fighters=10)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=15,
                        fighters=100, shields=50),
        }
        await main.cmd_attack(self.ctx(), "#12")
        report = await main.cmd_attack_step(self.ctx(), "9")

        # 9 attackers kill floor(9/0.75)=12 of 100 fighters, never reach
        # shields: the hull survives at 88 fighters / 50 shields.
        self.assertIn("You hit the unmanned Bob's Kestrel #12", report)
        self.assertEqual(STATE["parked_ships"][12]["fighters"], 88)
        self.assertEqual(STATE["parked_ships"][12]["shields"], 50)
        self.assertEqual(STATE["attack_events"][0]["outcome"], "unmanned_attacked")
        self.assertIn(12, STATE["parked_ships"])

    async def test_unmanned_notices_read_correctly_at_sign_in(self):
        events = [
            {"attacker_name": "Tester", "sector_id": 15,
             "outcome": "unmanned_destroyed", "created_at": "2026-06-27T12:00:00+00:00"},
            {"attacker_name": "Tester", "sector_id": 15,
             "outcome": "unmanned_attacked", "created_at": "2026-06-27T12:00:00+00:00"},
        ]
        text = main.format_attack_notices(events)
        self.assertIn("Tester destroyed your unmanned ship in Sec15", text)
        self.assertIn("Tester attacked your unmanned ship in Sec15", text)

    async def test_parked_ships_in_the_safe_zone_are_protected(self):
        STATE["player"] = fresh_player(id=1, sector_id=5, fighters=500)
        STATE["parked_ships"] = {
            12: _parked(12, owner_id=2, owner_name="Bob", sector_id=5),
        }
        prompt = await main.cmd_attack(self.ctx(), "#12")
        self.assertIn("safe zone", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_ATTACKS)


class TowCommandTests(unittest.IsolatedAsyncioTestCase):
    """The tow command: engaging (guided and by #id), ownership checks,
    the 5-turns-per-sector cost, refusals, and release."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["sector_players"] = {}
        STATE["players_by_id"] = {}
        STATE["stations"] = {}
        STATE["sector_mines"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _own_ship(self, ship_id=12, sector_id=15, **extra):
        STATE["parked_ships"][ship_id] = _parked(
            ship_id, owner_id=1, owner_name="Tester", sector_id=sector_id, **extra
        )

    async def test_engaging_a_tow_warns_about_the_turn_cost(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       turns_remaining=50)
        self._own_ship(12)
        prompt = await main.cmd_tow(self.ctx(), "#12")
        self.assertIn("Tow line attached to Tester's Kestrel #12", prompt)
        self.assertIn("WARNING: towing burns 5 turns per sector", prompt)
        self.assertEqual(STATE["player"]["towing_ship_id"], 12)

    async def test_cannot_tow_a_ship_you_do_not_own(self):
        STATE["player"] = fresh_player(id=1, sector_id=15)
        STATE["parked_ships"][12] = _parked(12, owner_id=2, owner_name="Bob",
                                            sector_id=15)
        prompt = await main.cmd_tow(self.ctx(), "#12")
        self.assertIn("isn't yours -- you can only tow your own ships", prompt)
        self.assertIsNone(STATE["player"]["towing_ship_id"])

    async def test_guided_tow_cycles_own_ships_and_engages_on_yes(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15)
        self._own_ship(12, ship_type="Kestrel")
        self._own_ship(13, ship_type="Mule")
        # A stranger's hull parked here never enters the cycle.
        STATE["parked_ships"][14] = _parked(14, owner_id=2, owner_name="Bob",
                                            sector_id=15)

        prompt = await main.cmd_tow(self.ctx(), "")
        self.assertIn("Tow your Kestrel #12? yes/no", prompt)
        prompt = await main.cmd_tow_step(self.ctx(), "no")
        self.assertIn("Tow your Mule #13? yes/no", prompt)
        prompt = await main.cmd_tow_step(self.ctx(), "yes")
        self.assertIn("Tow line attached to Tester's Mule #13", prompt)
        self.assertEqual(STATE["player"]["towing_ship_id"], 13)
        self.assertNotIn(PUBKEY, main.PENDING_TOWS)

    async def test_declining_every_ship_calls_the_tow_off(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15)
        self._own_ship(12)
        await main.cmd_tow(self.ctx(), "")
        prompt = await main.cmd_tow_step(self.ctx(), "no")
        self.assertIn("no more of your ships here", prompt)
        self.assertIsNone(STATE["player"]["towing_ship_id"])
        self.assertNotIn(PUBKEY, main.PENDING_TOWS)

    async def test_an_escape_pod_cannot_tow(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, ship_type="Escape Pod")
        self._own_ship(12)
        prompt = await main.cmd_tow(self.ctx(), "")
        self.assertIn("escape pod has no tow rig", prompt)

    async def test_moving_while_towing_costs_five_turns_and_drags_the_hull(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       turns_remaining=50, towing_ship_id=12)
        self._own_ship(12, sector_id=15)

        prompt = await main.cmd_move(self.ctx(), "16")

        self.assertIn("Moved to Sec16.", prompt)
        self.assertIn("Towing Tester's Kestrel #12: -5 turns", prompt)
        self.assertEqual(STATE["player"]["turns_remaining"], 45)
        self.assertEqual(STATE["parked_ships"][12]["sector_id"], 16)  # dragged along
        self.assertEqual(STATE["ship_move_log"], [(12, 16)])
        # The towed hull shows on the arrival's sector info too.
        self.assertIn("Unmanned: Tester's Kestrel #12", prompt)

    async def test_move_is_refused_when_towing_with_too_few_turns(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       turns_remaining=3, towing_ship_id=12)
        self._own_ship(12, sector_id=15)

        prompt = await main.cmd_move(self.ctx(), "16")

        self.assertIn("Towing burns 5 turns per sector and you have 3 left", prompt)
        self.assertEqual(STATE["player"]["sector_id"], 15)      # never moved
        self.assertEqual(STATE["player"]["turns_remaining"], 3)  # nothing spent
        self.assertEqual(STATE["parked_ships"][12]["sector_id"], 15)

    async def test_tow_again_releases_and_the_hull_stays_put(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=16,
                                       towing_ship_id=12)
        self._own_ship(12, sector_id=16)
        prompt = await main.cmd_tow(self.ctx(), "")
        self.assertIn("Tow released -- Tester's Kestrel #12 stays parked in Sec16", prompt)
        self.assertIsNone(STATE["player"]["towing_ship_id"])

    async def test_status_shows_the_tow(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       towing_ship_id=12)
        self._own_ship(12, sector_id=15)
        status = await main.cmd_status(self.ctx(), "")
        self.assertIn("Towing Kestrel #12 (5 turns/sector)", status)

    async def test_p2p_is_refused_while_towing(self):
        STATE["player"] = fresh_player(id=1, sector_id=15, towing_ship_id=12)
        self._own_ship(12, sector_id=15)
        prompt = await main.cmd_p2p(self.ctx(), "")
        self.assertIn("Can't run a P2P shuttle while towing", prompt)


class BoardCommandTests(unittest.IsolatedAsyncioTestCase):
    """The board/swap command: taking the helm of your own parked ship,
    parking the old hull in its place (or scuttling an escape pod)."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["players_by_id"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _spare(self, ship_id=12, sector_id=15, **extra):
        defaults = dict(ship_type="Kestrel", holds_total=15, fighters=20,
                        shields=20, mines=0, probes=3, fuel_ore=4, organics=0,
                        equipment=0, station_core=0)
        defaults.update(extra)
        STATE["parked_ships"][ship_id] = _parked(
            ship_id, owner_id=1, owner_name="Tester", sector_id=sector_id, **defaults
        )

    async def test_boarding_swaps_hulls_and_parks_the_old_one(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       ship_type="Falcon", ship_id=50,
                                       holds_total=20, fighters=10, shields=10,
                                       fuel_ore=7)
        self._spare(12)

        prompt = await main.cmd_board(self.ctx(), "#12")

        self.assertIn("You board your Kestrel #12", prompt)
        self.assertIn("your Falcon #50 is parked here", prompt)
        self.assertIn("Aboard: 20 ftr, 20 shd, cargo f4/o0/e0", prompt)
        p = STATE["player"]
        self.assertEqual(p["ship_type"], "Kestrel")
        self.assertEqual(p["ship_id"], 12)
        self.assertEqual(p["fighters"], 20)
        self.assertEqual(p["fuel_ore"], 4)       # the spare's cargo, as left
        old = STATE["parked_ships"][50]
        self.assertEqual(old["ship_type"], "Falcon")
        self.assertEqual(old["sector_id"], 15)
        self.assertEqual(old["fuel_ore"], 7)     # old hull keeps its cargo

    async def test_a_pod_pilot_boarding_scuttles_the_pod(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       ship_type="Escape Pod", ship_id=50,
                                       holds_total=0, fighters=0, shields=0)
        self._spare(12)

        prompt = await main.cmd_board(self.ctx(), "#12")

        self.assertIn("your escape pod is scuttled", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Kestrel")
        self.assertNotIn(50, STATE["parked_ships"])  # no pod left behind

    async def test_cannot_board_a_ship_you_do_not_own(self):
        STATE["player"] = fresh_player(id=1, sector_id=15)
        STATE["parked_ships"][12] = _parked(12, owner_id=2, owner_name="Bob",
                                            sector_id=15)
        prompt = await main.cmd_board(self.ctx(), "#12")
        self.assertIn("isn't yours -- you can't board it", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")

    async def test_boarding_releases_a_tow(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15,
                                       towing_ship_id=13)
        self._spare(12)
        self._spare(13, ship_type="Mule")

        await main.cmd_board(self.ctx(), "#12")

        self.assertIsNone(STATE["player"]["towing_ship_id"])

    async def test_guided_board_cycles_own_ships(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=15)
        self._spare(12, ship_type="Kestrel")
        self._spare(13, ship_type="Mule")

        prompt = await main.cmd_board(self.ctx(), "")
        self.assertIn("Board your Kestrel #12? yes/no", prompt)
        prompt = await main.cmd_board_step(self.ctx(), "no")
        self.assertIn("Board your Mule #13? yes/no", prompt)
        prompt = await main.cmd_board_step(self.ctx(), "yes")
        self.assertIn("You board your Mule #13", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_BOARDS)

    async def test_nothing_of_yours_here_to_board(self):
        STATE["player"] = fresh_player(id=1, sector_id=15)
        prompt = await main.cmd_board(self.ctx(), "")
        self.assertIn("No unmanned ships here to board", prompt)


class ShipyardMultiShipTests(unittest.IsolatedAsyncioTestCase):
    """The shipyard's multi-ship additions: buying while KEEPING the old
    hull (parked at the Stardock, full price), and selling a parked hull
    outright via its s<id> menu entry."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def enter_shipyard(self):
        await self.dock()
        # Shipyard is always one past the refit options (5 for a Falcon).
        return await self.say("5")

    async def test_buying_and_keeping_parks_the_old_ship_at_full_price(self):
        STATE["player"] = fresh_player(credits=250000, ship_type="Falcon",
                                       ship_id=50, fighters=10, shields=10,
                                       fuel_ore=3)
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("6")  # SS Endeavour -> sell-or-keep question
        prompt = await self.say("keep")
        self.assertIn(
            "Buy the SS Endeavour for 200000cr and keep your Falcon parked in Sec1? yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn(
            "Welcome aboard the SS Endeavour! -200000cr. Your Falcon is parked in Sec1.",
            prompt,
        )

        p = STATE["player"]
        self.assertEqual(p["ship_type"], "SS Endeavour")
        self.assertEqual(p["credits"], 250000 - 200000)   # FULL price, no trade-in
        self.assertEqual(p["fuel_ore"], 0)                # new hull starts empty
        # The old Falcon is parked right here with everything aboard.
        old = STATE["parked_ships"][50]
        self.assertEqual(old["ship_type"], "Falcon")
        self.assertEqual(old["sector_id"], 1)
        self.assertEqual(old["fighters"], 10)
        self.assertEqual(old["fuel_ore"], 3)
        self.assertEqual(
            STATE["park_buy_log"],
            [("SS Endeavour", 50, 0, 50, 0, -200000, 1)],
        )
        self.assertEqual(STATE["ship_log"], [])  # not an in-place hull swap

    async def test_keep_is_refused_when_the_full_price_cannot_be_afforded(self):
        # 198000cr affords the Endeavour only WITH the Kestrel trade-in
        # (net 196000), not at the full 200000cr price.
        STATE["player"] = fresh_player(credits=198000, ship_type="Kestrel",
                                       holds_total=15, fighters=20, shields=20)
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("6")
        self.assertIn("Reply sell/keep (or cancel).", prompt)

        prompt = await self.say("keep")
        self.assertIn("Can't afford to keep your Kestrel", prompt)
        self.assertIn("full 200000cr", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Kestrel")  # unchanged
        self.assertEqual(STATE["park_buy_log"], [])
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_a_pod_pilot_is_never_asked_to_keep(self):
        STATE["player"] = fresh_player(credits=10000, ship_type="Escape Pod",
                                       holds_total=0, fighters=0, shields=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("2")  # pod refits: Shields/Probes -> shipyard is 3... use menu text instead
        # A pod has only Shields + Probes refit lines, so the shipyard
        # option number varies; drive it via the state directly.
        main.PENDING_UPGRADES[PUBKEY] = {"stage": "shipyard_menu"}
        prompt = await self.say("2")  # Kestrel
        self.assertIn(
            "Trade in your Escape Pod (0cr) for a Kestrel (8000cr)? Net cost: 8000cr. yes/no",
            prompt,
        )

    async def test_shipyard_menu_lists_parked_ships_for_sale(self):
        STATE["player"] = fresh_player(id=1, name="Tester", credits=1000,
                                       ship_type="Falcon", sector_id=1)
        STATE["parked_ships"][12] = _parked(12, owner_id=1, owner_name="Tester",
                                            sector_id=1, ship_type="Kestrel")
        # Someone else's hull parked here is NOT for sale on your menu.
        STATE["parked_ships"][13] = _parked(13, owner_id=2, owner_name="Bob",
                                            sector_id=1, ship_type="Mule")
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.enter_shipyard()
        self.assertIn("s12) Sell your parked Kestrel #12 -- 4000cr", prompt)
        self.assertNotIn("s13)", prompt)

    async def test_selling_a_parked_ship(self):
        STATE["player"] = fresh_player(id=1, name="Tester", credits=1000,
                                       ship_type="Falcon", sector_id=1)
        STATE["parked_ships"][12] = _parked(12, owner_id=1, owner_name="Tester",
                                            sector_id=1, ship_type="Kestrel")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("s12")
        self.assertIn(
            "Sell your parked Kestrel #12 for 4000cr? Anything still aboard it is lost. yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Sold your parked Kestrel #12. +4000cr.", prompt)
        self.assertNotIn(12, STATE["parked_ships"])
        self.assertEqual(STATE["ship_delete_log"], [12])
        self.assertEqual(STATE["player"]["credits"], 5000)
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")  # helm unchanged

    async def test_selling_the_hull_you_are_towing_releases_the_tow(self):
        STATE["player"] = fresh_player(id=1, name="Tester", credits=0,
                                       ship_type="Falcon", sector_id=1,
                                       towing_ship_id=12)
        STATE["parked_ships"][12] = _parked(12, owner_id=1, owner_name="Tester",
                                            sector_id=1, ship_type="Kestrel")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("s12")
        prompt = await self.say("yes")

        self.assertIn("Sold your parked Kestrel #12", prompt)
        self.assertIsNone(STATE["player"]["towing_ship_id"])

    async def test_cannot_sell_someone_elses_parked_ship(self):
        STATE["player"] = fresh_player(id=1, name="Tester", credits=0,
                                       ship_type="Falcon", sector_id=1)
        STATE["parked_ships"][13] = _parked(13, owner_id=2, owner_name="Bob",
                                            sector_id=1, ship_type="Mule")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("s13")
        self.assertIn("No parked ship of yours here with id #13", prompt)
        self.assertIn(13, STATE["parked_ships"])



class StationDockingTests(unittest.IsolatedAsyncioTestCase):
    """Sheltering spare ships in a station's docking bays: the station
    menu dock/undock flows, capacity per level (2 x level), the
    protection docked hulls get from attack/tow/board and the Unmanned
    line, and going down with the station when it's destroyed."""

    def setUp(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["stations"] = {}
        STATE["next_station_id"] = 1
        STATE["players_by_id"] = {}
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["defense_log"] = []
        STATE["warps"] = {19: [20], 20: [19, 21], 21: [20]}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["sector_mines"] = {}
        STATE["sector_players"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _station(self, owner_id=1, owner_name="Tester", sector=20, **over):
        sid = STATE["next_station_id"]
        STATE["next_station_id"] += 1
        st = {
            "id": sid, "sector_id": sector, "owner_id": owner_id,
            "owner_name": owner_name, "level": 1, "shields": 0, "fighters": 0,
            "shields_enabled": 0, "fuel": 0, "organics": 0, "equipment": 0,
            "posture": "defensive", "engage_pct": 100,
            "last_fuel_burn": "2026-06-27T12:00:00+00:00",
            "upgrade_to": None, "upgrade_started_at": None,
            "credits": 0,
        }
        st.update(over)
        STATE["stations"][sid] = st
        return st

    def _spare(self, ship_id, sector_id=20, ship_type="Kestrel", **extra):
        STATE["parked_ships"][ship_id] = _parked(
            ship_id, owner_id=1, owner_name="Tester", sector_id=sector_id,
            ship_type=ship_type, **extra
        )

    async def dock(self):
        return await main.cmd_station(self.ctx(), "")

    async def say(self, text):
        return await main.cmd_station_step(self.ctx(), text)

    # --- the menu & docking flow ---

    async def test_menu_advertises_the_docking_bays(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        menu = await self.dock()
        self.assertIn("6) Dock a spare ship (0/2 bays used)", menu)
        self.assertNotIn("7) Undock", menu)   # nothing docked yet

    async def test_docking_a_spare_shelters_it(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        self._spare(12)

        await self.dock()
        prompt = await self.say("6")
        self.assertIn("Dock your Kestrel #12? yes/no", prompt)

        reply = await self.say("yes")
        self.assertIn("Docked your Kestrel #12 -- it's sheltered from attack", reply)
        self.assertEqual(STATE["parked_ships"][12]["docked_station_id"], 1)
        self.assertIn("Docked ships (1/2): Kestrel #12", reply)  # station status
        self.assertIn("7) Undock a ship", reply)                 # menu grew

    async def test_dock_all_stops_at_capacity(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station(level=1)                       # 2 bays
        self._spare(12)
        self._spare(13, ship_type="Mule")
        self._spare(14, ship_type="Falcon")

        await self.dock()
        await self.say("6")
        reply = await self.say("all")

        self.assertIn("Docked Kestrel #12, Mule #13 at the station. Bays are now full.", reply)
        self.assertEqual(STATE["parked_ships"][12]["docked_station_id"], 1)
        self.assertEqual(STATE["parked_ships"][13]["docked_station_id"], 1)
        self.assertIsNone(STATE["parked_ships"][14]["docked_station_id"])  # no room

    async def test_capacity_scales_with_level(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station(level=3)                       # 6 bays
        menu = await self.dock()
        self.assertIn("6) Dock a spare ship (0/6 bays used)", menu)

    async def test_dock_refused_when_bays_are_full(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station(level=1)
        self._spare(12, docked_station_id=1)
        self._spare(13, ship_type="Mule", docked_station_id=1)
        self._spare(14)

        await self.dock()
        reply = await self.say("6")
        self.assertIn("Docking bays are full (2/2)", reply)
        self.assertIsNone(STATE["parked_ships"][14]["docked_station_id"])

    async def test_nothing_here_to_dock(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        await self.dock()
        reply = await self.say("6")
        self.assertIn("No spare ships of yours out in this sector to dock", reply)

    async def test_undocking_returns_the_ship_to_the_open(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        self._spare(12, docked_station_id=1)

        await self.dock()
        prompt = await self.say("7")
        self.assertIn("Undock your Kestrel #12? yes/no", prompt)
        reply = await self.say("yes")
        self.assertIn("Undocked your Kestrel #12 -- parked out in Sec20 (attackable again)", reply)
        self.assertIsNone(STATE["parked_ships"][12]["docked_station_id"])

    async def test_docking_releases_a_tow_on_that_hull(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       towing_ship_id=12)
        self._station()
        self._spare(12)

        await self.dock()
        await self.say("6")
        await self.say("yes")

        self.assertIsNone(STATE["player"]["towing_ship_id"])
        self.assertEqual(STATE["parked_ships"][12]["docked_station_id"], 1)

    # --- the protection itself ---

    async def test_docked_ships_vanish_from_the_unmanned_line_but_show_as_docked(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        self._spare(12, docked_station_id=1)
        self._spare(13, ship_type="Mule")            # out in the open

        info = await main.cmd_info(self.ctx(), "")

        self.assertIn("Unmanned: Tester's Mule #13", info)
        self.assertNotIn("Kestrel #12", info.split("Docked:")[0])  # not on Unmanned
        self.assertIn("Docked: Tester's Kestrel #12", info)        # visible as docked

    async def test_docked_ships_cannot_be_attacked(self):
        STATE["player"] = fresh_player(id=2, name="Rival", sector_id=20, fighters=500)
        self._station(owner_id=1, owner_name="Tester")
        STATE["parked_ships"][12] = _parked(12, owner_id=1, owner_name="Tester",
                                            sector_id=20, docked_station_id=1)

        prompt = await main.cmd_attack(self.ctx(), "#12")
        self.assertIn("No unmanned ship #12 here", prompt)

        # And the guided cycle only offers the station, never the docked hull.
        prompt = await main.cmd_attack(self.ctx(), "")
        self.assertIn("Attack Space Station - Tester", prompt)
        self.assertNotIn("Kestrel #12", prompt)

    async def test_docked_ships_cannot_be_towed_or_boarded(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station()
        self._spare(12, docked_station_id=1)

        self.assertIn("No unmanned ship #12 here",
                      await main.cmd_tow(self.ctx(), "#12"))
        self.assertIn("No unmanned ship #12 here",
                      await main.cmd_board(self.ctx(), "#12"))
        self.assertEqual(STATE["parked_ships"][12]["docked_station_id"], 1)

    # --- going down with the station ---

    async def test_docked_ships_are_destroyed_with_the_station(self):
        STATE["player"] = fresh_player(id=2, name="Rival", sector_id=20,
                                       fighters=500)
        self._station(owner_id=1, owner_name="Tester", fighters=10, shields=20)
        STATE["parked_ships"][12] = _parked(12, owner_id=1, owner_name="Tester",
                                            sector_id=20, docked_station_id=1)
        STATE["parked_ships"][13] = _parked(13, owner_id=1, owner_name="Tester",
                                            sector_id=20, ship_type="Mule",
                                            docked_station_id=1)

        await main.cmd_attack(self.ctx(), "station")
        report = await main.cmd_attack_step(self.ctx(), "all")

        self.assertIn("You destroyed Space Station - Tester!", report)
        self.assertIn("2 docked ships went down with it.", report)
        self.assertNotIn(12, STATE["parked_ships"])
        self.assertNotIn(13, STATE["parked_ships"])
        outcomes = [e["outcome"] for e in STATE["attack_events"]]
        self.assertEqual(outcomes.count("station_destroyed"), 1)
        self.assertEqual(outcomes.count("unmanned_destroyed"), 2)
        self.assertTrue(all(e["victim_id"] == 1 for e in STATE["attack_events"]))

    async def test_undocked_ships_do_not_share_the_stations_fate(self):
        STATE["player"] = fresh_player(id=2, name="Rival", sector_id=20,
                                       fighters=500)
        self._station(owner_id=1, owner_name="Tester", fighters=0, shields=0)
        STATE["parked_ships"][13] = _parked(13, owner_id=1, owner_name="Tester",
                                            sector_id=20, ship_type="Mule")

        await main.cmd_attack(self.ctx(), "station")
        report = await main.cmd_attack_step(self.ctx(), "all")

        self.assertIn("You destroyed Space Station - Tester!", report)
        self.assertNotIn("went down with it", report)
        self.assertIn(13, STATE["parked_ships"])     # still out there, still loot


class StationTreasuryTests(unittest.IsolatedAsyncioTestCase):
    """The station treasury: depositing and withdrawing credits through
    the station menu, validation, the status line, and the treasury
    burning with the station."""

    def setUp(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["stations"] = {}
        STATE["next_station_id"] = 1
        STATE["players_by_id"] = {}
        STATE["attack_events"] = []
        STATE["kills"] = []
        STATE["defense_log"] = []
        STATE["warps"] = {19: [20], 20: [19], 21: [20]}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["sector_mines"] = {}
        STATE["sector_players"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    def _station(self, owner_id=1, owner_name="Tester", sector=20, **over):
        sid = STATE["next_station_id"]
        STATE["next_station_id"] += 1
        st = {
            "id": sid, "sector_id": sector, "owner_id": owner_id,
            "owner_name": owner_name, "level": 1, "shields": 0, "fighters": 0,
            "shields_enabled": 0, "fuel": 0, "organics": 0, "equipment": 0,
            "posture": "defensive", "engage_pct": 100,
            "last_fuel_burn": "2026-06-27T12:00:00+00:00",
            "upgrade_to": None, "upgrade_started_at": None,
            "credits": 0,
        }
        st.update(over)
        STATE["stations"][sid] = st
        return st

    async def dock(self):
        return await main.cmd_station(self.ctx(), "")

    async def say(self, text):
        return await main.cmd_station_step(self.ctx(), text)

    async def test_status_and_menu_show_the_treasury(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20)
        self._station(credits=12345)
        menu = await self.dock()
        self.assertIn("Treasury: 12345cr", menu)
        self.assertIn("8) Treasury -- deposit/withdraw credits", menu)

    async def test_depositing_credits(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       credits=5000)
        self._station()

        await self.dock()
        prompt = await self.say("8")
        self.assertIn("Treasury holds 0cr; you're carrying 5000cr", prompt)
        prompt = await self.say("d")
        self.assertIn("Deposit how much? You're carrying 5000cr", prompt)
        reply = await self.say("3000")

        self.assertIn("Deposited 3000cr into the treasury", reply)
        self.assertIn("goes down with the station", reply)   # the fine print
        self.assertIn("Treasury: 3000cr", reply)             # fresh status
        self.assertEqual(STATE["player"]["credits"], 2000)
        self.assertEqual(STATE["stations"][1]["credits"], 3000)

    async def test_deposit_all_and_withdraw_all(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       credits=700)
        self._station(credits=0)

        await self.dock()
        await self.say("8")
        await self.say("d")
        await self.say("all")
        self.assertEqual(STATE["player"]["credits"], 0)
        self.assertEqual(STATE["stations"][1]["credits"], 700)

        await self.say("8")
        await self.say("w")
        reply = await self.say("all")
        self.assertIn("Withdrew 700cr from the treasury", reply)
        self.assertEqual(STATE["player"]["credits"], 700)
        self.assertEqual(STATE["stations"][1]["credits"], 0)

    async def test_cannot_deposit_more_than_carried(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       credits=100)
        self._station()

        await self.dock()
        await self.say("8")
        await self.say("d")
        reply = await self.say("500")

        self.assertIn("You're only carrying 100cr", reply)
        self.assertEqual(STATE["player"]["credits"], 100)      # nothing moved
        self.assertEqual(STATE["stations"][1]["credits"], 0)
        # Still at the amount prompt -- a good number goes through.
        reply = await self.say("100")
        self.assertIn("Deposited 100cr", reply)

    async def test_cannot_withdraw_more_than_the_treasury_holds(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       credits=0)
        self._station(credits=50)

        await self.dock()
        await self.say("8")
        await self.say("w")
        reply = await self.say("500")

        self.assertIn("The treasury only holds 50cr", reply)
        self.assertEqual(STATE["stations"][1]["credits"], 50)

    async def test_empty_pockets_and_empty_treasury_short_circuit(self):
        STATE["player"] = fresh_player(id=1, name="Tester", sector_id=20,
                                       credits=0)
        self._station(credits=0)

        await self.dock()
        await self.say("8")
        reply = await self.say("d")
        self.assertIn("not carrying any credits to deposit", reply)

        await self.say("8")
        reply = await self.say("w")
        self.assertIn("The treasury is empty", reply)

    async def test_destroying_a_station_burns_its_treasury(self):
        STATE["player"] = fresh_player(id=2, name="Rival", sector_id=20,
                                       fighters=500, credits=0)
        self._station(owner_id=1, owner_name="Tester", fighters=0, shields=0,
                      credits=90000)

        await main.cmd_attack(self.ctx(), "station")
        report = await main.cmd_attack_step(self.ctx(), "all")

        self.assertIn("You destroyed Space Station - Tester!", report)
        self.assertIn("Its treasury of 90000cr is lost to the void.", report)
        self.assertEqual(STATE["player"]["credits"], 0)   # no looting
        self.assertNotIn(1, STATE["stations"])


class AdvertiseTests(unittest.IsolatedAsyncioTestCase):
    """The 48-hour public-channel advertisement: due immediately on a
    fresh install, silent while the countdown runs, due again after 48
    hours -- all keyed off the messages log so restarts never re-spam."""

    def setUp(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        _reset_multiship_state()
        STATE["last_tx_times"] = {}

    class _FakeCommands:
        def __init__(self):
            self.sent = []

        async def send_chan_msg(self, channel_idx, text):
            self.sent.append((channel_idx, text))

            class _Result:
                type = "OK"
            return _Result()

    class _FakeMC:
        def __init__(self):
            self.commands = AdvertiseTests._FakeCommands()

    def _last_ad(self, seconds_ago):
        from datetime import datetime, timedelta, timezone
        when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        STATE["last_tx_times"][
            (f"chan{main.PUBLIC_CHANNEL_IDX}", main.ADVERT_TEXT)
        ] = when.isoformat()

    def test_the_interval_is_48_hours(self):
        self.assertEqual(main.ADVERT_INTERVAL_SECONDS, 48 * 60 * 60)

    def test_the_ad_fits_in_one_radio_chunk(self):
        self.assertLessEqual(len(main.ADVERT_TEXT), 130)
        self.assertIn("Tradewars 2002 is ready", main.ADVERT_TEXT)
        self.assertIn("DM", main.ADVERT_TEXT)

    def test_due_immediately_when_never_advertised(self):
        self.assertEqual(main._seconds_until_next_advert(), 0)

    def test_not_due_during_the_countdown(self):
        self._last_ad(seconds_ago=60 * 60)          # an hour ago
        remaining = main._seconds_until_next_advert()
        self.assertGreater(remaining, 46 * 60 * 60)
        self.assertLess(remaining, 48 * 60 * 60)

    def test_due_again_after_48_hours(self):
        self._last_ad(seconds_ago=49 * 60 * 60)
        self.assertEqual(main._seconds_until_next_advert(), 0)

    async def test_maybe_advertise_broadcasts_on_the_public_channel(self):
        import contextlib
        import io
        mc = self._FakeMC()
        with contextlib.redirect_stdout(io.StringIO()):
            sent = await main.maybe_advertise(mc)
        self.assertTrue(sent)
        self.assertEqual(
            mc.commands.sent,
            [(main.PUBLIC_CHANNEL_IDX, main.ADVERT_TEXT)],  # one chunk, no prefix
        )

    async def test_maybe_advertise_stays_quiet_mid_countdown(self):
        self._last_ad(seconds_ago=60 * 60)
        mc = self._FakeMC()
        sent = await main.maybe_advertise(mc)
        self.assertFalse(sent)
        self.assertEqual(mc.commands.sent, [])

    async def test_disabled_switch_silences_an_otherwise_due_ad(self):
        # Never advertised, so an ad is due -- but the switch wins, and
        # nothing is transmitted or logged (leaving the countdown unarmed,
        # so re-enabling broadcasts on the next wake-up). setUp's reload
        # of main restores ADVERT_ENABLED = True for every other test.
        main.ADVERT_ENABLED = False
        mc = self._FakeMC()
        sent = await main.maybe_advertise(mc)
        self.assertFalse(sent)
        self.assertEqual(mc.commands.sent, [])
        self.assertEqual(STATE["last_tx_times"], {})


if __name__ == "__main__":
    unittest.main()