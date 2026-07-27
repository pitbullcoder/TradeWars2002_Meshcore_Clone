import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

DB_PATH = "meshcore_messages.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,      -- 'rx' or 'tx'
            pubkey_prefix TEXT,
            sender_name TEXT,
            text TEXT,
            timestamp TEXT NOT NULL,
            path TEXT                 -- radio route: 'direct', 'flood',
                                      -- hop hashes 'a3>7f', or NULL if
                                      -- unknown/not applicable (rx before
                                      -- this column, channel broadcasts)
        )
    """)

    # Migration for databases created before the radio path was logged.
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN path TEXT")
    except sqlite3.OperationalError:
        pass  # already has the column

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sectors (
            id INTEGER PRIMARY KEY,       -- sector number, 1..1000
            name TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS warps (
            from_sector_id INTEGER NOT NULL REFERENCES sectors(id),
            to_sector_id INTEGER NOT NULL REFERENCES sectors(id),
            PRIMARY KEY (from_sector_id, to_sector_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_warps_from ON warps(from_sector_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_id INTEGER NOT NULL UNIQUE REFERENCES sectors(id),
            port_class TEXT NOT NULL,     -- e.g. 'BBS', 'SSB', or 'STARDOCK'
            fuel_ore_dir TEXT,            -- 'B' (port buys) or 'S' (port sells)
            organics_dir TEXT,
            equipment_dir TEXT,
            fuel_ore_qty INTEGER DEFAULT 0,
            organics_qty INTEGER DEFAULT 0,
            equipment_qty INTEGER DEFAULT 0,
            fuel_ore_max INTEGER DEFAULT 0,
            organics_max INTEGER DEFAULT 0,
            equipment_max INTEGER DEFAULT 0,
            fuel_ore_price INTEGER DEFAULT 0,
            organics_price INTEGER DEFAULT 0,
            equipment_price INTEGER DEFAULT 0,
            last_restock TEXT            -- last time stock drifted toward its start level
        )
    """)

    # Migration for galaxies created before ports restocked: the timestamp
    # apply_port_restock drifts stock from. NULL on existing rows -> the
    # first access just starts the clock (see apply_port_restock).
    try:
        conn.execute("ALTER TABLE ports ADD COLUMN last_restock TEXT")
    except sqlite3.OperationalError:
        pass  # already has the column

    conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pubkey_prefix TEXT NOT NULL UNIQUE,
            name TEXT,
            sector_id INTEGER NOT NULL REFERENCES sectors(id),
            credits INTEGER NOT NULL DEFAULT 0,
            turns_remaining INTEGER NOT NULL DEFAULT 0,
            last_turn_reset TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_kill_log_seen TEXT
        )
    """)

    # Migration for databases created before the public kill log existed:
    # add the per-player "seen through" cutoff and baseline it to each
    # player's join date. The kills table starts empty, so there's nothing
    # to back-fill -- legacy players simply start seeing kills from now on.
    try:
        conn.execute("ALTER TABLE players ADD COLUMN last_kill_log_seen TEXT")
        conn.execute(
            "UPDATE players SET last_kill_log_seen = created_at "
            "WHERE last_kill_log_seen IS NULL"
        )
    except sqlite3.OperationalError:
        pass  # already has the column

    # A player may own several ships. Exactly one is "active" (the hull
    # they're flying), pointed at by players.active_ship_id; the rest are
    # parked. sector_id is where a NON-active ship sits (an active ship's
    # location is its owner's players.sector_id, and its sector_id is
    # NULL). player_id is the OWNER -- deliberately not unique anymore.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            ship_type TEXT NOT NULL,
            holds_total INTEGER NOT NULL,
            fighters INTEGER NOT NULL DEFAULT 0,
            shields INTEGER NOT NULL DEFAULT 0,
            mines INTEGER NOT NULL DEFAULT 0,
            probes INTEGER NOT NULL DEFAULT 0,
            fuel_ore INTEGER NOT NULL DEFAULT 0,
            organics INTEGER NOT NULL DEFAULT 0,
            equipment INTEGER NOT NULL DEFAULT 0,
            station_core INTEGER NOT NULL DEFAULT 0,
            sector_id INTEGER REFERENCES sectors(id)
        )
    """)

    # Migration for databases created before mines existed -- CREATE
    # TABLE IF NOT EXISTS above is a no-op against an already-existing
    # ships table, so this adds the column on its own for anyone running
    # against an older meshcore_messages.db.
    try:
        conn.execute("ALTER TABLE ships ADD COLUMN mines INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # already has the column

    # Migration for databases created before probes existed (same reasoning
    # as the mines column above).
    try:
        conn.execute("ALTER TABLE ships ADD COLUMN probes INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # already has the column

    # Migration for databases created before space stations existed: the
    # flag for whether the ship is hauling an undeployed Space Station Core
    # kit (which also occupies STATION_CORE_HOLDS holds while carried).
    try:
        conn.execute("ALTER TABLE ships ADD COLUMN station_core INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # already has the column

    # Migration for databases created back when a player could only own
    # one ship: the old schema had UNIQUE on ships.player_id (and no
    # sector_id column). SQLite can't drop a constraint in place, so the
    # table is rebuilt: copy every row into a new UNIQUE-less table (all
    # existing ships are active, so sector_id starts NULL), then swap it
    # in. Detected by the UNIQUE keyword in the stored table SQL; a table
    # freshly created by the statement above never matches.
    ships_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ships'"
    ).fetchone()["sql"]
    if "UNIQUE" in ships_sql.upper():
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE ships_multi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES players(id),
                ship_type TEXT NOT NULL,
                holds_total INTEGER NOT NULL,
                fighters INTEGER NOT NULL DEFAULT 0,
                shields INTEGER NOT NULL DEFAULT 0,
                mines INTEGER NOT NULL DEFAULT 0,
                probes INTEGER NOT NULL DEFAULT 0,
                fuel_ore INTEGER NOT NULL DEFAULT 0,
                organics INTEGER NOT NULL DEFAULT 0,
                equipment INTEGER NOT NULL DEFAULT 0,
                station_core INTEGER NOT NULL DEFAULT 0,
                sector_id INTEGER REFERENCES sectors(id)
            )
        """)
        conn.execute("""
            INSERT INTO ships_multi (id, player_id, ship_type, holds_total,
                fighters, shields, mines, probes, fuel_ore, organics,
                equipment, station_core, sector_id)
            SELECT id, player_id, ship_type, holds_total,
                fighters, shields, mines, probes, fuel_ore, organics,
                equipment, station_core, NULL
            FROM ships
        """)
        conn.execute("DROP TABLE ships")
        conn.execute("ALTER TABLE ships_multi RENAME TO ships")
        conn.execute("PRAGMA foreign_keys = ON")

    # Which of a player's ships they're flying. Backfilled from the 1:1
    # rows for databases that predate multi-ship ownership (each player
    # had exactly one ship, so MIN(id) is it).
    try:
        conn.execute(
            "ALTER TABLE players ADD COLUMN active_ship_id INTEGER REFERENCES ships(id)"
        )
    except sqlite3.OperationalError:
        pass  # already has the column
    conn.execute("""
        UPDATE players
        SET active_ship_id = (SELECT MIN(id) FROM ships WHERE ships.player_id = players.id)
        WHERE active_ship_id IS NULL
    """)

    # The parked ship a player is currently dragging behind them, if any
    # (NULL otherwise). Lives on the player so a tow survives session
    # timeouts; every hull swap/board/deletion clears it.
    try:
        conn.execute(
            "ALTER TABLE players ADD COLUMN towing_ship_id INTEGER REFERENCES ships(id)"
        )
    except sqlite3.OperationalError:
        pass  # already has the column

    # Mines a player has *deployed* into a sector (distinct from the
    # ships.mines column, which is the unlaid count carried aboard). One
    # row per (sector, owner); qty accumulates as the same player lays
    # more in the same spot. A player's own mines never detonate on them,
    # which is why ownership is tracked per row rather than as a single
    # per-sector count -- see clear_hostile_mines / get_hostile_mine_total.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_mines (
            sector_id INTEGER NOT NULL REFERENCES sectors(id),
            player_id INTEGER NOT NULL REFERENCES players(id),
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (sector_id, player_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sector_mines_sector ON sector_mines(sector_id)")

    # Combat notices waiting to be delivered to a victim the next time they
    # sign in: who hit them, where, what happened, and when. `delivered`
    # flips to 1 once the victim has been shown the notice.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attack_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            victim_id INTEGER NOT NULL REFERENCES players(id),
            attacker_name TEXT NOT NULL,
            sector_id INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attack_events_victim "
        "ON attack_events(victim_id, delivered)"
    )

    # Public kill log: a global, append-only record of every ship or escape
    # pod destroyed -- by combat or by mines. Unlike attack_events (a
    # private notice for the victim), this is shown to ANY player on
    # sign-in: every kill newer than their last sign-in
    # (players.last_kill_log_seen). killer_name is NULL for a mine kill;
    # kind is 'ship' (a hull blown up, pilot ejected to a pod) or 'pod' (an
    # escape pod wiped, pilot reset). Names are stored as-of the kill so the
    # log stays a faithful historical record even if a player renames.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            victim_name TEXT NOT NULL,
            killer_name TEXT,            -- NULL = killed by mines
            sector_id INTEGER NOT NULL,
            kind TEXT NOT NULL,          -- 'ship' or 'pod'
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kills_created ON kills(created_at)")

    # Player-built space stations -- one per sector (sector_id UNIQUE).
    # shields/fighters are current strength (both start at 0); their caps
    # come from `level` via station_caps(). shields_enabled toggles the
    # fuel-powered shield; fuel/organics/equipment is the shared material
    # stockpile (fuel powers shields AND, with the others, feeds upgrades).
    # posture is 'defensive' or 'offensive'; engage_pct is the % of
    # fighters the station commits when it auto-attacks a non-owner who
    # enters while it's offensive. last_fuel_burn anchors the daily shield
    # upkeep; upgrade_to / upgrade_started_at track an in-progress upgrade.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_id INTEGER NOT NULL UNIQUE REFERENCES sectors(id),
            owner_id INTEGER NOT NULL REFERENCES players(id),
            owner_name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            shields INTEGER NOT NULL DEFAULT 0,
            fighters INTEGER NOT NULL DEFAULT 0,
            shields_enabled INTEGER NOT NULL DEFAULT 0,
            fuel INTEGER NOT NULL DEFAULT 0,
            organics INTEGER NOT NULL DEFAULT 0,
            equipment INTEGER NOT NULL DEFAULT 0,
            posture TEXT NOT NULL DEFAULT 'defensive',
            engage_pct INTEGER NOT NULL DEFAULT 100,
            last_fuel_burn TEXT NOT NULL,
            upgrade_to INTEGER,
            upgrade_started_at TEXT,
            credits INTEGER NOT NULL DEFAULT 0
        )
    """)

    # The station's credit treasury -- a safe-deposit box for its owner.
    # Deposited credits aren't on the player, so a pod-kill's credit
    # reset can't touch them; the flip side is the whole treasury is lost
    # if the station is destroyed (the row deletion takes it).
    try:
        conn.execute(
            "ALTER TABLE stations ADD COLUMN credits INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # already has the column

    # Which station a spare (parked) ship is docked inside, NULL when it's
    # sitting out in the open. Docked ships are excluded from
    # get_parked_ships_in_sector, which is what shelters them from attack
    # (and from tow/board targeting) until they're undocked -- and what
    # ties their fate to the station's (see delete_station). Placed after
    # the stations table so the FK target exists.
    try:
        conn.execute(
            "ALTER TABLE ships ADD COLUMN docked_station_id INTEGER REFERENCES stations(id)"
        )
    except sqlite3.OperationalError:
        pass  # already has the column

    conn.commit()
    conn.close()


# --- Game balance constants ---
HOME_SECTOR = 1
STARTING_CREDITS = 1000
DAILY_TURNS = 100

# Sectors 1..SAFE_ZONE_MAX_SECTOR are the protected safe zone around the
# Stardock. It's the single source of truth for that boundary, shared by
# galaxy generation (which fully interconnects these sectors so the
# Stardock is always reachable and can't be walled off) and the gameplay
# rules that forbid laying mines or fighting inside it. New players spawn
# at the Stardock (HOME_SECTOR) and need a safe runway to get going.
SAFE_ZONE_MAX_SECTOR = 10

# Valid sector id range. MAX_SECTOR_ID matches galaxy.py's NUM_SECTORS;
# movement and probe targeting both validate against these.
MIN_SECTOR_ID = 1
MAX_SECTOR_ID = 1000

# When a pilot already in an escape pod is finished off (by combat or
# mines), they don't just lose the pod -- they're wiped back to a fresh
# start: a default-hull ship at the home sector with their credits reset
# to this amount.
POD_KILL_RESET_CREDITS = 20000

# Dragging one of your own unmanned hulls behind you. Slow going: every
# sector moved while towing burns this many turns instead of one, and a
# move is refused outright when fewer remain (rather than burning the
# player down to zero mid-haul).
TOW_TURNS_PER_SECTOR = 5

# Turns refill once a day at this hour, Eastern time. America/New_York
# (rather than a fixed UTC offset) keeps "3am Eastern" correct through
# daylight-saving changes.
TURN_RESET_HOUR = 3
_EASTERN = ZoneInfo("America/New_York")


def _last_reset_boundary(now_utc):
    """The most recent daily reset instant (TURN_RESET_HOUR Eastern), as a
    UTC datetime. A player whose last_turn_reset predates this is due for a
    refill."""
    now_eastern = now_utc.astimezone(_EASTERN)
    boundary = now_eastern.replace(
        hour=TURN_RESET_HOUR, minute=0, second=0, microsecond=0
    )
    if now_eastern < boundary:
        boundary -= timedelta(days=1)  # today's 3am hasn't arrived yet
    return boundary.astimezone(timezone.utc)


def _reset_days_between(earlier_iso, now_utc):
    """How many daily (3am-Eastern) reset boundaries have been crossed
    between `earlier_iso` (an ISO-8601 timestamp) and `now_utc`. Counts
    whole reset-days via Eastern calendar dates, so it's correct across
    daylight-saving changes (unlike dividing a timedelta by 24h). Used for
    both station shield upkeep and upgrade completion."""
    earlier = datetime.fromisoformat(earlier_iso)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    d_now = _last_reset_boundary(now_utc).astimezone(_EASTERN).date()
    d_earlier = _last_reset_boundary(earlier).astimezone(_EASTERN).date()
    return max(0, (d_now - d_earlier).days)

DEFAULT_SHIP_TYPE = "Falcon"

# The hull a destroyed player is dropped into. Looked up in SHIP_CATALOG
# like any other, but flagged "purchasable": False there so it never
# appears in the shipyard.
ESCAPE_POD_SHIP = "Escape Pod"

# --- Shipyard catalog --------------------------------------------------
# Every purchasable hull, keyed by name. To add a new ship later, just
# add another entry here -- nothing else in db.py or main.py needs to
# change structurally to support it.
#
#   classification -- flavor text shown in the shipyard menu.
#   price          -- credits to buy this hull outright. 0 for the
#                      Falcon: it's the free starter ship, never an
#                      actual purchase -- a player only ever lands back
#                      on it via a sell-back (see sell_value() below).
#   base_*         -- what a freshly acquired hull starts with.
#   max_*          -- the per-stat caps a Stardock refit can push that
#                      hull's holds/fighters/shields/mines up to. Looked
#                      up per the player's *current* ship_type (see
#                      upgrade_ship_stat and cmd_stardock_step in
#                      main.py) rather than a single fixed limit, since
#                      different hulls cap out at different points.
#                      max_mines is 0 for any hull without a mine bay --
#                      main.py's refit menu hides the Mines option
#                      entirely for those ships rather than showing a
#                      0/0 line.
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
        "max_probes": 25,   # best recon range in the fleet -- a scout
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
        "max_probes": 10,   # entry-level hauler -- cargo, almost no teeth
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
        "max_probes": 15,   # aggressive mid-tier warship with a small mine bay
    },
    "Nautilus": {
        "classification": "Minelayer",
        "price": 180000,
        "base_holds": 25,
        "base_fighters": 40,
        "base_shields": 120,
        "base_mines": 10,   # comes with a starter load of mines aboard
        "max_holds": 80,
        "max_fighters": 400,
        "max_shields": 1000,
        "max_mines": 150,   # biggest mine bay anywhere
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
        "max_fighters": 3000,   # out-guns the Bismark...
        "max_shields": 800,     # ...but thin shields -- a glass cannon
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
        "max_shields": 5000,    # the toughest hull to crack -- a shield tank
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
    # Not a hull anyone buys -- it's where a destroyed pilot ends up. A
    # mine field punches through a ship and the player is ejected into one
    # of these (see eject-to-pod handling in main.py): no holds, no
    # fighters, no shields, no mine bay, nothing. "purchasable": False
    # keeps it out of the Stardock shipyard menu (main.py filters on it)
    # so it can live in the catalog -- as the destruction code needs --
    # without ever showing up as something to buy. A pilot flying one can
    # still trade it back in for the free Falcon at a Stardock, which is
    # the intended way to climb back out of the pod.
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

# Fraction of a hull's catalog price refunded when it's traded in at the
# Stardock shipyard.
SHIP_RESALE_FRACTION = 0.5


def sell_value(ship_type):
    """Trade-in credit for handing back `ship_type` at the Stardock
    shipyard -- a flat fraction of its catalog price. Always 0 for the
    Falcon, since it's priced at 0cr to begin with."""
    return round(SHIP_CATALOG[ship_type]["price"] * SHIP_RESALE_FRACTION)


STARTER_SHIP = {
    "ship_type": DEFAULT_SHIP_TYPE,
    "holds_total": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_holds"],
    "fighters": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_fighters"],
    "shields": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_shields"],
    "mines": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_mines"],
}


# --- Space stations ----------------------------------------------------
# A player buys a Space Station Core kit at the Stardock, hauls it to a
# sector outside the safe zone, and deploys it as a permanent structure.
STATION_CORE_PRICE = 5_000_000   # credits to buy the kit
STATION_CORE_HOLDS = 150         # cargo holds the kit occupies while hauling

# Per-level capacity caps. A station starts at level 1; levels 2-4 are
# unlocked by upgrades (see STATION_UPGRADES). Shields and fighters both
# start at 0 -- fighters are transferred in from a ship, shields are
# powered up by burning fuel. These caps are the ceiling each can reach at
# a given level. (Caps are a tunable design choice -- adjust freely.)
STATION_LEVEL_CAPS = {
    1: {"max_shields": 1000, "max_fighters": 1000},
    2: {"max_shields": 2500, "max_fighters": 2500},
    3: {"max_shields": 5000, "max_fighters": 5000},
    4: {"max_shields": 10000, "max_fighters": 10000},
}
STATION_MAX_LEVEL = 4

# What it takes to upgrade TO each level: credits (paid by the owner),
# materials drawn from the station's own stockpile, and the number of
# daily (3am-Eastern) boundaries that must pass before it completes.
STATION_UPGRADES = {
    2: {"credits": 10_000_000, "fuel": 2500, "organics": 2000, "equipment": 1000, "days": 5},
    3: {"credits": 12_500_000, "fuel": 3500, "organics": 2500, "equipment": 1750, "days": 8},
    4: {"credits": 15_000_000, "fuel": 5000, "organics": 3500, "equipment": 2000, "days": 12},
}

# Fuel burned per active shield point per day while shields are enabled
# (0.1 -> 100 fuel/day to run 1000 shields). Drawn from the same fuel
# stockpile that feeds upgrades. If a day's burn can't be covered, shields
# drop to 0 and power off until refuelled and re-enabled.
SHIELD_FUEL_BURN_PER_SHIELD = 0.1


def station_caps(level):
    """(max_shields, max_fighters) for a station at `level`."""
    caps = STATION_LEVEL_CAPS[level]
    return caps["max_shields"], caps["max_fighters"]


def station_daily_fuel_burn(level):
    """Whole fuel units burned per day to run a fully-charged shield at
    `level` (rounded). This is the cost of keeping shields enabled."""
    max_shields, _ = station_caps(level)
    return round(SHIELD_FUEL_BURN_PER_SHIELD * max_shields)


# How many spare ships a station can shelter in its docking bays, per
# station level: Lvl 1 -> 2 ships, Lvl 2 -> 4, Lvl 3 -> 6, Lvl 4 -> 8.
DOCKED_SHIPS_PER_LEVEL = 2


def station_dock_capacity(level):
    """Max spare ships a station at `level` can hold in its docking bays."""
    return DOCKED_SHIPS_PER_LEVEL * level

# Stardock refit prices, in credits per unit. Keyed by the ships column
# each upgrade applies to, so callers can go straight from a column name
# to its price without a separate lookup table. Same price regardless of
# ship type -- only the per-stat *caps* (SHIP_CATALOG[...]["max_*"])
# vary by hull.
STARDOCK_PRICES = {
    "holds_total": 500,
    "fighters": 50,
    "shields": 25,
    "mines": 2500,
    "probes": 100,
}


def get_all_warps():
    """
    Return the full warp graph as {sector_id: [adjacent_sector_ids]}.
    Used for pathfinding (BFS) when a requested destination isn't directly
    adjacent -- one query here is far cheaper than calling
    get_adjacent_sectors() once per node visited during the search.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT from_sector_id, to_sector_id FROM warps ORDER BY from_sector_id, to_sector_id"
    ).fetchall()
    conn.close()

    graph = {}
    for row in rows:
        graph.setdefault(row["from_sector_id"], []).append(row["to_sector_id"])
    return graph


def get_adjacent_sectors(sector_id):
    """Return a sorted list of sector ids directly reachable by warp from sector_id."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT to_sector_id FROM warps WHERE from_sector_id = ? ORDER BY to_sector_id",
        (sector_id,)
    ).fetchall()
    conn.close()
    return [row["to_sector_id"] for row in rows]


