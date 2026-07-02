"""
Tests for db-level turn handling: the 3am-Eastern reset boundary, the
sign-in refill, and per-move turn spending. Like galaxy_test.py (and
unlike main_test.py, which stubs db out), these run against the real db
module backed by a throwaway SQLite file. Run directly:

    python db_test.py
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import db


class TurnResetBoundaryTests(unittest.TestCase):
    """_last_reset_boundary: the most recent 3am-Eastern instant, in UTC.
    Eastern is UTC-4 in summer (EDT) and UTC-5 in winter (EST), so 3am
    Eastern is 07:00 UTC in summer and 08:00 UTC in winter."""

    def test_after_3am_eastern_uses_todays_boundary(self):
        # 2026-06-27 10:00 UTC = 06:00 EDT, past 3am -> today's 3am EDT = 07:00 UTC.
        now = datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 6, 27, 7, 0, tzinfo=timezone.utc),
        )

    def test_before_3am_eastern_uses_yesterdays_boundary(self):
        # 2026-06-27 05:00 UTC = 01:00 EDT, before 3am -> yesterday's 3am EDT.
        now = datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 6, 26, 7, 0, tzinfo=timezone.utc),
        )

    def test_winter_boundary_tracks_est(self):
        # 2026-01-15 10:00 UTC = 05:00 EST, past 3am -> today's 3am EST = 08:00 UTC.
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
        )


class TurnDbTests(unittest.TestCase):
    """reset_turns_if_needed and spend_turn against a real db."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "turns.db")
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")

    def _set(self, last_reset_dt, turns):
        conn = db.get_connection()
        conn.execute(
            "UPDATE players SET last_turn_reset = ?, turns_remaining = ? WHERE id = ?",
            (last_reset_dt.isoformat(), turns, self.player["id"]),
        )
        conn.commit()
        conn.close()

    def _turns(self):
        conn = db.get_connection()
        value = conn.execute(
            "SELECT turns_remaining FROM players WHERE id = ?", (self.player["id"],)
        ).fetchone()["turns_remaining"]
        conn.close()
        return value

    def test_daily_turn_limit_is_100(self):
        self.assertEqual(db.DAILY_TURNS, 100)

    def test_spend_turn_decrements_and_never_goes_negative(self):
        self._set(datetime.now(timezone.utc), turns=2)
        db.spend_turn(self.player["id"])
        self.assertEqual(self._turns(), 1)
        db.spend_turn(self.player["id"])
        db.spend_turn(self.player["id"])  # already at 0 -> no-op
        self.assertEqual(self._turns(), 0)

    def test_reset_refills_when_overdue(self):
        self._set(datetime.now(timezone.utc) - timedelta(days=2), turns=7)
        db.reset_turns_if_needed(self.player["id"])
        self.assertEqual(self._turns(), db.DAILY_TURNS)

    def test_no_reset_when_already_topped_up_today(self):
        self._set(datetime.now(timezone.utc), turns=7)
        db.reset_turns_if_needed(self.player["id"])
        self.assertEqual(self._turns(), 7)  # unchanged


