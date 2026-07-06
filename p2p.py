"""
The port-to-port (p2p) auto-shuttle: repeated move-and-trade legs
between two adjacent commodity ports that share at least one tradeable
commodity. cmd_p2p classifies the pair and prompts; cmd_p2p_step
validates the starting holds and runs legs one confirmation at a time.

Each leg's transit goes through movement.enter_sector, so mines and
offensive stations resolve exactly as in normal travel.
"""

import re

from db import (
    get_port,
    get_adjacent_sectors,
    get_player_with_ship,
    apply_port_restock,
    execute_trade,
    get_hostile_mine_total,
    get_station_in_sector,
)
from core import (
    command,
    PENDING_P2P,
    _JETTISON_COMMODITIES,
    _resolve_commodity,
    _commodity_label,
    _commodity_token,
)
from movement import enter_sector


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