def get_port(sector_id):
    """Fetch the port in a sector, as a dict. None if the sector has no port."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM ports WHERE sector_id = ?", (sector_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


_TRADEABLE_COMMODITIES = ("fuel_ore", "organics", "equipment")


def execute_trade(player_id, port_id, commodity, qty, total_price, player_is_buying):
    """
    Apply a completed trade in a single transaction:
      - player's credits go down (buying) or up (selling) by total_price
      - player's ship cargo for `commodity` goes up (buying) or down (selling) by qty
      - the port's stock for `commodity` goes down (buying, since the
        port is selling and depleting) or up (selling, since the port is
        buying and filling) by qty
    Caller is responsible for validating quantities/credits/capacity
    beforehand -- this function does not re-check anything.
    """
    if commodity not in _TRADEABLE_COMMODITIES:
        raise ValueError(f"invalid commodity column: {commodity}")

    credit_delta = -total_price if player_is_buying else total_price
    cargo_delta = qty if player_is_buying else -qty
    port_qty_delta = -qty if player_is_buying else qty

    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits + ? WHERE id = ?",
        (credit_delta, player_id)
    )
    conn.execute(
        f"UPDATE ships SET {commodity} = {commodity} + ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (cargo_delta, player_id)
    )
    conn.execute(
        f"UPDATE ports SET {commodity}_qty = {commodity}_qty + ? WHERE id = ?",
        (port_qty_delta, port_id)
    )
    conn.commit()
    conn.close()


# Number of daily (3am-Eastern) boundaries over which a port's stock drifts
# back toward its starting level. The drift is proportional -- each boundary
# closes 1/PORT_RESTOCK_DAYS of the remaining gap to the target -- so the
# recovery is front-loaded and asymptotic: a fully-drained selling port is
# ~67% restocked after 5 days and only nudges the last bit closed over the
# following weeks, so it never snaps straight back to full for a farmer.
PORT_RESTOCK_DAYS = 5


def _restock_qty(qty, target, days):
    """A commodity's stock after `days` reset-boundaries, closing
    1/PORT_RESTOCK_DAYS of the gap to `target` each boundary (the gap decays
    by (1 - 1/PORT_RESTOCK_DAYS) per day). Works both ways: a selling
    commodity climbs toward its capacity, a buying commodity drains toward
    zero. Asymptotic -- it only lands exactly on `target` once the remaining
    gap rounds away."""
    if days <= 0 or qty == target:
        return qty
    remaining_gap = (target - qty) * ((1 - 1 / PORT_RESTOCK_DAYS) ** days)
    return round(target - remaining_gap)


def apply_port_restock(port_id, now=None):
    """
    Bring a port's stock up to date lazily, the same way turns and station
    upkeep work -- there's no background job, so a port catches up the next
    time anyone trades there. Each tradeable commodity drifts toward its
    starting level (a selling commodity 'S' toward its capacity, a buying
    commodity 'B' toward empty) by _restock_qty for the whole run of
    reset-days since last_restock.

    Persists and returns the refreshed port dict (None if the port is
    gone). A NULL last_restock -- an old row from before this existed, or a
    freshly generated galaxy -- just starts the clock with no drift.
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM ports WHERE id = ?", (port_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    port = dict(row)
    now = now or datetime.now(timezone.utc)

    if port["last_restock"] is None:
        conn.execute("UPDATE ports SET last_restock = ? WHERE id = ?",
                     (now.isoformat(), port_id))
        conn.commit()
        conn.close()
        port["last_restock"] = now.isoformat()
        return port

    days = _reset_days_between(port["last_restock"], now)
    if days <= 0:
        conn.close()
        return port

    updates = {}
    for c in _TRADEABLE_COMMODITIES:
        direction = port[f"{c}_dir"]
        if direction not in ("S", "B"):
            continue  # dir-less (Stardock) commodities have no stock to drift
        target = port[f"{c}_max"] if direction == "S" else 0
        new_qty = _restock_qty(port[f"{c}_qty"], target, days)
        if new_qty != port[f"{c}_qty"]:
            updates[f"{c}_qty"] = new_qty

    # Advance the clock even if nothing moved (everything already at target),
    # so the elapsed days don't pile up and jump the stock next time.
    columns = list(updates.keys()) + ["last_restock"]
    values = list(updates.values()) + [now.isoformat()]
    set_clause = ", ".join(f"{col} = ?" for col in columns)
    conn.execute(f"UPDATE ports SET {set_clause} WHERE id = ?", values + [port_id])
    conn.commit()
    conn.close()

    port.update(updates)
    port["last_restock"] = now.isoformat()
    return port


