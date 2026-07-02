# TradeWars 2002 Clone For MeshCore

A multiplayer space trading-and-combat game inspired by the classic
**TradeWars 2002** BBS door game, played entirely over a **MeshCore /
Meshtastic radio mesh** — no internet required. Players pilot a ship through
a 1000-sector galaxy by sending short text messages to the game's radio
node; the bot replies over the same mesh. It's built to run on a Raspberry
Pi or any Linux machine attached to a MeshCore radio, so a whole game can
live on a single low-power node out in the field.

Think of it as a persistent, off-grid BBS door: buy low and sell high
between star ports, upgrade your ship, lay mine fields, hunt other pilots,
and try not to get your hull blown out from under you.

---

## How you play

You interact with the game by sending it **direct messages** over the mesh
(the same way you'd DM any other node). Each message is a command; the bot
runs it and replies. A short menu is always a `menu` away.

Because a mesh link is slow and only one conversation can sensibly happen at
a time, the game uses a **single "at the helm" lock**: one player holds the
controls, and others are politely asked to wait their turn. If the active
player goes quiet (radio out of range, app closed, walked away), they're
warned and then automatically signed off so the next pilot can play.

All replies are automatically split into radio-sized chunks (≤130
characters) and sent one at a time, waiting for each to be acknowledged
before the next goes out.

---

## Features & mechanics

### The galaxy

* **1000 sectors** connected by two-way **warps**, generated as a
  fully-connected network (every sector is reachable from any other).
* About **20% of sectors have a port**. Most ports trade commodities; one
  special **Stardock** sits at the home sector (Sector 1).
* **Complementary port pairs**: at least 5% of ports are placed as adjacent
  pairs where one buys exactly what the other sells, giving traders a
  reliable shuttle run for profit.
* **Safe zone (Sectors 1–10)**: these sectors are **fully interconnected**
  (every one warps directly to every other, so the Stardock can't be walled
  off), and they're protected — **no mines and no combat** are allowed
  inside. New pilots get a safe runway to find their feet.

### Turns & time

* You start with **1000 credits**, a free **Falcon**, and **100 turns**.
* **Each sector you move into costs one turn.** Trading, docking, scanning,
  and fighting are free.
* Turns **refill to 100 once a day at 3:00 AM Eastern** (daylight-saving
  aware). The refill is checked when you sign in.
* Being relocated by *someone else's* attack (or a mine) never costs you a
  turn.

### Trading & ports

Commodities are **fuel ore, organics, and equipment**. Each commodity port
is one of eight classes describing, per commodity, whether the port **buys
from you** or **sells to you**. Selling prices are deliberately set below
buying prices across the galaxy, so shuttling goods between complementary
ports yields a margin instead of relying on luck.

Dock with `p` (port). Trading is a **guided sell-then-buy flow**: the port
walks you through each commodity, your free cargo holds are recalculated
after every transaction, and you can `cancel` at any point or skip a single
item.

To free holds without a port, **`jettison`** dumps cargo overboard — a
pure loss, no credits. `jettison all` clears every hold; `jettison
<commodity>` dumps one (fuel/organics/equipment); `jettison <commodity> n`
dumps a set amount. A bare `jettison` just lists what's aboard, so you
can't space a hold by mistyping. It's a free action, allowed anywhere, and
never touches a Station Core kit you're carrying (deploy or sell that to
offload it).

**Ports restock over time.** Trading isn't free of consequences: buying
drains a selling port's stock, and selling fills a buying port's storage.
Each port drifts back toward its starting level at the daily **3am Eastern**
boundary, but only **proportionally** — each day closes a fifth of the
remaining gap, so recovery is front-loaded and asymptotic (a fully-drained
port is only ~67% back after five days and never snaps cleanly to 100%).
There's no background job; a port catches up the next time someone trades
there. Practically, you can't camp a single lucrative port — work it hard
and you'll have to let it breathe for a few days.

