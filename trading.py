"""
Everything that happens at a port: the guided sell-then-buy commodity
flow at ordinary ports, and the Stardock refit/shipyard flow (buying
holds/fighters/shields/mines, or swapping hulls). These are the longest
state machines in the game, walked one reply at a time via the PENDING_*
dicts in core.
"""

import re

from db import (
    get_port,
    execute_trade,
    apply_port_restock,
    upgrade_ship_stat,
    buy_ship,
    park_and_buy_ship,
    get_ship,
    get_parked_ships_in_sector,
    delete_ship,
    set_towing,
    get_player_with_ship,
    set_ship_station_core,
    adjust_player_credits,
    SHIP_CATALOG,
    DEFAULT_SHIP_TYPE,
    ESCAPE_POD_SHIP,
    STARDOCK_PRICES,
    STATION_CORE_PRICE,
    STATION_CORE_HOLDS,
    sell_value,
)

from core import (
    PENDING_TRADES,
    PENDING_UPGRADES,
    command,
    _resume_navigation_suffix,
)


# (display label, db column prefix) for each tradeable commodity, in the
# fixed order used everywhere a port's class/trades are shown -- this is
# also the order the buy/sell command ("p"/"port") lists its menu in.
COMMODITIES = [
    ("Fuel Ore", "fuel_ore"),
    ("Organics", "organics"),
    ("Equipment", "equipment"),
]


# (display label, ships column, price per unit, SHIP_CATALOG cap key)
# for every *possible* Stardock refit stat, in the fixed order shown in
# the refit menu. Not every stat applies to every ship -- e.g. Mines
# only matters for a hull with a mine bay -- so this is filtered down
# per player by _available_upgrades() before it's ever numbered or
# displayed; nothing here assumes a particular ship type.
STARDOCK_UPGRADE_DEFS = [
    ("Cargo Holds", "holds_total", STARDOCK_PRICES["holds_total"], "max_holds"),
    ("Fighters", "fighters", STARDOCK_PRICES["fighters"], "max_fighters"),
    ("Shields", "shields", STARDOCK_PRICES["shields"], "max_shields"),
    ("Mines", "mines", STARDOCK_PRICES["mines"], "max_mines"),
    ("Probes", "probes", STARDOCK_PRICES["probes"], "max_probes"),
]


def _ship_stat_limit(p, cap_key):
    """The per-stat cap for the player's *current* ship type -- e.g.
    _ship_stat_limit(p, "max_holds") for their cargo-hold cap. Always
    looked up fresh from SHIP_CATALOG rather than a fixed constant,
    since different hulls cap out at different points."""
    return SHIP_CATALOG[p["ship_type"]][cap_key]


def _available_upgrades(p):
    """STARDOCK_UPGRADE_DEFS filtered down to the stats that actually
    apply to the player's *current* ship -- a hull with no mine bay
    (max_mines == 0) simply won't offer Mines as a refit option. Menu
    numbering in build_stardock_menu/cmd_stardock_step is based on this
    filtered list, not the full one, so it always matches what's shown."""
    return [
        upgrade for upgrade in STARDOCK_UPGRADE_DEFS
        if _ship_stat_limit(p, upgrade[3]) > 0
    ]


def _shipyard_option(p):
    """Menu number for "enter the shipyard" -- always one past however
    many refit options apply to the player's current ship, so it shifts
    automatically as that list grows or shrinks per hull."""
    return len(_available_upgrades(p)) + 1


def _station_core_option(p):
    """Menu number for buying a Space Station Core kit -- one past the
    shipyard option."""
    return _shipyard_option(p) + 1


def _purchasable_ships():
    """SHIP_CATALOG hull names that can actually be bought, in catalog
    order. Excludes anything flagged "purchasable": False -- namely the
    Escape Pod, which exists in the catalog only as a destruction
    outcome, not something for sale. Both the shipyard menu and its
    number-to-hull mapping go through this, so the two always agree."""
    return [name for name, ship in SHIP_CATALOG.items() if ship.get("purchasable", True)]