_UPGRADEABLE_SHIP_STATS = ("holds_total", "fighters", "shields", "mines", "probes")


def upgrade_ship_stat(player_id, stat_column, qty, total_price):
    """
    Apply a completed Stardock refit purchase in a single transaction:
      - player's credits go down by total_price
      - the ship's `stat_column` (holds_total, fighters, or shields)
        goes up by qty
    Caller is responsible for validating quantity/credits/the ship's
    per-stat cap beforehand -- this function does not re-check anything.
    """
    if stat_column not in _UPGRADEABLE_SHIP_STATS:
        raise ValueError(f"invalid ship stat column: {stat_column}")

    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits - ? WHERE id = ?",
        (total_price, player_id)
    )
    conn.execute(
        f"UPDATE ships SET {stat_column} = {stat_column} + ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (qty, player_id)
    )
    conn.commit()
    conn.close()


def buy_ship(player_id, ship_type, holds_total, fighters, shields, mines, credit_delta):
    """
    Apply a completed Stardock shipyard transaction in a single
    transaction -- either buying a different hull, or selling the
    current one back (the caller just passes the Falcon's base stats
    and a positive credit_delta for that case):
      - player's credits change by credit_delta (negative for a net
        purchase cost, positive for a net refund)
      - the ACTIVE ship's type and holds_total/fighters/shields/mines are
        replaced with the new hull's values (parked ships are untouched)
      - any cargo being carried is cleared -- a hull swap empties the
        hold, since the old ship's cargo doesn't transfer to the new one
      - any tow in progress is released (the old hull -- and whatever
        rigging held the tow line -- is gone)
    Caller is responsible for validating affordability beforehand --
    this function does not re-check anything.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits + ?, towing_ship_id = NULL WHERE id = ?",
        (credit_delta, player_id)
    )
    conn.execute(
        """UPDATE ships
           SET ship_type = ?, holds_total = ?, fighters = ?, shields = ?, mines = ?,
               probes = 0, fuel_ore = 0, organics = 0, equipment = 0, station_core = 0
           WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)""",
        (ship_type, holds_total, fighters, shields, mines, player_id)
    )
    conn.commit()
    conn.close()


def park_and_buy_ship(player_id, park_sector_id, ship_type, holds_total,
                      fighters, shields, mines, credit_delta):
    """
    Buy a new hull while KEEPING the current one: in a single
    transaction, the active ship is parked in `park_sector_id` (with
    everything still aboard -- cargo, fighters, shields, mines, probes,
    even a carried Station Core kit), a fresh ship of `ship_type` is
    created and made active, credits change by credit_delta (negative:
    the full purchase price, no trade-in), and any tow is released.
    Returns the new ship's id. Caller validates affordability.
    """
    conn = get_connection()
    conn.execute(
        """UPDATE ships SET sector_id = ?
           WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)""",
        (park_sector_id, player_id)
    )
    cur = conn.execute(
        """INSERT INTO ships (player_id, ship_type, holds_total, fighters, shields,
                              mines, probes, fuel_ore, organics, equipment,
                              station_core, sector_id)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, NULL)""",
        (player_id, ship_type, holds_total, fighters, shields, mines)
    )
    new_ship_id = cur.lastrowid
    conn.execute(
        """UPDATE players
           SET active_ship_id = ?, towing_ship_id = NULL, credits = credits + ?
           WHERE id = ?""",
        (new_ship_id, credit_delta, player_id)
    )
    conn.commit()
    conn.close()
    return new_ship_id


def swap_active_ship(player_id, ship_id, park_sector_id):
    """
    Board one of the player's own parked ships (`ship_id`), in a single
    transaction: the current active ship is parked in `park_sector_id`
    -- unless it's an Escape Pod, which is scuttled (deleted) instead,
    since a pod isn't a hull worth keeping -- the target ship becomes
    active (sector_id back to NULL), and any tow is released. Caller
    validates ownership and that the ship is parked in the player's
    sector; this does not re-check.
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT ships.id, ships.ship_type FROM ships
           JOIN players ON players.active_ship_id = ships.id
           WHERE players.id = ?""",
        (player_id,)
    ).fetchone()
    # Repoint the player at the new hull FIRST -- active_ship_id
    # references ships(id), so a pod can't be deleted while it's still
    # the row the player points at.
    conn.execute("UPDATE ships SET sector_id = NULL WHERE id = ?", (ship_id,))
    conn.execute(
        "UPDATE players SET active_ship_id = ?, towing_ship_id = NULL WHERE id = ?",
        (ship_id, player_id)
    )
    if row["ship_type"] == ESCAPE_POD_SHIP:
        conn.execute("DELETE FROM ships WHERE id = ?", (row["id"],))
    else:
        conn.execute(
            "UPDATE ships SET sector_id = ? WHERE id = ?",
            (park_sector_id, row["id"])
        )
    conn.commit()
    conn.close()


def get_ship(ship_id):
    """A single ship row (parked or active) as a dict, joined with its
    owner's name as owner_name (player_id doubles as owner_id). None if
    no such ship."""
    conn = get_connection()
    row = conn.execute(
        """SELECT ships.*, ships.player_id AS owner_id, players.name AS owner_name
           FROM ships JOIN players ON players.id = ships.player_id
           WHERE ships.id = ?""",
        (ship_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_parked_ships_in_sector(sector_id):
    """Every UNMANNED ship sitting OUT IN THE OPEN in `sector_id`
    (including one being towed by a player standing there -- it follows
    its tower's sector), in stable id order, each with
    owner_id/owner_name and enough combat stats to size up or resolve an
    attack. Active ships never appear here (their sector_id is NULL),
    and neither do ships docked inside a station -- that exclusion is
    exactly what makes docking protective, since attack/tow/board all
    target through this query."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT ships.id, ships.player_id AS owner_id, players.name AS owner_name,
                  ships.ship_type, ships.fighters, ships.shields
           FROM ships JOIN players ON players.id = ships.player_id
           WHERE ships.sector_id = ? AND ships.docked_station_id IS NULL
           ORDER BY ships.id""",
        (sector_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def set_parked_ship_defenses(ship_id, shields, fighters):
    """Write back a PARKED ship's shields/fighters after it soaked an
    attack it survived (the parked-ship counterpart of
    set_ship_defenses, which targets a player's active ship)."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET shields = ?, fighters = ? WHERE id = ?",
        (shields, fighters, ship_id)
    )
    conn.commit()
    conn.close()


def move_ship_to_sector(ship_id, sector_id):
    """Relocate a parked/towed ship. Used by towing: each move the tower
    makes drags the towed hull's sector_id along."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET sector_id = ? WHERE id = ?", (sector_id, ship_id)
    )
    conn.commit()
    conn.close()


def delete_ship(ship_id):
    """Remove a ship outright (destroyed unmanned, or sold while parked
    at the Stardock). Any player towing it lets go -- their
    towing_ship_id is cleared so no dangling reference survives."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET towing_ship_id = NULL WHERE towing_ship_id = ?",
        (ship_id,)
    )
    conn.execute("DELETE FROM ships WHERE id = ?", (ship_id,))
    conn.commit()
    conn.close()


