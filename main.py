import asyncio, os, threading
import uvicorn
from dotenv import load_dotenv
load_dotenv()

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
    from bot.bot import bot
    await bot.start(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    asyncio.run(run_bot())
