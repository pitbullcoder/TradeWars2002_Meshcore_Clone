"""
Read-only rendering of game state into the short text screens players
see: sector info, the per-sector port/warps lines, and the command menu.
"""

from db import (
    get_port, get_adjacent_sectors, get_players_in_sector,
    get_station_in_sector, get_parked_ships_in_sector,
)

from core import COMMANDS, ship_label


def build_menu():
    lines = ["Available commands:"]
    seen = set()
    for cmd, (description, handler) in COMMANDS.items():
        if handler in seen:
            continue
        seen.add(handler)
        # Commands filed under a submenu (e.g. combat) aren't listed at the
        # top level -- they're reached via that submenu's own command.
        if getattr(handler, "_menu", "main") != "main":
            continue
        lines.append(f"  {cmd} - {description}")
    return "\n".join(lines)


def build_submenu(menu_name):
    """List just the commands filed under `menu_name` (e.g. 'combat').
    Mirrors build_menu's dedupe-by-handler so aliases don't double up.
    Returns a friendly note if nothing is filed there."""
    lines = [f"{menu_name.capitalize()} commands:"]
    seen = set()
    for cmd, (description, handler) in COMMANDS.items():
        if handler in seen:
            continue
        if getattr(handler, "_menu", "main") != menu_name:
            continue
        seen.add(handler)
        lines.append(f"  {cmd} - {description}")
    if len(lines) == 1:
        lines.append(f"  (no {menu_name} commands)")
    return "\n".join(lines)


def format_port_line(sector_id):
    port = get_port(sector_id)
    if port is None:
        return "Port: none"
    if port["port_class"] == "STARDOCK":
        return "Port: Stardock"
    return f"Port: {port['port_class']}"


def format_warps_line(sector_id):
    adjacent = get_adjacent_sectors(sector_id)
    warps = ", ".join(str(s) for s in adjacent) if adjacent else "none"
    return f"Warps: {warps}"


def build_sector_info(sector_id, viewer_id=None):
    """
    The sector info screen: sector number, then port, then adjacent
    sectors, each on their own line. Shown for the `info` command and
    automatically appended whenever a player's sector actually changes.

    `viewer_id` is the player looking (so they're left out of the ship
    list below). When other pilots are parked in the sector, a final
    "Ships here:" line names them and how many fighters each is flying
    (e.g. "Bob (1000 ftr)") so a pilot can weigh an attack; shields are
    deliberately left off, so an opponent's shield strength stays unknown
    until combat. The line is omitted entirely when the sector is empty of
    other ships, so a solo sector reads exactly as before.
    """
    lines = [
        f"Sec{sector_id}",
        format_port_line(sector_id),
        format_warps_line(sector_id),
    ]
    others = get_players_in_sector(sector_id, viewer_id)
    if others:
        listed = ", ".join(f"{o['name']} ({o['fighters']} ftr)" for o in others)
        lines.append("Ships here: " + listed)
    # Unmanned hulls parked here (spares left behind at a shipyard
    # purchase, or a hull being towed through). Everyone's are listed --
    # including the viewer's own -- named by owner + type + ship id so
    # 'a #12' / 'tow #12' / 'board #12' can target them. Like the ships
    # line, fighters are advertised but shields stay hidden.
    parked = get_parked_ships_in_sector(sector_id)
    if parked:
        listed = ", ".join(f"{ship_label(s)} ({s['fighters']} ftr)" for s in parked)
        lines.append("Unmanned: " + listed)
    station = get_station_in_sector(sector_id)
    if station is not None:
        # Mirror the ship display: show the station's fighter strength but
        # not its shields (which stay hidden until shots are traded).
        lines.append(
            f"Space Station - {station['owner_name']} ({station['fighters']} ftr)"
        )
    return "\n".join(lines)