def set_towing(player_id, ship_id):
    """Engage (ship_id) or release (None) a player's tow."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET towing_ship_id = ? WHERE id = ?",
        (ship_id, player_id)
    )
    conn.commit()
    conn.close()


def transfer_station_credits(player_id, station_id, amount):
    """Move credits between a player's pocket and their station's
    treasury in one transaction: positive `amount` deposits (player ->
    treasury), negative withdraws. One statement pair, one commit -- a
    radio drop mid-visit can never duplicate or vanish credits. Caller
    validates ownership and that the source side can afford it."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits - ? WHERE id = ?",
        (amount, player_id)
    )
    conn.execute(
        "UPDATE stations SET credits = credits + ? WHERE id = ?",
        (amount, station_id)
    )
    conn.commit()
    conn.close()


def dock_ship_at_station(ship_id, station_id):
    """Shelter a spare ship inside a station's docking bay. Any tow line
    attached to the hull is released in the same transaction (you can't
    drag a ship that's inside a hangar). Caller validates ownership,
    location, and bay capacity."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET towing_ship_id = NULL WHERE towing_ship_id = ?",
        (ship_id,)
    )
    conn.execute(
        "UPDATE ships SET docked_station_id = ? WHERE id = ?",
        (station_id, ship_id)
    )
    conn.commit()
    conn.close()


def undock_ship(ship_id):
    """Move a docked spare back out into the open: it becomes an ordinary
    parked ship in its sector -- boardable, towable, and attackable again."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET docked_station_id = NULL WHERE id = ?", (ship_id,)
    )
    conn.commit()
    conn.close()


