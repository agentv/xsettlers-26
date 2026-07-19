# TODO: Write bootstrap tests covering:
# - Config loads correctly (all fields parsed, no KeyError)
# - bootstrap_game() idempotency: calling twice does not duplicate rows
# - All sectors from config created with correct coords and capacities
# - Sentinel sector (-1,-1,-1) exists with id=-1 after schema init
# - Each player gets a row in players table
# - Each player gets full starting_configuration instantiated (orgs + pods)
# - player_sectors rows stamped at confidence=100 for all start sectors
# - game_state row created with current_turn=0
# - Geometry column (location) populated for each real sector
