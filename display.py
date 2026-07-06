"""
Read-only rendering of game state into the short text screens players
see: sector info, the per-sector port/warps lines, and the command menu.
"""

from db import (
    get_port, get_adjacent_sectors, get_players_in_sector,
    get_station_in_sector, get_parked_ships_in_sector,
    get_ships_docked_at_station,
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
        # Spares sheltering inside the station's docking bays -- visible
        # to everyone (you can see them through the bay doors) but not
        # attackable, towable, or boardable until undocked. They go down
        # with the station if it's destroyed.
        docked = get_ships_docked_at_station(station["id"])
        if docked:
            listed = ", ".join(ship_label(s) for s in docked)
            lines.append("Docked: " + listed)
    return "\n".join(lines)


# --- Public kill log --------------------------------------------------
# At most this many kill-log entries are shown at sign-in (the most recent
# ones), with a one-line note counting any older kills not shown. The log
# covers "everything since you last played", which over a busy stretch
# could be a lot -- this keeps the sign-in briefing from flooding a slow
# radio link while still surfacing the full count.
KILL_LOG_MAX_ENTRIES = 20


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