def get_ships_docked_at_station(station_id):
    """Every spare ship sheltering inside `station_id`'s docking bays, in
    stable id order, shaped like get_parked_ships_in_sector's rows."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT ships.id, ships.player_id AS owner_id, players.name AS owner_name,
                  ships.ship_type, ships.fighters, ships.shields
           FROM ships JOIN players ON players.id = ships.player_id
           WHERE ships.docked_station_id = ?
           ORDER BY ships.id""",
        (station_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def move_player_to_sector(player_id, sector_id):
    """Update a player's current sector. Caller is responsible for validating
    that the move is legal (e.g. that sector_id is adjacent)."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET sector_id = ? WHERE id = ?", (sector_id, player_id)
    )
    conn.commit()
    conn.close()


def lay_mines(player_id, sector_id, qty):
    """
    Deploy `qty` of a player's carried mines into `sector_id`, in one
    transaction:
      - the ship's `mines` count (unlaid, aboard) drops by qty
      - the sector_mines row for (sector_id, player_id) goes up by qty,
        created if this is the player's first mine in that sector
    Caller validates qty (> 0, <= mines aboard) and the sector rule
    (not a mine-free sector) beforehand -- this does not re-check.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET mines = mines - ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (qty, player_id)
    )
    conn.execute(
        """INSERT INTO sector_mines (sector_id, player_id, qty)
           VALUES (?, ?, ?)
           ON CONFLICT(sector_id, player_id) DO UPDATE SET qty = qty + excluded.qty""",
        (sector_id, player_id, qty)
    )
    conn.commit()
    conn.close()