### Port-to-port shuttle (P2P)

Once you've found two adjacent ports that **share a tradeable commodity**
— one sells something the other buys — `p2p <sector>` automates the shuttle
run between them so you don't have to hand-trade every hop. This includes
**exact complements** (each buys what the other sells, for a two-way loop)
and **partial overlaps** — e.g. a port sitting next to one that *buys
everything* (`BBB`), where you repeatedly sell a commodity there and rebuy
it at home in a one-way loop.

* **Setup.** Stand on one of the two ports and run `p2p <adjacent sector>`.
  The game works out which commodities can flow between the ports and, if
  there's more than one option, asks **once** which to run (e.g. *"trade
  fuel+organics or fuel+equipment?"*); if only one shuttle is possible it
  just asks you to **confirm (`y`/`n`)**. Either reply also starts the run.
* **Starting cargo.** Your **holds must be full** of the commodity you'll
  sell on the first hop — P2P never buys your initial load for you. If
  they're not, it tells you to `jettison` any other cargo and dock (`p`) to
  buy the right commodity first. (For the rare port that only *buys* all
  three commodities, you instead start with **empty holds** and buy at the
  far port first.)
* **Each leg.** The game moves you one warp (costing **one turn**, exactly
  like a normal move), auto-docks, **sells your full load and refills your
  holds** with the return commodity — always at the **listed price**, buying
  the **maximum** you can carry. It reports what entered, sold, and bought,
  with both the **leg's profit and a running total**, then asks **"Continue?
  y/n"**. `y` runs the next leg back the other way; `n`/`stop`/`cancel` ends
  the run.
* **It's still real space.** A P2P move is a normal move: if the sector you
  warp into holds **someone else's mines** or a **non-owner space station**,
  you're attacked as usual and the shuttle is **abandoned** on the spot.
* **It ends cleanly** when you stop, run out of turns, or a port can no
  longer sustain a full load (out of stock to sell you, or no room to buy
  from you) — in which case the trade is skipped and you're left where you
  were. Because ports only restock gradually (see above), a hard-run shuttle
  will drain its pair and pause itself; come back in a few days and it's
  viable again.

### Ships & the Stardock

Nine buyable hulls, plus the escape pod you can't buy. Prices climb with
capability, but the heavy hulls are **sidegrades, not strict upgrades** —
each specializes in something (fighters, shields, mines, or cargo), so the
"best" ship depends on how you play. Figures below are each hull's **maximum
upgradeable capacity**.

| Ship | Class | Price | Holds | Fighters | Shields | Mines | Probes | Role |
|------|-------|------:|------:|---------:|--------:|------:|-------:|------|
| **Falcon** | Frigate | free | 75 | 50 | 200 | — | 10 | The free starter; balanced all-rounder. |
| **Kestrel** | Corvette | 8,000 | 40 | 120 | 150 | — | 25 | Cheap, nimble scout — best probe range in the fleet. |
| **Mule** | Fleet Tender | 40,000 | 120 | 5 | 250 | — | 10 | Entry-level hauler; lots of cargo, almost no guns. |
| **Barracuda** | Destroyer | 120,000 | 60 | 900 | 1,200 | 20 | 15 | Aggressive mid-tier warship with a small mine bay. |
| **Nautilus** | Minelayer | 180,000 | 80 | 400 | 1,000 | 150 | 15 | Mine specialist — the biggest mine bay anywhere; starts with 10 mines. |
| **SS Endeavour** | Merchant Freighter | 200,000 | 200 | 10 | 400 | — | 10 | Serious cargo hauler; weak guns. |
| **Hornet** | Fleet Carrier | 350,000 | 50 | 3,000 | 800 | — | 20 | Fighter glass-cannon: out-guns everything, thin shields. |
| **Vanguard** | Battlecruiser | 450,000 | 70 | 1,500 | 5,000 | 30 | 20 | Shield tank: the toughest hull to crack. |
| **Bismark** | Capital Ship | 500,000 | 125 | 2,000 | 3,500 | 50 | 20 | The flagship all-rounder — strong everywhere. |
| *Escape Pod* | Escape Pod | n/a | — | — | — | — | — | Where you end up when your ship is destroyed. Not for sale. |