def build_stardock_menu(p):
    """The top-level Stardock menu: current/max and price for each
    upgradeable stat that applies to the player's current ship, plus an
    entry into the shipyard and the Space Station Core kit. Shown when a
    player first docks and again after every refit purchase (or skip) so
    they can keep buying in one visit."""
    lines = ["Stardock refits:"]
    for i, (label, col, price, cap_key) in enumerate(_available_upgrades(p), start=1):
        limit = _ship_stat_limit(p, cap_key)
        lines.append(f"  {i}) {label} {p[col]}/{limit} @ {price}cr each")
    lines.append(f"  {_shipyard_option(p)}) Shipyard -- buy or sell your ship")
    kit_tag = " (carrying one)" if p.get("station_core") else ""
    lines.append(
        f"  {_station_core_option(p)}) Space Station Core kit -- "
        f"{STATION_CORE_PRICE}cr, {STATION_CORE_HOLDS} holds{kit_tag}"
    )
    lines.append(f"{p['credits']}cr available. Reply with a number, or 'cancel'.")
    return "\n".join(lines)


def build_shipyard_menu(p):
    """The shipyard sub-menu: every hull in SHIP_CATALOG with its
    classification, caps, and price (or 'current ship'/'free'), plus a
    sell-back option for whatever the player is currently flying (only
    shown if they're not already flying the free default ship -- there
    would be nothing to trade in)."""
    lines = ["Shipyard:"]
    for i, name in enumerate(_purchasable_ships(), start=1):
        ship = SHIP_CATALOG[name]
        if name == p["ship_type"]:
            tag = "(current ship)"
        elif ship["price"]:
            tag = f"{ship['price']}cr"
        else:
            tag = "free"
        stats = (
            f"{ship['max_holds']} holds / {ship['max_fighters']} fighters / "
            f"{ship['max_shields']} shields"
        )
        if ship["max_mines"]:
            stats += f" / {ship['max_mines']} mines"
        lines.append(f"  {i}) {name} ({ship['classification']}): {stats} -- {tag}")
    if p["ship_type"] != DEFAULT_SHIP_TYPE:
        resale = sell_value(p["ship_type"])
        lines.append(
            f"  S) Sell your {p['ship_type']} -- trade in for {resale}cr "
            f"and return to the {DEFAULT_SHIP_TYPE}"
        )
    # Any of the player's OWN ships parked in this sector can be sold
    # outright (this is what towing a spare hull back to the Stardock is
    # for). Sold as-is: the refund is the hull's trade-in value only;
    # whatever's still aboard goes with it.
    for ship in _own_parked_ships_here(p):
        lines.append(
            f"  s{ship['id']}) Sell your parked {ship['ship_type']} #{ship['id']} "
            f"-- {sell_value(ship['ship_type'])}cr"
        )
    lines.append(f"{p['credits']}cr available. Reply with a number to buy, 'S' to sell, or '0' to go back.")
    return "\n".join(lines)


def _own_parked_ships_here(p):
    """The player's own unmanned ships sitting in their current sector
    (which, whenever the shipyard menu is being built, is the Stardock's
    sector), in stable id order."""
    return [s for s in get_parked_ships_in_sector(p["sector_id"])
            if s["owner_id"] == p["id"]]


def _build_sell_prompt(state, p, port, item):
    """Fill `state` in for a sell offer and return its prompt text, or
    None if there's nothing actually sellable right now (port has no
    room) so the caller should move on to the next queued item."""
    key = item["key"]
    label = item["label"]
    price = port[f"{key}_price"]
    have = p[key]
    room = port[f"{key}_max"] - port[f"{key}_qty"]
    max_qty = max(0, min(have, room))

    state.update({
        "stage": "quantity",
        "action": "sell",
        "commodity": key,
        "direction": "B",
        "price": price,
        "label": label,
        "max_qty": max_qty,
    })
    if max_qty <= 0:
        return None
    return (
        f"Port buys {label} @ {price}cr each.\n"
        f"You have {have}, port has room for {room} more.\n"
        f"Sell how many? (0-{max_qty}, or 'cancel')"
    )