def get_hostile_mine_total(sector_id, player_id):
    """Total mines in `sector_id` deployed by players OTHER than
    `player_id` -- i.e. how many would detonate if `player_id` entered.
    A player's own mines are excluded, since they never detonate on
    their owner. 0 if the sector is clear (for that player)."""
    conn = get_connection()
    row = conn.execute(
        """SELECT COALESCE(SUM(qty), 0) AS total
           FROM sector_mines
           WHERE sector_id = ? AND player_id != ?""",
        (sector_id, player_id)
    ).fetchone()
    conn.close()
    return row["total"]


def clear_hostile_mines(sector_id, player_id):
    """Remove every mine in `sector_id` NOT owned by `player_id` -- the
    detonated ones are spent and gone. The entering player's own mines
    (if any) are left in place. Pairs with get_hostile_mine_total: read
    the count, then clear, when resolving an entry."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM sector_mines WHERE sector_id = ? AND player_id != ?",
        (sector_id, player_id)
    )
    conn.commit()
    conn.close()


def consume_probe(player_id):
    """Spend one probe from a player's ship when they launch it. Caller
    validates there's at least one aboard beforehand."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET probes = probes - 1 WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (player_id,)
    )
    conn.commit()
    conn.close()


def detonate_one_hostile_mine(sector_id, player_id):
    """Spend a SINGLE mine in `sector_id` owned by someone other than
    `player_id`. A probe is so fragile that one mine destroys it, leaving
    the rest of the field intact -- unlike a ship arrival, which sets off
    the whole lot via clear_hostile_mines. No-op if there are no hostile
    mines there."""
    conn = get_connection()
    row = conn.execute(
        """SELECT player_id, qty FROM sector_mines
           WHERE sector_id = ? AND player_id != ? AND qty > 0
           LIMIT 1""",
        (sector_id, player_id)
    ).fetchone()
    if row is not None:
        owner = row["player_id"]
        if row["qty"] <= 1:
            conn.execute(
                "DELETE FROM sector_mines WHERE sector_id = ? AND player_id = ?",
                (sector_id, owner)
            )
        else:
            conn.execute(
                "UPDATE sector_mines SET qty = qty - 1 WHERE sector_id = ? AND player_id = ?",
                (sector_id, owner)
            )
        conn.commit()
    conn.close()


def set_ship_defenses(player_id, shields, fighters):
    """Set a ship's shields and fighters to absolute values -- used to
    write back what's left after a mine hit the player survived. (A hit
    they don't survive goes through buy_ship instead, swapping the whole
    hull for an Escape Pod.)"""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET shields = ?, fighters = ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (shields, fighters, player_id)
    )
    conn.commit()
    conn.close()


def set_ship_cargo(player_id, fuel_ore, organics, equipment):
    """Set a ship's commodity cargo to absolute values -- used when
    offloading materials into a station's stockpile."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET fuel_ore = ?, organics = ?, equipment = ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (fuel_ore, organics, equipment, player_id)
    )
    conn.commit()
    conn.close()


def adjust_player_credits(player_id, delta):
    """Change a player's credit balance by `delta` (negative to spend).
    Used for purchases that aren't a hull swap -- the Station Core kit and
    station upgrades."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits + ? WHERE id = ?",
        (delta, player_id)
    )
    conn.commit()
    conn.close()