Several hulls — the **Barracuda, Nautilus, Vanguard, and Bismark** — carry a
**mine bay**; the others can't lay mines at all. Ships start with base stats
well below these maximums and are upgraded toward them at the Stardock (the
Nautilus is the one hull that comes with mines already aboard).

Ship stats and prices (and most other balance numbers) live in
`SHIP_CATALOG` in `db.py`, so they're easy to tune for your own game.

At the **Stardock** (Sector 1) you can:

* **Refit** your current ship — buy extra cargo holds, fighters, and shields
  up to that hull's maximums.
* Visit the **shipyard** to buy a different hull or sell your current one.
* Buy **probes** (recon drones) for scouting.
* Buy a **Space Station Core kit** (see *Space stations* below) to build your
  own station out in the galaxy.

### Owning more than one ship

You aren't limited to a single hull. When you **buy a ship at the shipyard**
you're asked whether to **sell your current ship as a trade-in** (50% of its
catalog price off the new hull) or **keep it** — keeping it means paying the
new hull's **full price**, and your old ship is **parked, unmanned, right
there in the sector** with everything still aboard (cargo, fighters, shields,
mines, probes, even a carried Station Core kit).

* Parked ships appear on the sector info screen on an **`Unmanned:`** line,
  named by **owner and type with a ship id** — e.g. `Joe's Kestrel #12
  (5 ftr)`. The id is how you target one (`board #12`, `tow #12`, `a #12`).
* **`board`** (alias `swap`) takes the helm of one of your own parked ships
  in the sector; your current hull is parked in its place, exactly as it is.
  Boarding is free (no turn cost). A pilot stuck in an **escape pod** can
  board a ship they own — the pod is scuttled — which makes a parked spare a
  genuine insurance policy against losing your flagship.
* **`tow`** drags one of your own unmanned hulls behind you. It's slow:
  **every sector moved while towing costs 5 turns** instead of 1 (you're
  warned when you engage, and a move is refused outright if you have fewer
  than 5 turns left). `tow` again releases the line. Only **your own** ships
  can ever be towed, an escape pod can't tow, and P2P shuttles won't run
  while towing.
* **Selling a parked ship**: tow (or leave) an unmanned hull in Sector 1 and
  the shipyard menu lists it — `s12) Sell your parked Kestrel #12` — for 50%
  of its catalog price. It's sold **as-is**: anything still aboard is lost
  with it.
* **Unmanned ships can be attacked** (outside the safe zone — hulls parked in
  Sectors 1–10, including at the Stardock, are protected). They defend with
  whatever fighters and shields were left aboard, using the normal combat
  math. A destroyed unmanned ship is simply gone — no pod, no kill-log entry
  — but the owner gets a while-you-were-away notice. For protection out in
  the wild, spares can be **docked inside your own space station** (see
  *Space stations* below).

### Navigation

* Type a **sector number** to move. Adjacent sectors are a single warp.
* For a distant sector, the game **plots the shortest route** through the
  warp network and walks you hop-by-hop, asking you to confirm each jump.
* You can **dock mid-route** (`p`) — trade or refit at a port you pass
  through — and then continue the same plotted course without re-plotting.

### Combat

* Attack another pilot in your sector with `a <name>`, an unmanned parked
  ship with `a #<id>`, or a station with `a station`. A bare `a` walks you
  through **every target present one at a time** — pilots first, then
  unmanned ships, then an enemy station — with a yes/no prompt for each.
  Combat is **not allowed in the safe zone (Sectors 1–10)**.
* You're asked **how many fighters to commit** — send a number, `all`, or
  `cancel`. Fighters you hold back are never at risk.
