import asyncio
import os
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main():
    """Run both the web server and Discord bot in the SAME event loop.
    This fixes the 'Future attached to a different loop' crash that broke
    every single bot command when they tried to use the DB pool."""

    from bot import bot  # bot.py lives at root, bot variable = commands.Bot instance
    import db

    # Create the DB pool once, in THIS loop, before anything else touches it
    await db.get_pool()

    port = int(os.environ.get("PORT", 8000))

    config = uvicorn.Config(
        "server:app",
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        backlog=2048,
        timeout_keep_alive=30,
        loop="none",   # CRITICAL: don't let uvicorn create its own loop
    )
    server = uvicorn.Server(config)

    # Run bot and web server concurrently in the same loop
    await asyncio.gather(
        server.serve(),
        bot.start(os.environ["DISCORD_TOKEN"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
