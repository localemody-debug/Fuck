import asyncio, os, sys, threading
import uvicorn
from dotenv import load_dotenv

load_dotenv()

# Ensure current directory is in path so all modules resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def run_web():
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
    # FIX: bot.py is at root level — import directly, no sub-package
    from bot import bot
    await bot.start(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    asyncio.run(run_bot())
