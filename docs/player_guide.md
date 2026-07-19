# XSettlers — Player Guide

This is the rulebook. If you just want to know how to play — what your ships do,
what your pods do, and how you win — start here. For the underlying data model
and tool APIs, see [Product Requirements](product_requirements.md); this doc
never contradicts that one, it just explains the same rules in plain language.

---

## The Setting

Mankind has at last inherited the stars.

With the invention of reliable faster-than-light travel, humanity has broken
free of its home system and begun the long work of spreading across the
galaxy. Scout ships roam the spaceways searching for habitable worlds. Colony
ships follow, carrying the people, equipment, and resources needed to claim
and shape whatever they find.

You command one of these fledgling societies. You control a fleet of ships
that move through a grid of space sectors, each with its own capacity to
produce energy, food, and goods. Ships can be converted into permanent
colonies — sacrificing mobility for the ability to produce at greater scale.
Your colonies in turn fuel the construction of new ships, and the cycle of
expansion continues.

You are not alone. Rival civilizations are out there too, expanding from
their own home worlds, scouting the same promising sectors, racing toward the
same rich territories.

**The player who accumulates the most resources by the end of the game
wins.**

---

## The Basics

The game is played turn by turn, through Slack. Each turn, you issue
commands to your ships and colonies whenever you're ready — there's no
real-time pressure. Once every player has declared they're done for the
turn, the turn resolves all at once: ships that were traveling arrive, pods
produce, scans reveal what they've found, and the universe advances by one
tick.

Everything you own falls into one of two kinds of **organizations**:

- **Ships** — mobile. They can move across the map, scan the space around
  them, and carry pods that keep producing the whole time.
- **Colonies** — stationary. Once a ship converts into a colony, it can never
  move again, but it becomes a fixed, permanent base of operations.

Every organization — ship or colony — carries **pods**. Pods are what
actually do the productive work.

---

## Pods: What Actually Produces

A pod's job is defined by its **mission**, which you can reassign at any
time:

| Mission | What it does |
|---|---|
| `produce_energy` | Generates energy every turn. Energy powers everything else. |
| `produce_food` | Generates food every turn. |
| `produce_goods` | Generates goods every turn — this is also where raw-material extraction ("mining") lives for now: there's no separate mining step in the current game, produce_goods covers both. A distinct mining mechanic may be split out in a future version. |
| `scan` | Looks at a nearby sector and reports back what's there. |
| `idle` | Does nothing. The default until you assign it something else. |

Pods keep running their assigned mission every turn, whether their ship is
sitting still or in the middle of a journey — **with one exception**: a pod
on `scan` mission is suppressed while its ship is traveling. You can't scan
mid-flight.

There's no setup required to get started — every ship begins the game with
its pods already producing.

---

## Movement

When you send a ship somewhere, it doesn't teleport — it takes a number of
turns proportional to the distance. While it's en route:

- The ship is considered **in transit** and is not sitting in any normal
  sector — you'll see it flagged as such.
- It **cannot** be scanned from, and it can't be given a new mission until it
  arrives (or you cancel the move).
- Its produce pods (`produce_energy` / `produce_food` / `produce_goods`)
  **keep working** the entire time. A ship en route is not a ship on pause —
  it's still generating resources for you every turn.

You can preview a move before committing to it, to see how many turns it'll
take and when you'd arrive. Once committed, you can still cancel a move while
it's in progress — the ship rubber-bands back to where it started, with no
partial credit for the distance it covered.

---

## Colonizing

Any ship can be ordered to colonize the sector it's currently sitting in.
Once that completes, the ship becomes a permanent colony there: it loses the
ability to move, but the tradeoff is a foothold you can build around
indefinitely. Colonies keep producing through their pods exactly like ships
do, and once you commit a ship to colonizing, it's locked in for a short
transition window before the conversion finishes.

---

## Scanning & Discovery

Space is mostly unknown at the start of the game. You only know what your
ships have actually looked at. A pod on `scan` mission, aimed at a nearby
sector, reveals that sector's contents at the end of the turn — but only if
its ship is stationary and the target is close enough. Sectors you've
scanned stay visible for a while, but that knowledge fades over time if you
don't refresh it — so revisiting territory matters, not just discovering it
once.

---

## Winning

The game runs for a fixed number of turns. At the end of the last turn,
everything you've accumulated — every unit of energy, food, and goods sitting
in storage across every ship and colony you own — is added up into a single
score.

**Highest total wins.**

There's no partial credit for resources you produced but haven't banked, and
no bonus for territory alone — production is what counts. Explore fast
enough to find good sectors, but not so aggressively that your pods sit idle
while you're still deciding where to go.

---

## Quick Reference

| You want to... | Command |
|---|---|
| See how long a move would take, without committing | `preview_move` |
| Actually move a ship | `confirm_move` |
| Change your mind mid-flight | `cancel_move` |
| Give an organization a new mission (move/colonize/defend/attack/idle) | `set_mission` |
| Change what a pod is doing | `set_pod_mission` |
| Point a scan pod at a target | `set_pod_scan_target` |
| Check on one of your ships or colonies | `show_organization` |
| Look at nearby sectors | `show_sector_neighborhood` |
| See your overall standing | `show_game_status` |
| Signal you're done for this turn | `declare_end_turn` |
| Take that back | `rescind_end_turn` |

Full technical detail on each of these lives in
[Product Requirements](product_requirements.md).