class PlayersInSectorTests(unittest.TestCase):
    """get_players_in_sector against a real db: it reports each present
    pilot's fighters (what the sector-info screen advertises) but never
    their shields, in stable id order, and honors exclude_player_id."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "presence.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany("INSERT INTO sectors (id) VALUES (?)", [(1,), (5,)])
        conn.commit()
        conn.close()

    def _place(self, pubkey, name, sector_id, fighters, shields):
        player = db.create_player(pubkey, name)  # starts at the home sector
        db.move_player_to_sector(player["id"], sector_id)
        db.set_ship_defenses(player["id"], shields, fighters)
        return player

    def test_lists_present_pilots_with_fighters_and_no_shields(self):
        self._place("pk1", "Alice", 5, fighters=10, shields=111)
        self._place("pk2", "Bob", 5, fighters=20, shields=222)
        self._place("pk3", "Cleo", 1, fighters=30, shields=333)  # parked elsewhere

        here = db.get_players_in_sector(5)

        self.assertEqual(
            here,
            [{"name": "Alice", "fighters": 10}, {"name": "Bob", "fighters": 20}],
        )
        # Shields are never part of the row -- they stay hidden.
        self.assertTrue(all("shields" not in row for row in here))

    def test_exclude_player_id_drops_the_viewer(self):
        alice = self._place("pk1", "Alice", 5, fighters=10, shields=0)
        self._place("pk2", "Bob", 5, fighters=20, shields=0)

        here = db.get_players_in_sector(5, exclude_player_id=alice["id"])

        self.assertEqual(here, [{"name": "Bob", "fighters": 20}])

    def test_empty_sector_returns_nothing(self):
        self.assertEqual(db.get_players_in_sector(5), [])


class KillLogDbTests(unittest.TestCase):
    """record_kill / get_kills_since / cutoff handling against a real db."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "kills.db")
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.commit()
        conn.close()

    def _insert_kill(self, victim, killer, sector, kind, created_at):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO kills (victim_name, killer_name, sector_id, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (victim, killer, sector, kind, created_at),
        )
        conn.commit()
        conn.close()

    def test_record_kill_persists_combat_and_mine_kills(self):
        db.record_kill("Bob", "Alice", 5, "ship")
        db.record_kill("Cleo", None, 9, "pod")   # None killer = mines

        rows = db.get_kills_since("2000-01-01T00:00:00+00:00")
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["victim_name"], rows[0]["killer_name"]), ("Bob", "Alice"))
        self.assertEqual(rows[0]["kind"], "ship")
        self.assertIsNone(rows[1]["killer_name"])
        self.assertEqual((rows[1]["victim_name"], rows[1]["kind"]), ("Cleo", "pod"))

    def test_get_kills_since_filters_by_cutoff_oldest_first(self):
        self._insert_kill("A", "K", 1, "ship", "2026-06-27T10:00:00+00:00")
        self._insert_kill("B", "K", 2, "ship", "2026-06-27T12:00:00+00:00")
        self._insert_kill("C", None, 3, "pod", "2026-06-27T14:00:00+00:00")

        rows = db.get_kills_since("2026-06-27T11:00:00+00:00")
        self.assertEqual([r["victim_name"] for r in rows], ["B", "C"])  # A excluded, ordered

    def test_get_kills_since_none_returns_empty(self):
        self._insert_kill("A", "K", 1, "ship", "2026-06-27T10:00:00+00:00")
        self.assertEqual(db.get_kills_since(None), [])

    def test_get_kills_since_respects_limit(self):
        for i in range(5):
            self._insert_kill(f"V{i}", "K", i, "ship", f"2026-06-27T1{i}:00:00+00:00")
        rows = db.get_kills_since("2000-01-01T00:00:00+00:00", limit=2)
        self.assertEqual([r["victim_name"] for r in rows], ["V0", "V1"])  # oldest two

    def test_new_player_cutoff_is_their_join_time(self):
        player = db.create_player("pk", "Alice")
        cutoff = db.get_kill_log_cutoff(player["id"])
        self.assertIsNotNone(cutoff)
        # A kill from long before they joined never shows.
        self._insert_kill("Old", "K", 1, "ship", "2000-01-01T00:00:00+00:00")
        self.assertEqual(db.get_kills_since(cutoff), [])

    def test_mark_kill_log_seen_advances_the_cutoff(self):
        player = db.create_player("pk", "Alice")
        before = db.get_kill_log_cutoff(player["id"])
        db.mark_kill_log_seen(player["id"])
        after = db.get_kill_log_cutoff(player["id"])
        self.assertGreaterEqual(after, before)