def _build_buy_prompt(state, p, port, item):
    """Fill `state` in for a buy offer and return its prompt text, or
    None if there's nothing actually buyable right now (no free holds,
    no stock, or can't afford even 1) so the caller should move on."""
    key = item["key"]
    label = item["label"]
    price = port[f"{key}_price"]
    holds_used = p["fuel_ore"] + p["organics"] + p["equipment"] + (
        STATION_CORE_HOLDS if p.get("station_core") else 0)
    free_holds = p["holds_total"] - holds_used
    stock = port[f"{key}_qty"]
    max_affordable = p["credits"] // price if price > 0 else free_holds
    max_qty = max(0, min(free_holds, stock, max_affordable))

    state.update({
        "stage": "quantity",
        "action": "buy",
        "commodity": key,
        "direction": "S",
        "price": price,
        "label": label,
        "max_qty": max_qty,
    })
    if max_qty <= 0:
        return None
    return (
        f"Port sells {label} @ {price}cr each.\n"
        f"Free holds: {free_holds}, {label} stock: {stock}, you can afford {max_affordable}.\n"
        f"Buy how many {label}? (0-{max_qty}, or 'cancel')"
    )


def _advance_trade_queue(pubkey, p, port):
    """
    Pop items off the pending trade's queue, in order, until one yields a
    usable prompt (max_qty > 0) -- `state` is updated in place for
    whichever item it settles on. Returns that prompt, or a closing
    message once the queue is exhausted with nothing left worth offering.
    """
    state = PENDING_TRADES[pubkey]
    while state["queue"]:
        item = state["queue"].pop(0)
        builder = _build_sell_prompt if item["action"] == "sell" else _build_buy_prompt
        prompt = builder(state, p, port, item)
        if prompt is not None:
            return prompt
    PENDING_TRADES.pop(pubkey, None)
    return "Nothing more to trade here." + _resume_navigation_suffix(pubkey, p["sector_id"])


@command("p", "port", description="dock at port to trade or refit")
async def cmd_trade(ctx, args):
    """
    Start a guided port visit. At the Stardock (Sec1), this means a
    refit visit -- see build_stardock_menu/cmd_stardock_step -- since
    the Stardock deals in ship upgrades, not commodities. At any other
    port, builds a queue of trade offers in two passes, both walking
    COMMODITIES (fuel, organics, equipment) in order:

      1. SELL -- for every commodity the player is carrying that this
         port buys (direction "B"), offer to sell however many units
         they have, capped by the port's remaining storage room.
      2. BUY  -- for every commodity this port sells (direction "S"),
         offer to buy however many cargo holds are free *at that point*
         -- which grows as cargo is sold off in step 1 -- capped by port
         stock and what the player can afford.

    The queue is stored in PENDING_TRADES and walked one item at a time
    by cmd_trade_step: each offer asks for a quantity (0 skips to the
    next item), then yes/no/counter-offer same as before. 'cancel' ends
    the whole visit early. Recomputing the queue fresh on each dock means
    it always reflects current cargo and port stock, never stale state
    from a previous visit.
    """
    p = ctx.player
    port = get_port(p["sector_id"])
    if port is None:
        return "No port in current sector."
    if port["port_class"] == "STARDOCK":
        PENDING_UPGRADES[ctx.pubkey] = {"stage": "menu"}
        return build_stardock_menu(p)

    # Docking is a "visit": bring the port's stock up to date first, so the
    # offers reflect whatever has drifted back since it was last traded.
    port = apply_port_restock(port["id"])

    queue = []
    for label, key in COMMODITIES:
        if p[key] > 0 and port[f"{key}_dir"] == "B":
            queue.append({"action": "sell", "key": key, "label": label})
    for label, key in COMMODITIES:
        if port[f"{key}_dir"] == "S":
            queue.append({"action": "buy", "key": key, "label": label})

    if not queue:
        return "Nothing to trade with this port."

    PENDING_TRADES[ctx.pubkey] = {
        "sector_id": p["sector_id"],
        "port_id": port["id"],
        "queue": queue,
    }
    return _advance_trade_queue(ctx.pubkey, p, port)