def get_player_with_ship(pubkey_prefix):
    """Fetch a player joined with their ship, as a dict. None if no such player."""
    conn = get_connection()
    row = conn.execute("""
        SELECT players.*,
               ships.id AS ship_id, ships.ship_type, ships.holds_total,
               ships.fighters, ships.shields, ships.mines,
               ships.probes,
               ships.fuel_ore, ships.organics, ships.equipment,
               ships.station_core
        FROM players
        JOIN ships ON ships.id = players.active_ship_id
        WHERE players.pubkey_prefix = ?
    """, (pubkey_prefix,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_players_in_sector(sector_id, exclude_player_id=None):
    """Players whose current position is `sector_id`, in stable id order,
    each as a {"name", "fighters"} dict -- the fighter count is what the
    sector-info screen advertises so a pilot can size up who they're
    sharing the sector with before deciding to attack (shields are
    deliberately NOT included; an opponent's shield strength stays hidden
    until shots are actually traded). `exclude_player_id` drops one entry
    -- the viewer, so they don't see themselves among the ships present.
    Note this reflects each player's last recorded sector, not whether
    they're currently online (the game has no online/offline state beyond
    the single active-session lock), so it includes pilots parked there
    and logged off."""
    conn = get_connection()
    sql = """
        SELECT players.name, ships.fighters
        FROM players
        JOIN ships ON ships.id = players.active_ship_id
        WHERE players.sector_id = ?
    """
    params = [sector_id]
    if exclude_player_id is not None:
        sql += " AND players.id != ?"
        params.append(exclude_player_id)
    sql += " ORDER BY players.id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_ships_in_sector(sector_id, exclude_player_id=None):
    """Player+ship rows for everyone currently in `sector_id` (optionally
    excluding the viewer), as dicts -- enough to pick and resolve an
    attack target: id, name, ship_type, fighters, shields, credits."""
    conn = get_connection()
    sql = """
        SELECT players.id, players.name, players.credits,
               ships.ship_type, ships.fighters, ships.shields
        FROM players
        JOIN ships ON ships.id = players.active_ship_id
        WHERE players.sector_id = ?
    """
    params = [sector_id]
    if exclude_player_id is not None:
        sql += " AND players.id != ?"
        params.append(exclude_player_id)
    sql += " ORDER BY players.id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def record_attack_event(victim_id, attacker_name, sector_id, outcome):
    """Queue a combat notice for `victim_id` to receive on their next
    sign-in. `outcome` is a short tag ('attacked', 'destroyed',
    'pod_destroyed') the presentation layer turns into a message."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO attack_events (victim_id, attacker_name, sector_id, outcome, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (victim_id, attacker_name, sector_id, outcome, now)
    )
    conn.commit()
    conn.close()


def pop_attack_events(player_id):
    """Return `player_id`'s undelivered combat notices (oldest first) as
    dicts, marking them delivered so they're shown only once."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, attacker_name, sector_id, outcome, created_at
           FROM attack_events
           WHERE victim_id = ? AND delivered = 0
           ORDER BY created_at, id""",
        (player_id,)
    ).fetchall()
    events = [dict(row) for row in rows]
    if events:
        conn.executemany(
            "UPDATE attack_events SET delivered = 1 WHERE id = ?",
            [(e["id"],) for e in events]
        )
        conn.commit()
    conn.close()
    return events


def record_kill(victim_name, killer_name, sector_id, kind):
    """Append a kill to the public log. `killer_name` is the attacker's
    display name, or None for a kill by mines. `kind` is 'ship' (a hull
    destroyed, pilot ejected into a pod) or 'pod' (an escape pod wiped,
    pilot reset). Names are recorded as given, so the log is a stable
    historical record."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO kills (victim_name, killer_name, sector_id, kind, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (victim_name, killer_name, sector_id, kind, now)
    )
    conn.commit()
    conn.close()


def get_kills_since(cutoff_iso, limit=None):
    """Public kill-log rows strictly newer than `cutoff_iso` (an ISO-8601
    UTC timestamp), oldest first, as dicts. A None cutoff returns nothing
    -- a player with no established baseline hasn't "signed on" yet, so
    there's no window to report. `limit`, if given, caps the row count."""
    if cutoff_iso is None:
        return []
    conn = get_connection()
    sql = (
        "SELECT victim_name, killer_name, sector_id, kind, created_at "
        "FROM kills WHERE created_at > ? ORDER BY created_at, id"
    )
    params = [cutoff_iso]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_kill_log_cutoff(player_id):
    """The timestamp through which `player_id` has already seen the public
    kill log -- effectively their last sign-in. Kills newer than this are
    what they'll be shown next sign-in. None only if the row is missing."""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_kill_log_seen FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    conn.close()
    return row["last_kill_log_seen"] if row else None


def mark_kill_log_seen(player_id):
    """Advance `player_id`'s kill-log cutoff to now, so the kills they were
    just shown won't appear again on their next sign-in."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE players SET last_kill_log_seen = ? WHERE id = ?",
        (now, player_id)
    )
    conn.commit()
    conn.close()


# --- Space stations ----------------------------------------------------

def set_ship_station_core(player_id, has_core):
    """Set (or clear) the undeployed Station Core kit flag on a ship."""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET station_core = ? WHERE id = (SELECT active_ship_id FROM players WHERE id = ?)",
        (1 if has_core else 0, player_id)
    )
    conn.commit()
    conn.close()


def get_station_in_sector(sector_id):
    """The station in `sector_id` as a dict, or None. Pure read -- it does
    NOT apply daily upkeep (call apply_station_upkeep for that)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM stations WHERE sector_id = ?", (sector_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_station(station_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_stations_by_owner(owner_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM stations WHERE owner_id = ? ORDER BY sector_id", (owner_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_station(owner_id, owner_name, sector_id):
    """Deploy a fresh level-1 station for `owner_id` in `sector_id`.
    Returns the new station dict. Caller must have verified the sector is
    outside the safe zone and has no station already."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO stations (sector_id, owner_id, owner_name, last_fuel_burn)
           VALUES (?, ?, ?, ?)""",
        (sector_id, owner_id, owner_name, now)
    )
    station_id = cur.lastrowid
    conn.commit()
    conn.close()
    return get_station(station_id)


def delete_station(station_id):
    """Remove a destroyed station -- and every spare ship docked inside
    it, which shares its fate. Any tow reference to a doomed hull is
    cleared first (defensive; docking already releases tows)."""
    conn = get_connection()
    conn.execute(
        """UPDATE players SET towing_ship_id = NULL
           WHERE towing_ship_id IN
             (SELECT id FROM ships WHERE docked_station_id = ?)""",
        (station_id,)
    )
    conn.execute("DELETE FROM ships WHERE docked_station_id = ?", (station_id,))
    conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    conn.commit()
    conn.close()


def _update_station(station_id, **fields):
    """Set arbitrary station columns in one statement."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    conn.execute(
        f"UPDATE stations SET {cols} WHERE id = ?",
        (*fields.values(), station_id)
    )
    conn.commit()
    conn.close()


def deposit_to_station(station_id, fuel=0, organics=0, equipment=0):
    """Add materials to a station's stockpile (caller moves them off the
    ship separately)."""
    conn = get_connection()
    conn.execute(
        "UPDATE stations SET fuel = fuel + ?, organics = organics + ?, "
        "equipment = equipment + ? WHERE id = ?",
        (fuel, organics, equipment, station_id)
    )
    conn.commit()
    conn.close()


def set_station_defenses(station_id, shields, fighters):
    _update_station(station_id, shields=shields, fighters=fighters)


def set_station_posture(station_id, posture, engage_pct=None):
    if engage_pct is None:
        _update_station(station_id, posture=posture)
    else:
        _update_station(station_id, posture=posture, engage_pct=engage_pct)


def set_station_shields(station_id, enabled, shields, last_fuel_burn=None):
    """Toggle a station's shields. Enabling charges them to `shields` and
    stamps last_fuel_burn (so the daily upkeep clock restarts); disabling
    sets shields to 0."""
    fields = {"shields_enabled": 1 if enabled else 0, "shields": shields}
    if last_fuel_burn is not None:
        fields["last_fuel_burn"] = last_fuel_burn
    _update_station(station_id, **fields)


def apply_station_upkeep(station_id, now=None):
    """
    Bring a station up to date lazily: complete any in-progress upgrade
    whose days have elapsed, then charge daily shield fuel for the whole
    reset-days since last_fuel_burn. If shields are enabled and the fuel
    stockpile can't cover a day, shields drop to 0 and power off. Persists
    and returns the refreshed station dict (None if it no longer exists).
    """
    station = get_station(station_id)
    if station is None:
        return None
    now = now or datetime.now(timezone.utc)

    changes = {}

    # Complete a finished upgrade first (so the new level's caps/costs apply).
    if station["upgrade_to"] is not None and station["upgrade_started_at"]:
        needed = STATION_UPGRADES[station["upgrade_to"]]["days"]
        if _reset_days_between(station["upgrade_started_at"], now) >= needed:
            station["level"] = station["upgrade_to"]
            changes.update(level=station["upgrade_to"],
                           upgrade_to=None, upgrade_started_at=None)

    # Daily shield fuel burn.
    if station["shields_enabled"]:
        days = _reset_days_between(station["last_fuel_burn"], now)
        if days > 0:
            daily = station_daily_fuel_burn(station["level"])
            max_shields, _ = station_caps(station["level"])
            affordable = station["fuel"] // daily if daily > 0 else days
            if days <= affordable:
                changes["fuel"] = station["fuel"] - days * daily
                changes["shields"] = max_shields            # recharged for the day
            else:
                changes["fuel"] = station["fuel"] - affordable * daily
                changes["shields"] = 0
                changes["shields_enabled"] = 0
            changes["last_fuel_burn"] = now.isoformat()

    if changes:
        _update_station(station_id, **changes)
        return get_station(station_id)
    return station


def start_station_upgrade(station_id, target_level, now=None):
    """Begin an upgrade to `target_level`: consume the material cost from
    the stockpile and stamp the start time. Credits are deducted from the
    owner by the caller. Caller validates affordability/materials first."""
    now = now or datetime.now(timezone.utc)
    spec = STATION_UPGRADES[target_level]
    conn = get_connection()
    conn.execute(
        "UPDATE stations SET fuel = fuel - ?, organics = organics - ?, "
        "equipment = equipment - ?, upgrade_to = ?, upgrade_started_at = ? "
        "WHERE id = ?",
        (spec["fuel"], spec["organics"], spec["equipment"],
         target_level, now.isoformat(), station_id)
    )
    conn.commit()
    conn.close()
    return get_station(station_id)


def create_player(pubkey_prefix, name):
    """Create a new player + starter ship. Returns the player dict (with ship)."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()

    cur = conn.execute(
        """INSERT INTO players (pubkey_prefix, name, sector_id, credits, turns_remaining, last_turn_reset, created_at, last_kill_log_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (pubkey_prefix, name, HOME_SECTOR, STARTING_CREDITS, DAILY_TURNS, now, now, now)
    )
    player_id = cur.lastrowid

    cur = conn.execute(
        """INSERT INTO ships (player_id, ship_type, holds_total, fighters, shields, mines, fuel_ore, organics, equipment)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)""",
        (player_id, STARTER_SHIP["ship_type"], STARTER_SHIP["holds_total"],
         STARTER_SHIP["fighters"], STARTER_SHIP["shields"], STARTER_SHIP["mines"])
    )
    ship_id = cur.lastrowid
    conn.execute(
        "UPDATE players SET active_ship_id = ? WHERE id = ?", (ship_id, player_id)
    )

    conn.commit()
    conn.close()
    return get_player_with_ship(pubkey_prefix)


def get_or_create_player(pubkey_prefix, name):
    """Returns (player_dict, is_new). Creates player + ship on first contact."""
    player = get_player_with_ship(pubkey_prefix)
    if player is not None:
        return player, False
    return create_player(pubkey_prefix, name), True


def reset_turns_if_needed(player_id):
    """Refill turns to DAILY_TURNS if the player hasn't been reset since
    the most recent daily reset boundary (3am Eastern). Checked on sign-in,
    so there's no maintenance job: the first time a player is seen after
    3am Eastern, their turns top back up. Using America/New_York means the
    boundary tracks EST/EDT automatically across daylight-saving changes."""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_turn_reset FROM players WHERE id = ?", (player_id,)
    ).fetchone()

    last_reset = datetime.fromisoformat(row["last_turn_reset"])
    now = datetime.now(timezone.utc)

    if last_reset < _last_reset_boundary(now):
        conn.execute(
            "UPDATE players SET turns_remaining = ?, last_turn_reset = ? WHERE id = ?",
            (DAILY_TURNS, now.isoformat(), player_id)
        )
        conn.commit()

    conn.close()


def spend_turns(player_id, n):
    """Charge `n` turns at once (towing costs several per sector),
    clamped so the balance never drops below zero."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET turns_remaining = MAX(0, turns_remaining - ?) "
        "WHERE id = ?",
        (n, player_id)
    )
    conn.commit()
    conn.close()


