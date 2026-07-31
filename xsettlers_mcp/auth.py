import hmac
from config.loader import load_config

def authenticate(player_token: str, scenario_file: str = None) -> dict:
    """
    Resolve a player's opaque token against the service-wide player directory
    (config/game_config.yaml's players list). Client-agnostic by design --
    any caller (Slack, another LLM agent, curl) that knows a valid player's
    token authenticates the same way; nothing here is Slack-specific.

    Two separate questions, deliberately split:

    - *Who are you?* Called with no scenario_file, this resolves the token to
      a directory identity and stops there. The directory says who exists on
      this service; it says nothing about which games they play.
    - *May you play THIS game?* Given scenario_file (repo-root-relative, e.g.
      "config/game0.yaml"), the identity must additionally be a participant in
      that scenario. A token is an identity, not a blanket invitation to every
      game in the library.

    player_token is each player's actual credential (not a public-looking
    ID), so comparison is constant-time (hmac.compare_digest) rather than
    plain == -- avoids leaking match-length information via timing.

    Deliberately does NOT depend on the DB or on a game having been
    bootstrapped yet -- the directory is config-driven and exists before any
    scenario is chosen. This is what makes it safe to call from
    game_select.select_scenario() before bootstrap has run.
    """
    cfg = load_config(scenario_override=scenario_file)
    for p in cfg.players:
        if hmac.compare_digest(p.player_token, player_token):
            if scenario_file is not None and not any(s.email == p.email for s in cfg.seats):
                return {"ok": False,
                        "error": f"{p.display_name} is not a participant in this scenario"}
            return {"ok": True, "player_token": player_token,
                    "email": p.email, "display_name": p.display_name}
    return {"ok": False, "error": "Unrecognized player — not on this service's player directory"}
