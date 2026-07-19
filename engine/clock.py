import asyncio, os
from engine.turn import end_of_turn
TICK_SECONDS = int(os.getenv("GAME_TICK_SECONDS", 300))

async def run_clock():
    print(f"Game clock started. Tick interval: {TICK_SECONDS}s")
    while True:
        await asyncio.sleep(TICK_SECONDS)
        print("Clock tick — running end of turn.")
        end_of_turn()