* Resolution: your fighters clash with the defender's first (you spend
  ~0.75 of a fighter per enemy fighter destroyed); any survivors then strip
  **shields** (about 10 shields per leftover fighter). A ship is destroyed
  only when **both** its fighters and shields are gone.
* The **sector info screen shows other ships' fighter counts but never
  their shields** — you can size up a target's firepower, but their shield
  strength stays hidden until you actually trade shots.
* When you destroy a ship, the pilot ejects into an escape pod and drifts
  off — **you are not told where they went.** They have to be hunted down.

### Mines

* Only ships with a **mine bay** (the Barracuda, Nautilus, Vanguard, and
  Bismark) can carry and lay mines. Lay them with `lay <n>`.
* **No mines may be laid in the safe zone (Sectors 1–10).**
* When a pilot enters a sector holding mines that aren't their own, **all of
  them detonate at once**. Each mine does 1–10 damage, which cascades
  through shields, then fighters, then the hull.
* Your own mines never detonate on you.

### Space stations

Stations are formidable, player-built structures you anchor in a sector to
hold territory, stockpile materials, and fight on your behalf.

* **Building one.** Buy a **Space Station Core kit** at the Stardock for
  **5,000,000 cr**. The kit fills **150 cargo holds**, so you need a large,
  near-empty hull to haul it (a maxed-out **SS Endeavour** is the practical
  carrier). Fly it to any sector **outside the safe zone (Sectors 1–10)** and
  `deploy`. Only **one station per sector** is allowed, and swapping hulls at
  a shipyard while carrying an undeployed kit loses it (it's cargo).
* **It's yours alone.** Only the owner can `dock` at a station to manage it.
  It appears on the sector info screen as **`Space Station - <owner>`** with
  its fighter count (shields stay hidden, just like ships).
* **Docking bays for your spare ships.** A station shelters your unmanned
  hulls: **2 bays per level** (Lvl 1 → 2 ships … Lvl 4 → 8). From the station
  menu, **Dock a spare ship** tucks any of your parked ships in the sector
  inside; while docked they **can't be attacked, towed, or boarded** and drop
  off the sector's `Unmanned:` line — instead they're listed on a `Docked:`
  line, visible to everyone but out of reach. **Undock** returns a ship to
  the open (attackable again) so you can board, tow, or sell it. The catch:
  if the station is **destroyed, every ship docked inside is destroyed with
  it** — the attacker is told how many went down, and you get a notice per
  lost hull.
* **Arming it.** A fresh station is **Level 1** with a ceiling of **1000
  shields and 1000 fighters**, both starting at **0**. Dock to:
  * **Deposit** fuel, organics, and equipment from your cargo into the
    station's shared stockpile.
  * **Transfer fighters** from your ship into the station.
  * **Power up shields**, which charge to the level cap and **burn 0.1 fuel
    per shield per day** (100 fuel/day at Level 1) from the stockpile. If a
    day's burn can't be covered, shields drop to 0 and switch off until you
    refuel and re-enable. Shields recharge to full each day while powered.
* **Upgrading (Levels 2–4).** Each upgrade costs **credits + materials from
  the stockpile + real time**, and only one runs at a time:

  | To level | Credits | Fuel | Organics | Equipment | Days | New caps |
  |----------|---------|------|----------|-----------|------|----------|
  | 2 | 10,000,000 | 2500 | 2000 | 1000 | 5  | 2500 / 2500 |
  | 3 | 12,500,000 | 3500 | 2500 | 1750 | 8  | 5000 / 5000 |
  | 4 | 15,000,000 | 5000 | 3500 | 2000 | 12 | 10000 / 10000 |

  "Days" are counted by the same 3 AM Eastern daily boundary as turn resets.
* **Posture.** Set a station **defensive** (default) or **offensive**, and
  for offensive choose what **percent of its fighters engage**. A defensive
  station does nothing until attacked. An **offensive** station opens fire on
  any **non-owner** who enters its sector, committing that percentage of its
  fighters with the normal combat math — it can damage or destroy them, and a
  pilot it kills is logged publicly as a kill by `Space Station - <owner>`. A
  station never fires on its own owner.
* **Attacking a station.** Anyone can attack one with **`a station`** (kept
  separate from `a <name>` so a sector can hold both a port and a station).
  Same fighter-vs-shield math as a ship. When a station's **shields *and*
  fighters both reach 0 it's destroyed and removed** — the owner gets a
  private notice the next time they sign in. (Destroying a station isn't
  entered in the public kill log; it isn't a ship or pod.)
* **Upkeep is lazy.** There's no background clock: a station's daily fuel
  burn and upgrade progress are brought up to date whenever the owner signs
  in or anyone interacts with the station.

### Death, escape pods & resets

* A destroyed **ship** ejects its pilot into an **Escape Pod**, which drifts
  4–6 warps away. Cargo and the hull are lost; **credits are kept**.
* Finishing off someone who's **already in a pod** — or running a pod into a
  mine field — is a **total reset**: back to a fresh Falcon at the Stardock
  with credits reset to 20,000. A pod has no defenses, so it's fragile.
* A pilot flying a pod can trade it in for the free Falcon at any Stardock —
  the intended way to climb back out.

### The public kill log

When you sign in, you're shown a **public log of every kill since you last
played** — both player-vs-player combat kills and kills by mines. Brand-new
players only see kills from when they first joined onward. (The most recent
entries are shown, with a count of any older ones, to keep it radio-sized.)

### While-you-were-away notices

Separately from the public log, if you personally were attacked or destroyed
while signed off, you get a private briefing of what happened to you the next
time you sign in.

---

## Command reference

Send these as direct messages to the game node.

| Command | Aliases | What it does |
|---------|---------|--------------|
| `menu`  | `help`, `?` | List commands (`help combat` for the combat sub-menu). |
| `info`  | `i` | Show your current sector: port, warps, and other ships present. |
| `status`| `st` | Show your credits, sector, ship, and turns remaining. |
| *(number)* | | Move to that sector (plots a route if it isn't adjacent). |
| `p`     | `port` | Dock to trade, or to refit / visit the shipyard / buy a Station Core kit at a Stardock. |
| `jettison <what>` | `jet` | Dump cargo from your holds to free up space: `jettison all`, or `jettison <commodity> [n]`. |
| `p2p <sector>` | | Auto-shuttle trade with an adjacent complementary port; pick a commodity, then `y`/`n` to keep running. |
| `a [target]` | `attack` | Attack a ship here (then commit fighters): `a <name>` for a pilot, `a #<id>` for an unmanned ship, `a station` for a station, or bare `a` to cycle through every target. |
| `tow [#<id>]` | | Tow one of your own unmanned ships (5 turns per sector moved); `tow` again releases. |
| `board [#<id>]` | `swap` | Board one of your own unmanned ships here; your current hull is parked in its place (a pod is scuttled). |
| `deploy` | | Deploy a carried Space Station Core kit in your current sector (outside Sectors 1–10). |
| `station` | `dock` | Dock at your own space station here to deposit materials, transfer fighters, power shields, set posture, and upgrade. |
| `lay <n>` | `mine <n>` | Lay `n` mines in your current sector (needs a ship with a mine bay). |
| `probe <n>` | | Send a recon probe to scout a route to sector `n`. |
| `combat`| | Show the combat & recon sub-menu. |
| `quit`  | `logout` | Sign off so another player can take a turn. |

---

## Setup & hosting (Raspberry Pi / Linux)

### What you need

* A **Raspberry Pi or Linux machine** (anything that runs Python 3.9+).
* A **MeshCore radio** flashed with the **USB Companion** firmware,
  connected to the host over USB.
* **Python 3** with `venv`, and the **`meshcore`** Python package.

### 1. Serial port & permissions

The bot connects to the radio over a USB serial device. By default it
expects **`/dev/ttyACM0` at 115200 baud** (see the `MeshCore.create_serial`
call near the bottom of `main.py`). Plug in the radio and confirm the device:

```bash
ls /dev/ttyACM* /dev/ttyUSB*    # find your radio's device node
```

If it shows up as something other than `/dev/ttyACM0` (e.g. `/dev/ttyUSB0`),
edit that line in `main.py` to match.

On most Linux distros / Raspberry Pi OS you also need permission to read the
serial port. Add your user to the `dialout` group once, then log out and
back in (or reboot):

```bash
sudo usermod -aG dialout "$USER"
```

### 2. Time zone data

Turn resets happen at 3:00 AM **Eastern**, which relies on the system time
zone database. Minimal images sometimes lack it:

```bash
sudo apt install tzdata     # Debian / Raspberry Pi OS
# or, inside the venv: pip install tzdata
```

### 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install meshcore
```

(Re-run `source venv/bin/activate` in any new shell before using the tools
below.)

### 4. Generate the galaxy (one time)

This builds the sectors, warps, and ports. Run it **before** the first
launch:

```bash
source venv/bin/activate
python galaxy.py
```

> ⚠️ Re-running `galaxy.py` **wipes and regenerates** the entire map. Players
> keep their credits, cargo, and ship, but the whole port/warp layout
> changes. It prompts for confirmation first.

### 5. Run the game

```bash
source venv/bin/activate
python main.py
```

You should see `Connected OK` and then `Bot is running...`. The game now
listens for player messages over the mesh. All player and game data is
stored in a local SQLite file (`meshcore_messages.db`) in the project
directory.

### Keeping it running

`main.py` is a long-lived process, so on a headless Pi you'll want it to
survive your SSH session. The simplest option is a terminal multiplexer:

```bash
sudo apt install tmux
tmux new -s tw2002
source venv/bin/activate && python main.py
# detach with Ctrl-b then d; reattach later with: tmux attach -t tw2002
```

For an always-on node, a small **systemd** service that runs
`venv/bin/python main.py` from the project directory (with
`Restart=on-failure`) works well and will start the game on boot.

---

## Testing

The project ships with unit and integration tests. The main suite stubs out
the database for fast, isolated tests; the `db` and `galaxy` suites run
against a throwaway SQLite file.

```bash
source venv/bin/activate
python main_test.py      # game logic (commands, combat, navigation, kill log, stations, ...)
python db_test.py        # database-level behavior (turns, kill log, presence, station upkeep)
python galaxy_test.py    # galaxy generation (port pairs, safe-zone connectivity)
```

The codebase is kept clean under [pyflakes](https://pypi.org/project/pyflakes/):

```bash
pip install pyflakes
python -m pyflakes *.py
```

---

## Project layout

The game is split into focused modules:

| Module | Responsibility |
|--------|----------------|
| `main.py` | Entry point, radio wiring, command handlers, sign-in flow. |
| `core.py` | Shared game state, the command registry, and the message context. |
| `db.py` | SQLite schema, all persistence, and the game-balance constants. |
| `galaxy.py` | One-time generation of sectors, warps, and ports. |
| `trading.py` | Port trading and the Stardock refit / shipyard / Station Core flows. |
| `station.py` | Deploying and managing player-built space stations. |
| `combat.py` | Pure combat & mine-damage math (no database access). |
| `pathfinding.py` | Warp-graph routing and escape-pod placement. |
| `display.py` | Rendering sector info and menus into text screens. |
| `messaging.py` | Splitting replies into radio chunks and the send/ack transport. |
| `session.py` | The single-player "at the helm" lock and inactivity timeout. |
| `*_test.py` | The test suites described above. |

---

*Inspired by TradeWars 2002. Built for MeshCore as a hobby project.*