async def cmd_trade_step(ctx, message):
    """
    Advance a pending port visit by one step. Stages, in order per queued
    item:
      "quantity" -- reply with a whole number of units. 0 skips this
                    item and moves on to the next queued offer (or closes
                    out the visit if none remain). A non-zero amount,
                    capped at state["max_qty"], moves to "confirm" with
                    the listed total price.
      "confirm"  -- "yes" accepts the listed price; a whole-number reply
                    is a negotiated cr/unit offer -- a buying port
                    accepts up to 104% of its listed price, a selling
                    port accepts down to 96%, otherwise it holds firm and
                    the player can try again. Accepting re-validates
                    against current state (in case the port or player's
                    cargo changed since the quote), executes the trade,
                    then advances to the next queued item. "no" skips
                    this item (same as a quantity of 0) rather than
                    ending the whole visit.
    'cancel' is accepted at any stage and ends the whole visit early.
    """
    p = ctx.player
    pubkey = ctx.pubkey
    text = message.strip()
    lower = text.lower()

    state = PENDING_TRADES.get(pubkey)
    if not state:
        PENDING_TRADES.pop(pubkey, None)
        return "No trade in progress."

    if lower == "cancel":
        PENDING_TRADES.pop(pubkey, None)
        return "Trade cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])

    port = get_port(state["sector_id"])
    if port is None or port["id"] != state["port_id"]:
        PENDING_TRADES.pop(pubkey, None)
        return "Port no longer available. Trade cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])

    if state["stage"] == "quantity":
        if not re.match(r"^\d+$", text):
            return "Enter a whole number of units (0 to skip), or 'cancel'."
        qty = int(text)
        if qty > state["max_qty"]:
            return f"Max is {state['max_qty']}. Enter a smaller quantity, or 'cancel'."

        if qty == 0:
            return _advance_trade_queue(pubkey, p, port)

        state["qty"] = qty
        state["total_price"] = qty * state["price"]
        state["stage"] = "confirm"
        verb = "Sell" if state["action"] == "sell" else "Buy"
        return (
            f"{verb} {qty} {state['label']} for {state['total_price']}cr "
            f"({state['price']}cr/unit)? yes/no, or offer a cr/unit price"
        )

    if state["stage"] == "confirm":
        if lower in ("n", "no"):
            return _advance_trade_queue(pubkey, p, port)

        key = state["commodity"]
        direction = state["direction"]
        qty = state["qty"]
        label = state["label"]
        listed_price = state["price"]
        player_is_buying = state["action"] == "buy"

        if lower in ("y", "yes"):
            price_per_unit = listed_price
        elif re.match(r"^\d+$", text):
            offer = int(text)
            if offer <= 0:
                return "Price must be a positive whole number, or 'yes'/'no'."

            # Same simple rule everywhere: a buying port (direction 'B')
            # accepts an asking price up to 4% above what it offered; a
            # selling port (direction 'S') accepts a bid down to 96% of
            # what it asked. Outside that band, the port holds firm and
            # the player can try again, accept the listed price, or skip.
            if direction == "B":
                accepted = offer <= listed_price * 1.04
            else:
                accepted = offer >= listed_price * 0.96

            if not accepted:
                return (
                    f"Port won't budge to {offer}cr/unit for {label}. "
                    f"Try another price, 'yes' for {listed_price}cr/unit, or 'no' to skip."
                )
            price_per_unit = offer
        else:
            return "Reply 'yes' to confirm, 'no' to skip, or enter a cr/unit price to negotiate."

        total_price = qty * price_per_unit

        # Re-validate against current state right before committing --
        # the port or the player's cargo/credits may have changed since
        # the quote was given a step ago.
        if player_is_buying:
            holds_used = p["fuel_ore"] + p["organics"] + p["equipment"] + (
                STATION_CORE_HOLDS if p.get("station_core") else 0)
            free_holds = p["holds_total"] - holds_used
            stock = port[f"{key}_qty"]
            ok = qty <= free_holds and qty <= stock and total_price <= p["credits"]
        else:
            have = p[key]
            room = port[f"{key}_max"] - port[f"{key}_qty"]
            ok = qty <= have and qty <= room

        if not ok:
            nxt = _advance_trade_queue(pubkey, p, port)
            return f"Conditions changed since this quote -- skipping.\n\n{nxt}"

        execute_trade(p["id"], port["id"], key, qty, total_price, player_is_buying)
        verb = "Bought" if player_is_buying else "Sold"
        sign = "-" if player_is_buying else "+"
        result_line = f"{verb} {qty} {label} for {sign}{total_price}cr ({price_per_unit}cr/unit)."

        # Re-fetch so the next queued item (if any) reflects this trade's
        # effect on cargo space and credits -- ctx.player is otherwise
        # stale for the rest of this call.
        p = get_player_with_ship(pubkey)
        nxt = _advance_trade_queue(pubkey, p, port)
        return f"{result_line}\n\n{nxt}"

    # Shouldn't be reachable, but don't leave a broken trade stuck in state.
    PENDING_TRADES.pop(pubkey, None)
    return "Something went wrong with this trade. Cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])


