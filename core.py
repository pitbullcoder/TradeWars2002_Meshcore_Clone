"""
Foundational game state and the command registry, shared by every other
module. Kept dependency-light (only the db layer) so nothing here imports
a feature module -- that's what keeps the package free of import cycles.

The PENDING_* dicts are the single source of truth for in-progress player
flows; main.py re-exports and clears them on (re)load, so the test suite's
importlib.reload(main) still resets state cleanly even though the dicts
physically live here.
"""

from db import get_port


# pubkey -> list of remaining sector ids (not including the player's
# current sector) for a multi-hop warp that's awaiting yes/no confirmation
# one hop at a time. Populated by cmd_move, consumed by cmd_confirm_warp.
PENDING_WARPS = {}


# pubkey -> dict tracking an in-progress port trade across its three
# steps (pick item -> pick quantity -> confirm). Populated by cmd_trade,
# consumed/advanced by cmd_trade_step.
PENDING_TRADES = {}


# pubkey -> dict tracking an in-progress Stardock refit visit (buying
# cargo holds, fighters, or shields). Populated by cmd_trade when
# docking at the Stardock, consumed/advanced by cmd_stardock_step.
PENDING_UPGRADES = {}


# pubkey -> dict {"target_id":, "target_name":} for an attack that's been
# aimed at a ship in the sector but is waiting on the attacker to say how
# many fighters to commit. Populated by cmd_attack, consumed by
# cmd_attack_step (which re-fetches the target's live ship row before
# resolving, so a stale count can't be acted on).
PENDING_ATTACKS = {}


# pubkey -> dict {"station_id":, "stage":, ...} tracking an in-progress
# visit to a player's own space station (deposit materials, transfer
# fighters, toggle shields, set posture, upgrade). Populated by
# cmd_station, advanced by cmd_station_step.
PENDING_STATIONS = {}


# pubkey -> dict tracking an in-progress port-to-port shuttle (p2p): the
# two sectors, the forward/backward commodities, where the next leg moves
# to, and the running profit. Populated by cmd_p2p (after the player picks
# a commodity), advanced one leg at a time by cmd_p2p_step.
PENDING_P2P = {}


# pubkey -> dict {"queue": [ship dicts], "idx": int} cycling yes/no through
# the player's own parked ships in the sector for a tow. Populated by
# cmd_tow, consumed by cmd_tow_step.
PENDING_TOWS = {}


# pubkey -> dict {"queue": [ship dicts], "idx": int} cycling yes/no through
# the player's own parked ships in the sector for a board/swap. Populated
# by cmd_board, consumed by cmd_board_step.
PENDING_BOARDS = {}


def ship_label(ship):
    """The display name for an unmanned hull: owner + type + ship id,
    e.g. "Joe's Kestrel #12". The #id is what disambiguates two parked
    ships of the same type (and what 'a #12' / 'tow #12' / 'board #12'
    target directly)."""
    return f"{ship['owner_name']}'s {ship['ship_type']} #{ship['id']}"


# Map of command -> (description, async handler(ctx, args) -> str)
COMMANDS = {}


def command(name, *aliases, description="", menu="main"):
    def decorator(func):
        # `menu` files a command under a help submenu ("main" shows in the
        # top-level menu; anything else, e.g. "combat", is listed only by
        # that submenu). Stored on the function so the COMMANDS tuple shape
        # stays (description, handler) for every existing unpacker.
        func._menu = menu
        for n in (name, *aliases):
            COMMANDS[n] = (description, func)
        return func
    return decorator


# Map of command -> async handler(args) -> str, for messages on the public channel.
# Unlike COMMANDS (private DMs), there's no player/Ctx here and unrecognized
# verbs are silently ignored rather than replying with a menu — channel
# chatter that isn't a bot command shouldn't get an automatic reply.
CHANNEL_COMMANDS = {}


def channel_command(name, *aliases):
    def decorator(func):
        for n in (name, *aliases):
            CHANNEL_COMMANDS[n] = func
        return func
    return decorator


class Ctx:
    """Per-message context passed to every command handler."""
    def __init__(self, mc, pubkey, sender, player):
        self.mc = mc
        self.pubkey = pubkey
        self.sender = sender
        self.player = player


def parse(text):
    parts = text.strip().split(maxsplit=1)
    verb = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    return verb, args


def _warp_confirm_options(sector_id):
    """
    '(p/yes/no)' if sector_id has a port to dock at, else just
    '(yes/no)' -- the 'p' option is only worth advertising on a "Warp
    to: ...?" prompt when there's actually something to dock at right
    where the player is currently standing.
    """
    return "(p/yes/no)" if get_port(sector_id) is not None else "(yes/no)"


def _resume_navigation_suffix(pubkey, sector_id):
    """
    If a multi-hop warp confirmation is still pending for pubkey,
    return text re-prompting to continue that route -- formatted the
    same as the original "Warp to: ...?" prompt, just starting from the
    player's current sector instead of the one they set out from.
    Returns "" if there's no route in progress.

    This is what lets a player dock at a port partway through a plotted
    multi-hop course (see cmd_confirm_warp's "p"/"port" handling)
    without losing the rest of the route: cmd_trade_step and
    cmd_stardock_step both call this once their visit ends, appending
    the result to their own closing message so the player is dropped
    straight back into the warp confirmation for the remaining hops.
    """
    remaining = PENDING_WARPS.get(pubkey)
    if not remaining:
        return ""
    route = " -> ".join(str(s) for s in [sector_id] + remaining)
    return f"\n\nWarp to: {route}? {_warp_confirm_options(sector_id)}"