class StationDbTests(unittest.TestCase):
    """Station persistence + the lazy daily upkeep math (fuel burn,
    shield shutdown, upgrade completion) against a real db."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "stations.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany("INSERT INTO sectors (id) VALUES (?)", [(1,), (20,)])
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")
        self.station = db.create_station(self.player["id"], "Alice", 20)

    def test_create_defaults(self):
        st = self.station
        self.assertEqual(
            (st["level"], st["shields"], st["fighters"], st["shields_enabled"]),
            (1, 0, 0, 0),
        )
        self.assertEqual((st["posture"], st["engage_pct"]), ("defensive", 100))
        self.assertEqual((st["fuel"], st["organics"], st["equipment"]), (0, 0, 0))

    def test_caps_and_daily_burn(self):
        self.assertEqual(db.station_caps(1), (1000, 1000))
        self.assertEqual(db.station_caps(4), (10000, 10000))
        self.assertEqual(db.station_daily_fuel_burn(1), 100)   # 0.1 * 1000
        self.assertEqual(db.station_daily_fuel_burn(4), 1000)  # 0.1 * 10000

    def test_deposit_posture_and_defenses(self):
        db.deposit_to_station(self.station["id"], fuel=100, organics=50, equipment=25)
        db.set_station_defenses(self.station["id"], shields=300, fighters=400)
        db.set_station_posture(self.station["id"], "offensive", engage_pct=60)
        st = db.get_station(self.station["id"])
        self.assertEqual((st["fuel"], st["organics"], st["equipment"]), (100, 50, 25))
        self.assertEqual((st["shields"], st["fighters"]), (300, 400))
        self.assertEqual((st["posture"], st["engage_pct"]), ("offensive", 60))

    def test_shield_fuel_burns_per_day_and_recharges(self):
        db.deposit_to_station(self.station["id"], fuel=350)
        db.set_station_shields(self.station["id"], enabled=True, shields=300,  # damaged
                               last_fuel_burn="2026-06-10T12:00:00+00:00")
        # 3 reset-days later: 3 * 100 = 300 fuel burned, shields recharged.
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        st = db.apply_station_upkeep(self.station["id"], now=now)
        self.assertEqual(st["fuel"], 50)
        self.assertEqual(st["shields"], 1000)        # recharged to the level cap
        self.assertEqual(st["shields_enabled"], 1)

    def test_shields_power_down_when_fuel_cannot_cover_a_day(self):
        db.deposit_to_station(self.station["id"], fuel=250)  # only 2 days affordable
        db.set_station_shields(self.station["id"], enabled=True, shields=1000,
                               last_fuel_burn="2026-06-10T12:00:00+00:00")
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)  # 3 days
        st = db.apply_station_upkeep(self.station["id"], now=now)
        self.assertEqual(st["fuel"], 50)             # 2 days burned, then ran out
        self.assertEqual(st["shields"], 0)
        self.assertEqual(st["shields_enabled"], 0)

    def test_upgrade_completes_only_after_required_days(self):
        db._update_station(self.station["id"], upgrade_to=2,
                           upgrade_started_at="2026-06-10T12:00:00+00:00")
        four = db.apply_station_upkeep(
            self.station["id"], now=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((four["level"], four["upgrade_to"]), (1, 2))   # 4 days: not yet
        five = db.apply_station_upkeep(
            self.station["id"], now=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((five["level"], five["upgrade_to"]), (2, None))  # 5 days: done
        self.assertEqual(db.station_caps(five["level"]), (2500, 2500))

    def test_start_upgrade_draws_materials_from_stockpile(self):
        db.deposit_to_station(self.station["id"], fuel=3000, organics=3000, equipment=3000)
        st = db.start_station_upgrade(self.station["id"], 2)
        spec = db.STATION_UPGRADES[2]
        self.assertEqual(st["fuel"], 3000 - spec["fuel"])
        self.assertEqual(st["organics"], 3000 - spec["organics"])
        self.assertEqual(st["equipment"], 3000 - spec["equipment"])
        self.assertEqual(st["upgrade_to"], 2)

    def test_get_station_in_sector_and_delete(self):
        self.assertEqual(db.get_station_in_sector(20)["id"], self.station["id"])
        self.assertIsNone(db.get_station_in_sector(1))
        db.delete_station(self.station["id"])
        self.assertIsNone(db.get_station_in_sector(20))


class MultiShipMigrationTests(unittest.TestCase):
    """init_db against a database from the one-ship era: the UNIQUE
    constraint on ships.player_id must be rebuilt away, sector_id added,
    and players.active_ship_id backfilled from the old 1:1 rows."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy.db")
        # Build the OLD schema by hand -- players without
        # active_ship_id/towing_ship_id, ships with UNIQUE(player_id) and
        # no sector_id -- with a player and their single ship in it.
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.execute("""
            CREATE TABLE players (
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
        conn.execute("""
            CREATE TABLE ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL UNIQUE REFERENCES players(id),
                ship_type TEXT NOT NULL,
                holds_total INTEGER NOT NULL,
                fighters INTEGER NOT NULL DEFAULT 0,
                shields INTEGER NOT NULL DEFAULT 0,
                mines INTEGER NOT NULL DEFAULT 0,
                probes INTEGER NOT NULL DEFAULT 0,
                fuel_ore INTEGER NOT NULL DEFAULT 0,
                organics INTEGER NOT NULL DEFAULT 0,
                equipment INTEGER NOT NULL DEFAULT 0,
                station_core INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO players (pubkey_prefix, name, sector_id, credits, "
            "turns_remaining, last_turn_reset, created_at) "
            "VALUES ('pk', 'Alice', 1, 500, 10, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO ships (player_id, ship_type, holds_total, fighters, "
            "shields, mines, fuel_ore) VALUES (1, 'Kestrel', 15, 20, 20, 0, 7)"
        )
        conn.commit()
        conn.close()

        db.init_db()  # the migration under test

    def test_unique_constraint_is_gone_and_sector_id_added(self):
        conn = db.get_connection()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ships'"
        ).fetchone()["sql"]
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(ships)")}
        conn.close()
        self.assertNotIn("UNIQUE", sql.upper())
        self.assertIn("sector_id", columns)

    def test_ship_data_survives_and_active_ship_is_backfilled(self):
        p = db.get_player_with_ship("pk")
        self.assertEqual(p["ship_type"], "Kestrel")
        self.assertEqual(p["fuel_ore"], 7)          # row copied intact
        self.assertEqual(p["active_ship_id"], p["ship_id"])
        self.assertIsNone(p["towing_ship_id"])

    def test_second_ship_for_the_same_player_is_now_allowed(self):
        new_id = db.park_and_buy_ship(1, 1, "Mule", 40, 0, 30, 0, -40000)
        conn = db.get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM ships WHERE player_id = 1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)
        self.assertEqual(db.get_player_with_ship("pk")["ship_id"], new_id)

    def test_migration_is_idempotent(self):
        db.init_db()  # a second run must not duplicate or fail
        conn = db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class MultiShipDbTests(unittest.TestCase):
    """The multi-ship functions against a real db: parking on purchase,
    boarding, towing bookkeeping, parked-ship queries, and turn bundles."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ships.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO sectors (id) VALUES (?)", [(i,) for i in range(1, 21)]
        )
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")

    def _fresh(self):
        return db.get_player_with_ship("pk")

    def test_create_player_sets_the_active_ship(self):
        p = self._fresh()
        self.assertIsNotNone(p["active_ship_id"])
        self.assertEqual(p["active_ship_id"], p["ship_id"])

    def test_park_and_buy_keeps_the_old_hull_with_everything_aboard(self):
        db.set_ship_cargo(self.player["id"], 5, 0, 2)   # load the Falcon
        old_ship_id = self._fresh()["ship_id"]

        new_id = db.park_and_buy_ship(
            self.player["id"], 1, "Kestrel", 15, 20, 20, 0, credit_delta=-8000
        )

        p = self._fresh()
        self.assertEqual(p["ship_id"], new_id)
        self.assertEqual(p["ship_type"], "Kestrel")
        self.assertEqual(p["fuel_ore"], 0)              # new hull starts empty
        self.assertEqual(p["credits"], self.player["credits"] - 8000)
        old = db.get_ship(old_ship_id)
        self.assertEqual(old["sector_id"], 1)           # parked right here
        self.assertEqual(old["fuel_ore"], 5)            # cargo stays aboard
        self.assertEqual(old["equipment"], 2)
        self.assertEqual(old["owner_name"], "Alice")

    def test_parked_ships_query_and_ship_writes_target_only_the_active_hull(self):
        old_ship_id = self._fresh()["ship_id"]
        db.park_and_buy_ship(self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0)

        parked = db.get_parked_ships_in_sector(1)
        self.assertEqual([s["id"] for s in parked], [old_ship_id])
        self.assertEqual(parked[0]["owner_id"], self.player["id"])

        # A defensive write goes to the ACTIVE Kestrel, not the parked Falcon.
        db.set_ship_defenses(self.player["id"], 99, 88)
        self.assertEqual(self._fresh()["shields"], 99)
        old = db.get_ship(old_ship_id)
        self.assertEqual(old["shields"], 10)            # Falcon untouched

        # And the parked-ship counterpart goes to the parked hull only.
        db.set_parked_ship_defenses(old_ship_id, 3, 4)
        self.assertEqual(db.get_ship(old_ship_id)["shields"], 3)
        self.assertEqual(self._fresh()["shields"], 99)

    def test_swap_active_ship_parks_current_and_boards_target(self):
        old_ship_id = self._fresh()["ship_id"]
        new_id = db.park_and_buy_ship(
            self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0
        )
        db.set_towing(self.player["id"], old_ship_id)

        # Board the parked Falcon from the Kestrel, over in Sec5.
        db.swap_active_ship(self.player["id"], old_ship_id, 5)

        p = self._fresh()
        self.assertEqual(p["ship_id"], old_ship_id)
        self.assertEqual(p["ship_type"], "Falcon")
        self.assertIsNone(p["towing_ship_id"])          # boarding drops the tow
        kestrel = db.get_ship(new_id)
        self.assertEqual(kestrel["sector_id"], 5)       # parked where we stood

    def test_swapping_out_of_an_escape_pod_scuttles_it(self):
        old_ship_id = self._fresh()["ship_id"]
        db.park_and_buy_ship(self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0)
        pod_id = self._fresh()["ship_id"]
        # The active Kestrel is destroyed: buy_ship swaps it to a pod in place.
        db.buy_ship(self.player["id"], "Escape Pod", 0, 0, 0, 0, 0)

        db.swap_active_ship(self.player["id"], old_ship_id, 1)

        self.assertEqual(self._fresh()["ship_type"], "Falcon")
        self.assertIsNone(db.get_ship(pod_id))          # pod row deleted

    def test_delete_ship_clears_any_tow_reference(self):
        old_ship_id = self._fresh()["ship_id"]
        db.park_and_buy_ship(self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0)
        db.set_towing(self.player["id"], old_ship_id)

        db.delete_ship(old_ship_id)

        self.assertIsNone(db.get_ship(old_ship_id))
        self.assertIsNone(self._fresh()["towing_ship_id"])

    def test_buy_ship_replaces_only_the_active_hull_and_releases_the_tow(self):
        old_ship_id = self._fresh()["ship_id"]
        db.park_and_buy_ship(self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0)
        db.set_towing(self.player["id"], old_ship_id)

        db.buy_ship(self.player["id"], "Mule", 40, 0, 30, 0, credit_delta=-40000)

        p = self._fresh()
        self.assertEqual(p["ship_type"], "Mule")
        self.assertIsNone(p["towing_ship_id"])
        old = db.get_ship(old_ship_id)
        self.assertEqual(old["ship_type"], "Falcon")    # parked hull untouched

    def test_move_ship_to_sector_relocates_a_parked_hull(self):
        old_ship_id = self._fresh()["ship_id"]
        db.park_and_buy_ship(self.player["id"], 1, "Kestrel", 15, 20, 20, 0, 0)

        db.move_ship_to_sector(old_ship_id, 7)

        self.assertEqual(db.get_ship(old_ship_id)["sector_id"], 7)
        self.assertEqual([s["id"] for s in db.get_parked_ships_in_sector(7)],
                         [old_ship_id])
        self.assertEqual(db.get_parked_ships_in_sector(1), [])

    def test_spend_turns_charges_in_bulk_and_clamps_at_zero(self):
        conn = db.get_connection()
        conn.execute("UPDATE players SET turns_remaining = 7 WHERE id = ?",
                     (self.player["id"],))
        conn.commit()
        conn.close()

        db.spend_turns(self.player["id"], 5)
        self.assertEqual(self._fresh()["turns_remaining"], 2)
        db.spend_turns(self.player["id"], 5)
        self.assertEqual(self._fresh()["turns_remaining"], 0)   # clamped


class StationDockingDbTests(unittest.TestCase):
    """Docking bays against a real db: dock/undock bookkeeping, the
    parked-ships exclusion that shelters docked hulls, capacity math, and
    delete_station taking its docked ships down with it."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "dock.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO sectors (id) VALUES (?)", [(i,) for i in range(1, 25)]
        )
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")
        # Park the starter Falcon in Sec20 and fly a Kestrel.
        self.spare_id = db.get_player_with_ship("pk")["ship_id"]
        db.park_and_buy_ship(self.player["id"], 20, "Kestrel", 15, 20, 20, 0, 0)
        self.station = db.create_station(self.player["id"], "Alice", 20)

    def test_dock_capacity_is_two_per_level(self):
        self.assertEqual(db.station_dock_capacity(1), 2)
        self.assertEqual(db.station_dock_capacity(2), 4)
        self.assertEqual(db.station_dock_capacity(3), 6)
        self.assertEqual(db.station_dock_capacity(4), 8)

    def test_docking_hides_a_ship_from_the_parked_query(self):
        self.assertEqual(
            [s["id"] for s in db.get_parked_ships_in_sector(20)], [self.spare_id]
        )

        db.dock_ship_at_station(self.spare_id, self.station["id"])

        self.assertEqual(db.get_parked_ships_in_sector(20), [])   # sheltered
        docked = db.get_ships_docked_at_station(self.station["id"])
        self.assertEqual([s["id"] for s in docked], [self.spare_id])
        self.assertEqual(docked[0]["owner_name"], "Alice")

    def test_undocking_returns_it_to_the_open(self):
        db.dock_ship_at_station(self.spare_id, self.station["id"])
        db.undock_ship(self.spare_id)
        self.assertEqual(
            [s["id"] for s in db.get_parked_ships_in_sector(20)], [self.spare_id]
        )
        self.assertEqual(db.get_ships_docked_at_station(self.station["id"]), [])

    def test_docking_releases_a_tow_on_the_hull(self):
        db.set_towing(self.player["id"], self.spare_id)
        db.dock_ship_at_station(self.spare_id, self.station["id"])
        self.assertIsNone(db.get_player_with_ship("pk")["towing_ship_id"])

    def test_delete_station_destroys_the_ships_docked_inside(self):
        db.dock_ship_at_station(self.spare_id, self.station["id"])

        db.delete_station(self.station["id"])

        self.assertIsNone(db.get_ship(self.spare_id))          # gone with it
        self.assertIsNone(db.get_station(self.station["id"]))
        conn = db.get_connection()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM ships WHERE player_id = ?", (self.player["id"],)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 1)                          # just the active Kestrel

    def test_delete_station_spares_undocked_ships(self):
        db.delete_station(self.station["id"])                   # spare never docked
        self.assertIsNotNone(db.get_ship(self.spare_id))
        self.assertEqual(
            [s["id"] for s in db.get_parked_ships_in_sector(20)], [self.spare_id]
        )