def _buy_station_core(pubkey, p):
    """Buy a Space Station Core kit: needs STATION_CORE_HOLDS free holds,
    STATION_CORE_PRICE credits, and that the ship isn't already carrying
    one. Returns a result line followed by a freshly-rebuilt Stardock menu
    (the visit stays open). No-ops with an explanation if it can't go
    through."""
    if p.get("station_core"):
        msg = "You're already hauling a Station Core kit -- deploy it first."
    else:
        holds_used = p["fuel_ore"] + p["organics"] + p["equipment"]
        free_holds = p["holds_total"] - holds_used
        if free_holds < STATION_CORE_HOLDS:
            msg = (f"A Station Core kit needs {STATION_CORE_HOLDS} free holds; "
                   f"you have {free_holds}.")
        elif p["credits"] < STATION_CORE_PRICE:
            msg = (f"Can't afford a Station Core kit ({STATION_CORE_PRICE}cr); "
                   f"you have {p['credits']}cr.")
        else:
            adjust_player_credits(p["id"], -STATION_CORE_PRICE)
            set_ship_station_core(p["id"], True)
            msg = (f"Bought a Space Station Core kit (-{STATION_CORE_PRICE}cr). It fills "
                   f"{STATION_CORE_HOLDS} holds -- haul it to a sector outside the safe "
                   "zone and 'deploy'.")
    return msg + "\n\n" + build_stardock_menu(get_player_with_ship(pubkey))


