import asyncio, os, sys, threading
import uvicorn
from dotenv import load_dotenv

load_dotenv()

# Ensure current directory is in path so all modules resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_web():
    """
    Run the FastAPI/Starlette web server in its own thread with its own event loop.
    uvicorn.run() creates its own event loop internally, so this is safe.
    The web server must create its OWN db pool inside its lifespan/startup handler
    (see server.py) — it must NOT share bot.pool across threads/loops.
    """
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        backlog=2048,
        timeout_keep_alive=30,
    )


async def run_bot():
    # Import here so the module-level bot object is created after sys.path is set
    try:
        from bot.bot import bot
    except ModuleNotFoundError:
        import bot as bot_module
        bot = bot_module.bot

    await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    # Web server runs in a daemon thread (its own loop via uvicorn internals).
    # Bot runs in the main thread's event loop via asyncio.run().
    # The db pool for the bot is created inside setup_hook on the main loop.
    # The db pool for the web server is created inside server.py's lifespan on uvicorn's loop.
    # They never share a pool — this is the correct architecture.
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    asyncio.run(run_bot())
