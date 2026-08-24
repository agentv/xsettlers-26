# XSettlers

*A Multiplayer Space Strategy Game*

Mankind has at last inherited the stars.

With the invention of reliable faster-than-light travel, humanity has broken free of its home system and begun the long work of spreading across the galaxy. Scout ships roam the spaceways searching for habitable worlds. Colony ships follow, carrying the people, equipment, and resources needed to claim and shape whatever they find.

XSettlers puts each player in command of one of these fledgling societies. You control a fleet of ships that move through a grid of space sectors, harvesting energy from them and manufacturing food and goods out of what you hold. Ships can be converted into permanent colonies — trading mobility for a fixed foothold to build around.

Every sector you discover and every resource you accumulate brings you closer to dominance. But you are not alone. Rival civilizations are out there, expanding from their own home worlds, scouting the same promising sectors, racing toward the same rich territories.

The game is played turn by turn, at each player's own pace. The turn resolves on a timer — or early, the moment every player has declared they're done — and fleets arrive, pods produce, scans report back, and the universe ticks forward.

**The player holding the most valuable stockpile when the last turn ends wins.** Not the largest stockpile: goods count double food, and energy scores nothing at all. See the [Player Guide](docs/player_guide.md).

## Playing it

XSettlers is an **MCP server**. It has no interface of its own — you play by pointing an MCP-speaking client at it and talking to the game through that client. Slack is the intended home, but nothing in the server is Slack-specific: any MCP client, `curl`, or another agent authenticates and plays identically.

It serves MCP's streamable HTTP transport (`POST /mcp`, `GET /health`) and is deployed on Fly.io. See `CLAUDE.md` for running it locally.

## Documentation

> This repository is the source of truth for XSettlers.

- [Player Guide](docs/player_guide.md) — start here if you just want to learn how to play
- [Product Requirements](docs/product_requirements.md) — the same rules, precisely stated
- [Data Model & Storage Design](docs/data_model_and_storage_design.md)
- [UI & Rendering Design](docs/ui_and_rendering_design.md)
- [Known TODOs](docs/TODO.md) — open work, and the design direction behind it
- [Dev History](docs/dev_history.md) — settled decisions, findings from play, recovery pointers

## Status

Playable end to end, solo or multiplayer, but pre-MVP. The significant gaps: combat is unimplemented (`defend`/`attack` are stubs), there is no way to build new ships or pods, sector richness varies but not by enough to change outcomes at a 20-turn horizon, and rival detection is not built. `docs/TODO.md` tracks each of these along with the design direction behind it.

**Security note:** `/mcp` has *no* perimeter authentication, and the player tokens in `config/game_config.yaml` are placeholders committed to this public repository. Anyone who knows the URL can play as anyone. That is a knowing tradeoff while the game holds nothing of value — read the SECURITY POSTURE comment in `xsettlers_mcp/server.py` before deploying anything you care about.

## Stack

Python · SpatiaLite · MCP SDK · Starlette/uvicorn · Fly.io

## License

[MIT](LICENSE)