async def cmd_stardock_step(ctx, message):
    """
    Advance a pending Stardock visit. Unlike the port trade queue (which
    walks every tradeable commodity exactly once), the Stardock menu is
    open-ended: after each completed or skipped action the player is
    shown a menu again and can keep going until they reply 'cancel'.

    Refit stages (buying cargo holds/fighters/shields/mines up to the
    current ship's caps -- see SHIP_CATALOG/_ship_stat_limit). Which
    stats are offered depends on the ship: a hull with no mine bay just
    won't show Mines as an option (see _available_upgrades).
      "menu"     -- reply with a refit's menu number, the shipyard's
                    menu number (_shipyard_option(p), always one past
                    the refit options), or 'cancel'. Rejects a stat
                    that's already at its cap, or one the player can't
                    afford even 1 unit of, before moving to "quantity".
      "quantity" -- reply with a whole number of units. 0 returns to the
                    menu without buying. A non-zero amount, capped at
                    state["max_qty"], moves to "confirm" with the listed
                    total price.
      "confirm"  -- "yes" commits the purchase and returns to the menu.
                    Anything else returns to the menu without buying.

    Shipyard stages (buying a different hull -- trading the current one
    in OR keeping it parked -- selling the current hull back to the free
    default ship, or selling a parked hull outright; see
    build_shipyard_menu):
      "shipyard_menu"    -- reply with a hull's menu number to price out
                             buying it, 'S'/'sell' to price out selling
                             the current ship, 's<id>' to price out
                             selling a parked ship of yours in this
                             sector, or '0' to go back to the main menu.
                             Rejects buying the hull already owned, or
                             one that can't be afforded even after the
                             trade-in. A buy moves to "shipyard_keep"
                             (a pod pilot skips straight to
                             "shipyard_confirm" -- a pod isn't kept).
      "shipyard_keep"    -- 'sell' trades the current hull in (net
                             cost); 'keep' parks it here and pays the
                             full price (rejected if full price can't
                             be afforded); 'no'/'back' returns to the
                             shipyard menu. Either choice moves to
                             "shipyard_confirm".
      "shipyard_confirm" -- "yes" commits the transaction and returns to
                             the main menu. Anything else returns to the
                             shipyard menu without buying/selling.

    'cancel' is accepted at any stage and ends the whole visit.
    """
    pubkey = ctx.pubkey
    text = message.strip()
    lower = text.lower()

    state = PENDING_UPGRADES.get(pubkey)
    if not state:
        PENDING_UPGRADES.pop(pubkey, None)
        return "No Stardock visit in progress."

    if lower == "cancel":
        PENDING_UPGRADES.pop(pubkey, None)
        return "Left the Stardock." + _resume_navigation_suffix(pubkey, ctx.player["sector_id"])

    # Always re-fetch -- a purchase made a step ago in this same visit
    # changes credits and/or the ship itself.
    p = get_player_with_ship(pubkey)

    if state["stage"] == "menu":
        if not re.match(r"^\d+$", text):
            return "Reply with a number, or 'cancel'.\n\n" + build_stardock_menu(p)
        choice = int(text)
        available = _available_upgrades(p)

        if choice == len(available) + 1:
            state["stage"] = "shipyard_menu"
            return build_shipyard_menu(p)

        if choice == len(available) + 2:   # Space Station Core kit
            return _buy_station_core(pubkey, p)

        if choice < 1 or choice > len(available):
            return "Not a valid option.\n\n" + build_stardock_menu(p)

        label, col, price, cap_key = available[choice - 1]
        limit = _ship_stat_limit(p, cap_key)
        current = p[col]
        room = limit - current
        if room <= 0:
            return f"Already at max {label} ({limit}).\n\n" + build_stardock_menu(p)

        max_qty = max(0, min(room, p["credits"] // price))
        if max_qty <= 0:
            return f"Can't afford even 1 {label} ({price}cr each).\n\n" + build_stardock_menu(p)

        state.update({
            "stage": "quantity",
            "label": label,
            "column": col,
            "price": price,
            "limit": limit,
            "max_qty": max_qty,
        })
        return (
            f"{label}: {current}/{limit}, {price}cr each.\n"
            f"Buy how many? (0-{max_qty}, or 'cancel')"
        )

    if state["stage"] == "quantity":
        if not re.match(r"^\d+$", text):
            return "Enter a whole number of units (0 to go back), or 'cancel'."
        qty = int(text)
        if qty > state["max_qty"]:
            return f"Max is {state['max_qty']}. Enter a smaller quantity, or 'cancel'."
        if qty == 0:
            state["stage"] = "menu"
            return build_stardock_menu(p)

        state["qty"] = qty
        state["total_price"] = qty * state["price"]
        state["stage"] = "confirm"
        return (
            f"Buy {qty} {state['label']} for {state['total_price']}cr "
            f"({state['price']}cr/unit)? yes/no"
        )

    if state["stage"] == "confirm":
        if lower not in ("y", "yes"):
            state["stage"] = "menu"
            return build_stardock_menu(p)

        col = state["column"]
        qty = state["qty"]
        total_price = state["total_price"]
        label = state["label"]
        limit = state["limit"]

        # Re-validate against current state right before committing --
        # credits or the stat's current value may have changed since the
        # quote a step ago (only this player can act during their own
        # visit, but this keeps the same safety margin as cmd_trade_step).
        if total_price > p["credits"] or p[col] + qty > limit:
            state["stage"] = "menu"
            return "Conditions changed -- purchase skipped.\n\n" + build_stardock_menu(p)

        upgrade_ship_stat(p["id"], col, qty, total_price)
        p = get_player_with_ship(pubkey)
        state["stage"] = "menu"
        return (
            f"Installed {qty} {label} for -{total_price}cr.\n\n"
            f"{build_stardock_menu(p)}"
        )

    if state["stage"] == "shipyard_menu":
        if text == "0":
            state["stage"] = "menu"
            return build_stardock_menu(p)

        if lower in ("s", "sell"):
            if p["ship_type"] == DEFAULT_SHIP_TYPE:
                return (
                    f"You're already flying the {DEFAULT_SHIP_TYPE} -- nothing to trade in.\n\n"
                    + build_shipyard_menu(p)
                )
            resale = sell_value(p["ship_type"])
            state.update({"stage": "shipyard_confirm", "action": "sell", "resale": resale})
            return (
                f"Sell your {p['ship_type']} and return to the {DEFAULT_SHIP_TYPE} "
                f"for {resale}cr? yes/no"
            )

        parked_sale = re.match(r"^s(\d+)$", lower)
        if parked_sale:
            ship_id = int(parked_sale.group(1))
            ship = next((s for s in _own_parked_ships_here(p) if s["id"] == ship_id), None)
            if ship is None:
                return (
                    f"No parked ship of yours here with id #{ship_id}.\n\n"
                    + build_shipyard_menu(p)
                )
            resale = sell_value(ship["ship_type"])
            state.update({
                "stage": "shipyard_confirm", "action": "sell_parked",
                "parked_ship_id": ship_id, "resale": resale,
            })
            return (
                f"Sell your parked {ship['ship_type']} #{ship_id} for {resale}cr? "
                "Anything still aboard it is lost. yes/no"
            )

        if not re.match(r"^\d+$", text):
            return "Reply with a number to buy, 'S' to sell, or '0' to go back.\n\n" + build_shipyard_menu(p)
        choice = int(text)
        catalog_names = _purchasable_ships()
        if choice < 1 or choice > len(catalog_names):
            return "Not a valid option.\n\n" + build_shipyard_menu(p)

        ship_name = catalog_names[choice - 1]
        if ship_name == p["ship_type"]:
            return f"You already own the {ship_name}.\n\n" + build_shipyard_menu(p)

        ship = SHIP_CATALOG[ship_name]
        trade_in = sell_value(p["ship_type"])
        net_cost = ship["price"] - trade_in
        if net_cost > p["credits"]:
            return (
                f"Can't afford the {ship_name} -- net cost {net_cost}cr "
                f"({ship['price']}cr less a {trade_in}cr trade-in), you have {p['credits']}cr.\n\n"
                + build_shipyard_menu(p)
            )

        if p["ship_type"] == ESCAPE_POD_SHIP:
            # A pod isn't a hull worth keeping -- skip the keep question
            # and go straight to the trade-in confirm (trade-in is 0cr).
            state.update({
                "stage": "shipyard_confirm",
                "action": "buy",
                "ship_name": ship_name,
                "trade_in": trade_in,
                "net_cost": net_cost,
            })
            return (
                f"Trade in your {p['ship_type']} ({trade_in}cr) for a {ship_name} "
                f"({ship['price']}cr)? Net cost: {net_cost}cr. yes/no"
            )

        state.update({
            "stage": "shipyard_keep",
            "ship_name": ship_name,
            "trade_in": trade_in,
            "net_cost": net_cost,
        })
        return (
            f"Buying a {ship_name} ({ship['price']}cr). Sell your {p['ship_type']} "
            f"as trade-in (+{trade_in}cr, net {net_cost}cr), or keep it parked here in "
            f"Sec{p['sector_id']} (full {ship['price']}cr)? Reply sell/keep (or cancel)."
        )

    if state["stage"] == "shipyard_keep":
        ship_name = state["ship_name"]
        ship = SHIP_CATALOG[ship_name]
        if lower in ("s", "sell", "trade", "trade-in"):
            state.update({"stage": "shipyard_confirm", "action": "buy"})
            return (
                f"Trade in your {p['ship_type']} ({state['trade_in']}cr) for a {ship_name} "
                f"({ship['price']}cr)? Net cost: {state['net_cost']}cr. yes/no"
            )
        if lower in ("k", "keep", "park"):
            if ship["price"] > p["credits"]:
                state["stage"] = "shipyard_menu"
                return (
                    f"Can't afford to keep your {p['ship_type']} -- the {ship_name} costs "
                    f"the full {ship['price']}cr without a trade-in, you have "
                    f"{p['credits']}cr.\n\n" + build_shipyard_menu(p)
                )
            state.update({"stage": "shipyard_confirm", "action": "buy_keep"})
            return (
                f"Buy the {ship_name} for {ship['price']}cr and keep your "
                f"{p['ship_type']} parked in Sec{p['sector_id']}? yes/no"
            )
        if lower in ("no", "n", "0", "back"):
            state["stage"] = "shipyard_menu"
            return build_shipyard_menu(p)
        return "Reply 'sell' to trade in, 'keep' to park your current ship, or 'cancel'."

    if state["stage"] == "shipyard_confirm":
        if lower not in ("y", "yes"):
            state["stage"] = "shipyard_menu"
            return build_shipyard_menu(p)

        if state["action"] == "sell":
            new_ship = SHIP_CATALOG[DEFAULT_SHIP_TYPE]
            buy_ship(
                p["id"], DEFAULT_SHIP_TYPE,
                new_ship["base_holds"], new_ship["base_fighters"], new_ship["base_shields"],
                new_ship["base_mines"],
                credit_delta=state["resale"],
            )
            result_line = (
                f"Sold your old ship. Welcome back to the {DEFAULT_SHIP_TYPE}. "
                f"+{state['resale']}cr."
            )
        elif state["action"] == "sell_parked":
            # Re-validate: the ship must still exist, still be theirs, and
            # still be sitting in this sector (it could have been destroyed
            # -- or this could be a stale confirm).
            ship = get_ship(state["parked_ship_id"])
            if (ship is None or ship["owner_id"] != p["id"]
                    or ship["sector_id"] != p["sector_id"]):
                state["stage"] = "shipyard_menu"
                return ("That ship is no longer parked here.\n\n"
                        + build_shipyard_menu(p))
            # If they were towing this very hull, the sale releases the
            # line (delete_ship also clears any towing reference, but be
            # explicit for the player-visible state).
            if p.get("towing_ship_id") == ship["id"]:
                set_towing(p["id"], None)
            delete_ship(ship["id"])
            adjust_player_credits(p["id"], state["resale"])
            result_line = (
                f"Sold your parked {ship['ship_type']} #{ship['id']}. "
                f"+{state['resale']}cr."
            )
        elif state["action"] == "buy_keep":
            ship_name = state["ship_name"]
            ship = SHIP_CATALOG[ship_name]
            if ship["price"] > p["credits"]:
                # Credits could have changed since the keep prompt.
                state["stage"] = "shipyard_menu"
                return (f"You no longer have the full {ship['price']}cr.\n\n"
                        + build_shipyard_menu(p))
            old_type = p["ship_type"]
            park_and_buy_ship(
                p["id"], p["sector_id"], ship_name,
                ship["base_holds"], ship["base_fighters"], ship["base_shields"],
                ship["base_mines"],
                credit_delta=-ship["price"],
            )
            result_line = (
                f"Welcome aboard the {ship_name}! -{ship['price']}cr. "
                f"Your {old_type} is parked in Sec{p['sector_id']}."
            )
        else:
            ship_name = state["ship_name"]
            ship = SHIP_CATALOG[ship_name]
            buy_ship(
                p["id"], ship_name,
                ship["base_holds"], ship["base_fighters"], ship["base_shields"],
                ship["base_mines"],
                credit_delta=-state["net_cost"],
            )
            result_line = f"Welcome aboard the {ship_name}! -{state['net_cost']}cr (net)."

        p = get_player_with_ship(pubkey)
        state["stage"] = "menu"
        return f"{result_line}\n\n{build_stardock_menu(p)}"

    # Shouldn't be reachable, but don't leave a broken visit stuck in state.
    PENDING_UPGRADES.pop(pubkey, None)
    return "Something went wrong with this visit. Cancelled." + _resume_navigation_suffix(pubkey, ctx.player["sector_id"])
