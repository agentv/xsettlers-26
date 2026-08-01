# XSettlers Task Journal

> **Superseded — retained for early project history only (2026-07-31).**
> This journal stopped being maintained around 2026-07-15 and its contents are
> now stale in ways that mislead: the one "Open / In Progress" row below claims
> `fly.toml` still needs volume configuration, but the volume was created and
> the app deployed on 2026-07-29, and the `mcp/tools/` path it mentions was
> renamed to `xsettlers_mcp/tools/` on 2026-07-22. Nothing here is authoritative.
>
> Open work now lives in [Known TODOs](TODO.md); completed work and the
> reasoning behind settled decisions live in [Dev History](dev_history.md).

New entries added at top. Completed tasks include derived information captured at time of completion.

**Hosting: Fly.io.** That's the only deployment target for XSettlers — see `docs/mcp_server_layer_design.md` and `fly.toml`.

---

## Open / In Progress

| # | Task | Status | Notes |
|---|---|---|---|
| 4 | Finalize `fly.toml` | 🟡 Pending | Needs volume config for SpatiaLite. |

---

## Completed

| # | Task | Completed | Derived Information |
|---|---|---|---|
| 2 | Sync project scaffold to GitHub | 2026-07-14 | Repo created, local replica confirmed on dev machine. |
| 1 | Create `xsettlers/` directory scaffold | 2026-07-14 | Bash script generated and executed. Full tree matches agreed structure: `config/`, `db/`, `models/` (stubbed), `engine/`, `mcp/tools/`, `tests/`. |

*A handful of tasks from an abandoned, unrelated Google Drive MCP Server integration (IAM/Secrets Manager/GCP quota work) were dropped on 2026-07-15 and are omitted here — they never pertained to XSettlers hosting.*