def spend_turn(player_id):
    """Charge a single turn for a sector-to-sector move, never dropping
    below zero (the WHERE guard makes a spend at 0 turns a no-op). Other
    actions -- trading, attacking, laying mines, sending probes -- are
    free; only physically moving costs a turn."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET turns_remaining = turns_remaining - 1 "
        "WHERE id = ? AND turns_remaining > 0",
        (player_id,)
    )
    conn.commit()
    conn.close()


def get_last_tx_time(pubkey_prefix, text):
    """The most recent timestamp (ISO string) at which exactly `text` was
    transmitted to `pubkey_prefix`, or None if it never was. Used to
    schedule the periodic channel advertisement off the messages log
    itself, so a bot restart resumes the countdown instead of
    re-broadcasting early."""
    conn = get_connection()
    row = conn.execute(
        """SELECT MAX(timestamp) FROM messages
           WHERE direction = 'tx' AND pubkey_prefix = ? AND text = ?""",
        (pubkey_prefix, text)
    ).fetchone()
    conn.close()
    return row[0]


def log_message(direction, pubkey_prefix, sender_name, text, path=None):
    """`path` is the radio route the message took, when known: 'direct',
    'flood', a '>'-joined string of raw repeater hop hashes (e.g.
    'a3>7f'), or 'N hops' for received messages (which carry only the
    hop count). None when unknown or not applicable (channel
    broadcasts)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (direction, pubkey_prefix, sender_name, text, timestamp, path) VALUES (?, ?, ?, ?, ?, ?)",
        (direction, pubkey_prefix, sender_name, text, datetime.now(timezone.utc).isoformat(), path)
    )
    conn.commit()
    conn.close()