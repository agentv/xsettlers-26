# XSettlers

*A Multiplayer Space Strategy Game*

Mankind has at last inherited the stars.

With the invention of reliable faster-than-light travel, humanity has broken free of its home system and begun the long work of spreading across the galaxy. Scout ships roam the spaceways searching for habitable worlds. Colony ships follow, carrying the people, equipment, and resources needed to claim and shape whatever they find.

XSettlers puts each player in command of one of these fledgling societies. You control a fleet of ships that can move through a vast grid of space sectors, each with its own capacity to produce energy, food, and goods. Ships can be converted into permanent colonies — sacrificing mobility for the ability to produce at greater scale. Your colonies in turn fuel the construction of new ships, and the cycle of expansion continues.

Every sector you discover, every colony you establish, and every resource you accumulate brings you closer to dominance. But you are not alone. Rival civilizations are out there, expanding from their own home worlds, scouting the same promising sectors, racing toward the same rich territories.

The game is played entirely through Slack, turn by turn, with each player issuing commands to their ships and colonies at their own pace. When all players are ready, the turn resolves — fleets arrive, colonies produce, and the universe ticks forward.

The player who builds the most powerful civilization wins.

## Documentation

> This repository is the source of truth for XSettlers going forward. The docs below originated as Slack canvases and were migrated here on 2026-07-18; the original canvases now serve only as historical/archival reference — this repo, not Slack, is authoritative for design and code from here on.

- [Product Requirements](docs/product_requirements.md)
- [Data Model & Storage Design](docs/data_model_and_storage_design.md)
- [MCP Server Layer Design](docs/mcp_server_layer_design.md)
- [Game Instance: MVP](docs/game_instance_mvp.md)
- [UI & Rendering Design](docs/ui_and_rendering_design.md)
- [Task Journal](docs/task_journal.md)
- [Known TODOs](docs/TODO.md)

## Stack

Python · SpatiaLite · MCP SDK · Slack. See `docs/mcp_server_layer_design.md` for hosting and deployment details (Fly.io).

## License

[MIT](LICENSE)