class StationTreasuryDbTests(unittest.TestCase):
    """transfer_station_credits against a real db: both directions in one
    function, and the treasury burning with the station row."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "treasury.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO sectors (id) VALUES (?)", [(i,) for i in range(1, 25)]
        )
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")
        self.station = db.create_station(self.player["id"], "Alice", 20)

    def test_new_stations_start_with_an_empty_treasury(self):
        self.assertEqual(db.get_station(self.station["id"])["credits"], 0)

    def test_deposit_and_withdraw_round_trip(self):
        start = db.get_player_with_ship("pk")["credits"]

        db.transfer_station_credits(self.player["id"], self.station["id"], 600)
        self.assertEqual(db.get_player_with_ship("pk")["credits"], start - 600)
        self.assertEqual(db.get_station(self.station["id"])["credits"], 600)

        db.transfer_station_credits(self.player["id"], self.station["id"], -250)
        self.assertEqual(db.get_player_with_ship("pk")["credits"], start - 350)
        self.assertEqual(db.get_station(self.station["id"])["credits"], 350)

    def test_treasury_dies_with_the_station(self):
        db.transfer_station_credits(self.player["id"], self.station["id"], 999)
        db.delete_station(self.station["id"])
        self.assertIsNone(db.get_station(self.station["id"]))
        # The credits are simply gone -- the player never gets them back.
        self.assertEqual(
            db.get_player_with_ship("pk")["credits"],
            db.STARTING_CREDITS - 999,
        )


class PortRestockTests(unittest.TestCase):
    """apply_port_restock against a real db: proportional drift of stock
    toward each commodity's starting level (selling up to capacity, buying
    down to empty), the NULL-init and same-day no-op paths, and the lazy
    clock advance."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "restock.db")
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.commit()
        conn.close()

    def _make_port(self, dirs, qtys, maxes, last_restock, port_class="BSS"):
        """dirs/qtys/maxes are 3-tuples in (fuel_ore, organics, equipment) order."""
        conn = db.get_connection()
        cur = conn.execute(
            "INSERT INTO ports (sector_id, port_class, "
            "fuel_ore_dir, organics_dir, equipment_dir, "
            "fuel_ore_qty, organics_qty, equipment_qty, "
            "fuel_ore_max, organics_max, equipment_max, last_restock) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (port_class, *dirs, *qtys, *maxes, last_restock),
        )
        conn.commit()
        port_id = cur.lastrowid
        conn.close()
        return port_id

    def test_restock_qty_curve(self):
        # Proportional: each day closes 1/5 of the gap to target.
        self.assertEqual(db._restock_qty(0, 1000, 1), 200)
        self.assertEqual(db._restock_qty(0, 1000, 2), 360)
        self.assertEqual(db._restock_qty(0, 1000, 5), 672)   # not 100% at 5 days
        self.assertEqual(db._restock_qty(500, 1000, 1), 600)
        self.assertEqual(db._restock_qty(1000, 0, 1), 800)   # buying drains
        self.assertEqual(db._restock_qty(1000, 0, 5), 328)
        self.assertEqual(db._restock_qty(0, 1000, 0), 0)     # no days -> unchanged
        self.assertEqual(db._restock_qty(1000, 1000, 3), 1000)  # already at target

    def test_selling_refills_up_buying_drains_down(self):
        # BSS: fuel buys (drains 900->720), organics/equipment sell (refill).
        port_id = self._make_port(
            dirs=("B", "S", "S"),
            qtys=(900, 0, 500),
            maxes=(1000, 1000, 1000),
            last_restock="2026-06-10T12:00:00+00:00",
        )
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)  # 1 boundary
        port = db.apply_port_restock(port_id, now=now)
        self.assertEqual(port["fuel_ore_qty"], 720)     # 900 -> drains 20% of gap-to-0
        self.assertEqual(port["organics_qty"], 200)     # 0 -> +20% of 1000
        self.assertEqual(port["equipment_qty"], 600)    # 500 -> +20% of 500 gap
        self.assertEqual(port["last_restock"], now.isoformat())

    def test_five_days_is_not_full(self):
        port_id = self._make_port(
            dirs=("S", "B", "B"),
            qtys=(0, 1000, 1000),
            maxes=(1000, 1000, 1000),
            last_restock="2026-06-10T12:00:00+00:00",
        )
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # 5 boundaries
        port = db.apply_port_restock(port_id, now=now)
        self.assertEqual(port["fuel_ore_qty"], 672)     # selling, ~67%
        self.assertEqual(port["organics_qty"], 328)     # buying, ~33% left

    def test_null_last_restock_just_starts_the_clock(self):
        port_id = self._make_port(
            dirs=("S", "S", "S"), qtys=(0, 0, 0), maxes=(1000, 1000, 1000),
            last_restock=None,
        )
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        port = db.apply_port_restock(port_id, now=now)
        self.assertEqual((port["fuel_ore_qty"], port["organics_qty"]), (0, 0))  # no drift
        self.assertEqual(port["last_restock"], now.isoformat())

    def test_same_day_is_a_noop(self):
        port_id = self._make_port(
            dirs=("S", "S", "S"), qtys=(100, 100, 100), maxes=(1000, 1000, 1000),
            last_restock="2026-06-15T12:00:00+00:00",  # 08:00 ET, after the 3am boundary
        )
        now = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)  # same reset-day
        port = db.apply_port_restock(port_id, now=now)
        self.assertEqual(port["fuel_ore_qty"], 100)     # unchanged, no boundary crossed

    def test_clock_advances_even_when_at_target(self):
        # Selling port already full: nothing to add, but the timestamp still
        # moves forward so days don't pile up.
        port_id = self._make_port(
            dirs=("S", "S", "S"), qtys=(1000, 1000, 1000), maxes=(1000, 1000, 1000),
            last_restock="2026-06-10T12:00:00+00:00",
        )
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        port = db.apply_port_restock(port_id, now=now)
        self.assertEqual(port["last_restock"], now.isoformat())
        self.assertEqual(port["fuel_ore_qty"], 1000)

    def test_stardock_is_left_alone(self):
        port_id = self._make_port(
            dirs=(None, None, None), qtys=(0, 0, 0), maxes=(0, 0, 0),
            last_restock="2026-06-10T12:00:00+00:00", port_class="STARDOCK",
        )
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        port = db.apply_port_restock(port_id, now=now)  # must not raise
        self.assertEqual((port["fuel_ore_qty"], port["organics_qty"]), (0, 0))

    def test_lazy_application_is_idempotent_within_a_boundary(self):
        port_id = self._make_port(
            dirs=("S", "S", "S"), qtys=(0, 0, 0), maxes=(1000, 1000, 1000),
            last_restock="2026-06-10T12:00:00+00:00",
        )
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)  # 2 boundaries
        first = db.apply_port_restock(port_id, now=now)
        second = db.apply_port_restock(port_id, now=now)  # same now -> no further drift
        self.assertEqual(first["fuel_ore_qty"], 360)
        self.assertEqual(second["fuel_ore_qty"], 360)



if __name__ == "__main__":
    unittest.main()
