import asyncio, os, threading
import uvicorn
from dotenv import load_dotenv
load_dotenv()

def run_web():
    port    = int(os.environ.get("PORT", 8000))
    workers = int(os.environ.get("WEB_WORKERS", 4))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        workers=workers,           # multiple processes to use all CPU cores
        log_level="warning",       # reduce I/O overhead in prod
        access_log=False,          # disable per-request logs at scale
        loop="uvloop",             # fastest async event loop available
        http="httptools",          # fastest HTTP parser
        backlog=2048,              # accept queue depth
        timeout_keep_alive=30,     # release idle connections faster
    )

async def run_bot():
    from bot.bot import bot
    await bot.start(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    asyncio.run(run_bot())
