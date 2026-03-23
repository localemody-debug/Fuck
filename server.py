import os, random, secrets
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
import httpx
from dotenv import load_dotenv
import db

load_dotenv()

from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import time

app = FastAPI(
    title="SabPot",
    docs_url=None,    # disable swagger in prod
    redoc_url=None,
)
app.add_middleware(GZipMiddleware, minimum_size=500)  # compress responses > 500 bytes

# Serve pet images from /static/pets/
_pets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pets")
os.makedirs(_pets_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")
templates = Jinja2Templates(directory="templates")

# Simple in-memory rate limiter per IP
import collections
_rate_store: dict = collections.defaultdict(list)
RATE_LIMIT   = 120   # requests
RATE_WINDOW  = 60    # seconds

_rate_last_cleanup = time.time()

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            global _rate_last_cleanup
            ip  = request.headers.get("x-forwarded-for","").split(",")[0].strip() or (request.client.host if request.client else "unknown")
            now = time.time()
            hits = _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
            if len(hits) >= RATE_LIMIT:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            _rate_store[ip].append(now)
            # Cleanup stale IPs every 5 minutes to prevent unbounded memory growth
            if now - _rate_last_cleanup > 300:
                _rate_last_cleanup = now
                stale = [k for k,v in list(_rate_store.items()) if not v or now - v[-1] > RATE_WINDOW]
                for k in stale:
                    del _rate_store[k]
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

SECRET_KEY    = os.environ["SECRET_KEY"]
CLIENT_ID     = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
REDIRECT_URI  = os.environ["REDIRECT_URI"]
DISCORD_API   = "https://discord.com/api/v10"
serializer    = URLSafeTimedSerializer(SECRET_KEY)

def set_session(response, data):
    token = serializer.dumps(data)
    response.set_cookie(
        "session", token,
        httponly=True,
        samesite="lax",
        max_age=60*60*24*7,
        path="/",
    )

def get_session(request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        return serializer.loads(token, max_age=60*60*24*7)
    except:
        return None

async def require_user(request):
    s = get_session(request)
    if not s:
        raise HTTPException(401, "Not logged in")
    # Check ban/timeout on every request
    pool = await db.get_pool()
    user = await pool.fetchrow(
        "SELECT is_banned, timeout_until FROM users WHERE id=$1", s["user_id"]
    )
    if user:
        if user["is_banned"]:
            raise HTTPException(403, "Your account has been banned")
        if user["timeout_until"] and user["timeout_until"] > __import__('datetime').datetime.now(__import__('datetime').timezone.utc):
            raise HTTPException(403, f"You are timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    return s

ADMIN_USER_CODE = "2963"
ADMIN_DISCORD_ID = 1482825709331157134  # .mody51777's Discord client ID used as reference

async def require_admin(request):
    """Only the account with login_code=2963 or username=.mody51777 can use admin endpoints."""
    s = await require_user(request)
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT login_code, username FROM users WHERE id=$1", s["user_id"])
    if not row:
        raise HTTPException(403, "Admin only")
    is_admin = (row["login_code"] == ADMIN_USER_CODE or 
                row["username"] == ".mody51777" or
                s.get("username") == ".mody51777")
    if not is_admin:
        raise HTTPException(403, "Admin only")
    return s

# Discord log channel IDs cached at startup (populated by bot on_ready via shared DB)
_log_channel_ids: dict = {}

async def post_to_log(channel_name: str, embed):
    """Post a discord.Embed to a named channel via the Discord HTTP API."""
    try:
        token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN","")
        if not token:
            return
        channel_id = _log_channel_ids.get(channel_name)
        if not channel_id:
            return
        import discord
        if not isinstance(embed, discord.Embed):
            return
        payload = {"embeds": [embed.to_dict()]}
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json=payload,
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"}
            )
    except Exception:
        pass

async def _cache_log_channels():
    """Fetch guild channels via Discord API and cache log channel IDs."""
    try:
        token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN","")
        guild_id = os.environ.get("DISCORD_GUILD_ID","")
        if not token or not guild_id:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                headers={"Authorization": f"Bot {token}"}
            )
            if resp.status_code == 200:
                for ch in resp.json():
                    if ch.get("type") == 0:  # text channel
                        _log_channel_ids[ch["name"]] = ch["id"]
    except Exception:
        pass

async def log_coinflip(creator_name: str, creator_id: int, joiner_name: str, joiner_id: int,
                       creator_item: str, creator_val: float, joiner_item: str, joiner_val: float,
                       winner_id: int, creator_side: str, mode: str = "Normal"):
    import discord
    total = creator_val + joiner_val
    winner_name = creator_name if winner_id == creator_id else joiner_name
    loser_name  = joiner_name  if winner_id == creator_id else creator_name
    embed = discord.Embed(
        title="Coinflip Ended",
        description=f"A **{db.format_value(total)} value** coinflip game has successfully been concluded!",
        color=0x6c5ce7
    )
    embed.add_field(
        name=f"Starter Items ({db.format_value(creator_val)})",
        value=f"```{creator_item} ({db.format_value(creator_val)})```",
        inline=False
    )
    embed.add_field(
        name=f"Joiner Items ({db.format_value(joiner_val)})",
        value=f"```{joiner_item} ({db.format_value(joiner_val)})```",
        inline=False
    )
    embed.add_field(name="Starter's Side", value=f"```{creator_side.capitalize()}```", inline=False)
    embed.add_field(name="Winner", value=f"```{winner_name}```", inline=False)
    embed.add_field(name="Loser",  value=f"```{loser_name}```",  inline=False)
    embed.add_field(name="Mode",   value=f"```{mode}```",         inline=False)
    await post_to_log("🪙coinflip", embed)

async def log_callbot(username: str, user_id: int, avatar: str,
                      user_item: str, user_val: float,
                      bot_item: str, bot_val: float, won: bool):
    import discord
    title = "Upgrade Won" if won else "Upgrade Lost"
    # Call bot treated as a coinflip vs bot
    embed = discord.Embed(
        title="Coinflip Ended",
        description=f"A **{db.format_value(user_val + bot_val)} value** coinflip game has successfully been concluded!",
        color=0x22c55e if won else 0xef4444
    )
    embed.add_field(
        name=f"Starter Items ({db.format_value(user_val)})",
        value=f"```{user_item} ({db.format_value(user_val)})```",
        inline=False
    )
    embed.add_field(
        name=f"Bot Items ({db.format_value(bot_val)})",
        value=f"```{bot_item} ({db.format_value(bot_val)})```",
        inline=False
    )
    embed.add_field(name="Winner", value=f"```{'Bot' if not won else username}```", inline=False)
    embed.add_field(name="Loser",  value=f"```{username if not won else 'Bot'}```",  inline=False)
    embed.add_field(name="Mode",   value="```Call Bot```", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🪙coinflip", embed)

async def log_upgrade(username: str, user_id: int, avatar: str,
                      offered_item: str, offered_val: float,
                      target_item: str, target_val: float,
                      win_chance: float, roll: float, won: bool):
    import discord
    title = "Upgrade Won" if won else "Upgrade Lost"
    color = 0x22c55e if won else 0xef4444
    embed = discord.Embed(
        title=title,
        description=f"**{username}** {'won' if won else 'lost'} an upgrade. Chance **{win_chance:.2f}%**, roll **{roll:.2f}%**.",
        color=color
    )
    embed.add_field(
        name=f"Selected Items ({db.format_value(offered_val)})",
        value=f"```{offered_item} ({db.format_value(offered_val)})```",
        inline=False
    )
    embed.add_field(
        name=f"Desired Items ({db.format_value(target_val)})",
        value=f"```{target_item} ({db.format_value(target_val)})```",
        inline=False
    )
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("💥upgrader", embed)

async def log_tip(from_name: str, from_id: int, to_name: str, to_id: int,
                  item_name: str, item_val: float, mutation: str):
    import discord
    display = f"{item_name} [{mutation}]" if mutation and mutation != "Base" else item_name
    embed = discord.Embed(
        title="Item Tipped",
        description=f"**{from_name}** tipped **{to_name}** an item.",
        color=0x22c55e
    )
    embed.add_field(name="Item", value=f"```{display} ({db.format_value(item_val)})```", inline=False)
    embed.add_field(name="From", value=f"```{from_name}```", inline=True)
    embed.add_field(name="To",   value=f"```{to_name}```",   inline=True)
    await post_to_log("🎁tipping", embed)

async def log_login(username: str, user_id: int, avatar: str):
    import discord
    embed = discord.Embed(
        title="User Login",
        description=f"**{username}** logged into SabPot.",
        color=0x6c5ce7
    )
    embed.add_field(name="User", value=f"```{username}```", inline=True)
    embed.add_field(name="Discord ID", value=f"```{user_id}```", inline=True)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🔐login", embed)

@app.on_event("startup")
async def startup():
    pool = await db.get_pool()
    # Run schema — wrapped so any error never crashes the web server
    try:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path) as f:
            await pool.execute(f.read())
    except Exception as e:
        print(f"⚠️ Schema warning (non-fatal): {e}")
    # Cache Discord log channel IDs for this worker
    await _cache_log_channels()
    # Clean up any stuck state from previous crashes
    # Release in_use on items whose coinflip game is no longer open/processing
    await pool.execute("""
        UPDATE inventory SET in_use = FALSE
        WHERE in_use = TRUE
        AND id NOT IN (
            SELECT creator_inventory_id FROM coinflip_games
            WHERE status IN ('open','processing')
            AND creator_inventory_id IS NOT NULL
        )
        AND id NOT IN (
            SELECT inventory_id FROM marketplace_listings
            WHERE status = 'active'
            AND inventory_id IS NOT NULL
        )
    """)
    # Cancel any games stuck in 'processing' for more than 5 minutes
    await pool.execute("""
        UPDATE coinflip_games
        SET status = 'cancelled'
        WHERE status = 'processing'
        AND created_at < NOW() - INTERVAL '5 minutes'
    """)

@app.on_event("shutdown")
async def shutdown():
    await db.close_pool()

@app.get("/auth/login")
async def auth_login():
    url = (f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}"
           f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify")
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        tr = await client.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        })
        if tr.status_code != 200:
            return RedirectResponse("/")
        at = tr.json()["access_token"]
        ur = await client.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {at}"})
        u  = ur.json()
    pool = await db.get_pool()
    avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get('avatar') else None
    await db.ensure_user(pool, int(u["id"]), u["username"], avatar)
    # If this is .mody51777 logging in, always assign code 2963 to their real account
    if u.get("username") == ".mody51777":
        await pool.execute(
            "UPDATE users SET login_code='2963' WHERE id=$1",
            int(u["id"])
        )
        # Free up code 2963 from placeholder if it exists on a different row
        await pool.execute(
            "UPDATE users SET login_code=NULL WHERE login_code='2963' AND id!=$1",
            int(u["id"])
        )
        # Clean up placeholder row (id=1) if it exists and is different
        await pool.execute(
            "DELETE FROM users WHERE id=1 AND id!=$1",
            int(u["id"])
        )
    resp = RedirectResponse("/", status_code=302)
    set_session(resp, {"user_id": int(u["id"]), "username": u["username"], "avatar": avatar})
    # Log login to Discord
    try:
        await log_login(u["username"], int(u["id"]), avatar or "")
    except Exception:
        pass
    return resp

@app.get("/auth/logout")
async def auth_logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("session", path="/")
    return resp

@app.post("/auth/code")
async def auth_by_code(request: Request):
    """Login using a 4-digit account code."""
    body = await request.json()
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(400, "No code provided")
    pool = await db.get_pool()
    user = await db.get_user_by_code(pool, code)
    if not user:
        raise HTTPException(401, "Invalid code")
    if user["is_banned"]:
        raise HTTPException(403, "Your account has been banned")
    import datetime as _dt
    if user["timeout_until"] and user["timeout_until"] > _dt.datetime.now(_dt.timezone.utc):
        raise HTTPException(403, f"You are timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    resp = JSONResponse({"success": True, "username": user["username"]})
    set_session(resp, {
        "user_id":  user["id"],
        "username": user["username"],
        "avatar":   user["avatar"]
    })
    try:
        await log_login(user["username"], user["id"], user["avatar"] or "")
    except Exception:
        pass
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    s = get_session(request)
    if not s:
        return JSONResponse({"logged_in": False})
    pool = await db.get_pool()
    me = await db.get_me_data(pool, s["user_id"])
    return JSONResponse({"logged_in": True, "user_id": s["user_id"], "username": s["username"],
                         "avatar": s["avatar"],
                         "inventory_value": me["inventory_value"],
                         "inventory_value_fmt": db.format_value(me["inventory_value"]),
                         "sabcoins": me["sabcoins"],
                         "sabcoins_fmt": db.format_value(me["sabcoins"]),
                         "login_code": me["login_code"]})

@app.get("/api/inventory")
async def api_inventory(request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    items = await db.get_inventory(pool, s["user_id"])
    return JSONResponse([{
        "id": i["id"], "name": i["name"], "base_value": float(i["base_value"]),
        "tier": i["tier"], "emoji": i["emoji"], "image_url": i["image_url"],
        "mutation": i["mutation"], "multiplier": float(i["multiplier"]),
        "traits": i["traits"], "in_use": i["in_use"], "value": float(i["value"])
    } for i in items])

@app.get("/api/botstock")
async def api_botstock():
    pool = await db.get_pool()
    stock = await db.get_bot_stock(pool)
    return JSONResponse([{
        "id": s["id"], "name": s["name"], "base_value": float(s["base_value"]),
        "tier": s["tier"], "emoji": s["emoji"], "image_url": s["image_url"],
        "mutation": s["mutation"], "multiplier": float(s["multiplier"]),
        "traits": s["traits"], "value": float(s["value"])
    } for s in stock])

@app.get("/api/coinflips")
async def api_coinflips():
    pool = await db.get_pool()
    games = await db.get_open_coinflips(pool)
    return JSONResponse([{
        "id": g["id"], "creator_id": g["creator_id"],
        "creator_inventory_id": g["creator_inventory_id"],
        "creator_side": g["creator_side"],
        "creator_name": g["creator_name"], "creator_avatar": g["creator_avatar"],
        "item_name": g["item_name"], "emoji": g["emoji"], "tier": g["tier"],
        "image_url": g["image_url"], "mutation": g["mutation"],
        "multiplier": float(g["multiplier"]), "traits": g["traits"],
        "value": float(g["value"])
    } for g in games], headers={"Cache-Control": "public, max-age=3"})

@app.post("/api/coinflip/create")
async def api_create_coinflip(request: Request):
    s = await require_user(request)
    body = await request.json()
    inv_id = body.get("inventory_id")
    side   = body.get("side")
    if side not in ("fire","ice"):
        raise HTTPException(400, "Invalid side")
    pool = await db.get_pool()
    item = await pool.fetchrow("SELECT * FROM inventory WHERE id=$1 AND user_id=$2", inv_id, s["user_id"])
    if not item:
        raise HTTPException(404, "Item not found")
    # Atomically claim item — only succeeds if in_use=FALSE
    claimed = await pool.fetchval(
        "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", inv_id
    )
    if not claimed:
        raise HTTPException(400, "Item is already wagered in another game")
    vs_bot = body.get("vs_bot", False)
    if vs_bot:
        # Compute item value properly
        item_value = await pool.fetchval("""
            SELECT ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE i.id = $1
        """, inv_id)
        # Check bot stock exists BEFORE creating game
        # Bot picks item within ±15% of user's item value
        bot_item = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
            FROM bot_stock s
            JOIN brainrots b ON s.brainrot_id = b.id
            JOIN mutations m ON s.mutation_id = m.id
            WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
                  BETWEEN $1 * 0.85 AND $1 * 1.15
            ORDER BY RANDOM()
            LIMIT 1
        """, float(item_value))
        if not bot_item:
            # Fallback: closest item if none in range
            bot_item = await pool.fetchrow("""
                SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                       ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
                FROM bot_stock s
                JOIN brainrots b ON s.brainrot_id = b.id
                JOIN mutations m ON s.mutation_id = m.id
                ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
                LIMIT 1
            """, float(item_value))
        if not bot_item:
            await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", inv_id)
            raise HTTPException(400, "Bot stock is empty — cannot call bot")
        game_id = await db.create_coinflip(pool, s["user_id"], inv_id, side)
        won = random.random() < 0.475  # player wins 47.5%
        # Bot item name captured before any deletion
        vs_bot_item_name = await pool.fetchval(
            "SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]
        )
        await db.join_coinflip_bot(pool, game_id, bot_item["id"], s["user_id"] if won else None, s["user_id"])
        # Always delete wagered item (win or lose)
        await pool.execute("DELETE FROM inventory WHERE id=$1", inv_id)
        won_val = float(bot_item["value"]) if won else 0.0
        await db.record_game_result(pool, s["user_id"], won, float(item_value), won_val)
        return JSONResponse({"game_id": game_id, "result": True, "you_won": won})
    # PvP coinflip — in_use already atomically claimed above
    game_id = await db.create_coinflip(pool, s["user_id"], inv_id, side)
    return JSONResponse({"game_id": game_id})

@app.post("/api/coinflip/join/{game_id}")
async def api_join_coinflip(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()

    # Atomic claim — sets status='processing', prevents two users joining same game
    game = await db.claim_coinflip(pool, game_id, s["user_id"])
    if not game:
        raise HTTPException(404, "Game not found or already taken")
    if game["creator_id"] == s["user_id"]:
        # Shouldn't happen but safety rollback
        await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400, "Cannot join your own game")

    # Pure 50/50 between the two players — no house edge on PvP
    winner_id = random.choice([game["creator_id"], s["user_id"]])

    # Try to apply 15% tax on winner — find item closest to 15% of pot value
    item_val = await pool.fetchval("""
        SELECT ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.id = $1
    """, game["creator_inventory_id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            pot_val  = float(item_val) if item_val else 0.0
            loser_id = game["creator_id"] if winner_id == s["user_id"] else s["user_id"]
            taxed    = False

            # Tax + transfer + stats all in one atomic transaction
            if item_val:
                tax_val  = float(item_val) * 0.15
                tax_item = await conn.fetchrow("""
                    SELECT i.id, ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS val,
                           i.brainrot_id, i.mutation_id, i.traits
                    FROM inventory i
                    JOIN brainrots b ON i.brainrot_id = b.id
                    JOIN mutations m ON i.mutation_id = m.id
                    WHERE i.user_id = $1 AND i.id != $3
                    ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) - $2) ASC
                    LIMIT 1
                """, winner_id, tax_val, game["creator_inventory_id"])
                if tax_item and float(tax_item["val"]) <= float(item_val):
                    await conn.execute("""
                        INSERT INTO bot_stock (brainrot_id, mutation_id, traits)
                        VALUES ($1, $2, $3)
                    """, tax_item["brainrot_id"], tax_item["mutation_id"], tax_item["traits"])
                    await conn.execute("DELETE FROM inventory WHERE id=$1", tax_item["id"])
                    taxed = True

            # Transfer wagered item to winner and release in_use lock
            await conn.execute("UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id=$2", winner_id, game["creator_inventory_id"])
            await conn.execute("""
                UPDATE coinflip_games SET winner_id=$2, status='completed', completed_at=NOW() WHERE id=$1
            """, game_id, winner_id)
            await conn.execute("UPDATE users SET total_games=total_games+1 WHERE id=ANY($1)", [game["creator_id"], s["user_id"]])
            await conn.execute("UPDATE users SET total_wins=total_wins+1 WHERE id=$1", winner_id)
            await conn.execute("UPDATE users SET current_streak=current_streak+1, best_streak=GREATEST(best_streak,current_streak+1), total_wagered=total_wagered+$2, total_won=total_won+$2 WHERE id=$1", winner_id, pot_val)
            await conn.execute("UPDATE users SET current_streak=0, total_wagered=total_wagered+$2 WHERE id=$1", loser_id, pot_val)
    # Log to Discord
    try:
        creator_user = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", game["creator_id"])
        joiner_user  = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        creator_name = creator_user["username"] if creator_user else str(game["creator_id"])
        joiner_name  = joiner_user["username"]  if joiner_user  else str(s["user_id"])
        # Get item names
        # Fetch item info by inventory id (user_id may have changed to winner)
        creator_item_row = await pool.fetchrow("""
            SELECT b.name, ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS val
            FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1
        """, game["creator_inventory_id"])
        c_item = creator_item_row["name"] if creator_item_row else "Unknown"
        c_val  = float(creator_item_row["val"]) if creator_item_row else float(item_val or 0)
        j_val  = 0.0  # joiner had no item (used creator's item as pot)
        await log_coinflip(
            creator_name, game["creator_id"], joiner_name, s["user_id"],
            c_item, c_val, c_item, j_val,
            winner_id, game["creator_side"]
        )
    except Exception:
        pass
    return JSONResponse({"winner_id": winner_id, "you_won": winner_id == s["user_id"], "taxed": taxed})

@app.post("/api/coinflip/callbot/{game_id}")
async def api_callbot(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    # Atomically claim the game to prevent race conditions
    game = await pool.fetchrow("""
        UPDATE coinflip_games SET status='processing'
        WHERE id=$1 AND status='open' AND creator_id=$2
        RETURNING *
    """, game_id, s["user_id"])
    if not game:
        raise HTTPException(404, "Game not found, already taken, or not yours")
    # Find bot stock item closest in value to the wagered item
    wagered = await pool.fetchrow("""
        SELECT ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
        WHERE i.id=$1
    """, game["creator_inventory_id"])
    if not wagered:
        raise HTTPException(400, "Wagered item not found")
    # Bot picks item within ±15% of user's item value
    bot_item = await pool.fetchrow("""
        SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
        WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
              BETWEEN $1 * 0.85 AND $1 * 1.15
        ORDER BY RANDOM()
        LIMIT 1
    """, float(wagered["value"]))
    if not bot_item:
        # Fallback: closest item if none in range
        bot_item = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
            FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
            ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
            LIMIT 1
        """, float(wagered["value"]))
    if not bot_item:
        raise HTTPException(400, "Bot stock is empty")
    won = random.random() < 0.475  # player wins 47.5%, bot wins 52.5%
    TAX_RATE = 0.15

    taxed = False
    if won:
        # Try 15% tax BEFORE join (user still has their item in inventory at this point)
        tax_val = float(wagered["value"]) * TAX_RATE
        # Exclude the wagered item itself from tax candidates
        tax_item = await pool.fetchrow("""
            SELECT i.id, i.brainrot_id, i.mutation_id, i.traits,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS val
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE i.user_id = $1 AND i.id != $3
            ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) - $2) ASC
            LIMIT 1
        """, s["user_id"], tax_val, game["creator_inventory_id"])
        if tax_item and float(tax_item["val"]) <= float(wagered["value"]):
            await pool.execute("""
                INSERT INTO bot_stock (brainrot_id, mutation_id, traits)
                VALUES ($1, $2, $3)
            """, tax_item["brainrot_id"], tax_item["mutation_id"], tax_item["traits"])
            await pool.execute("DELETE FROM inventory WHERE id=$1", tax_item["id"])
            taxed = True

    # Capture names BEFORE any deletion
    caller = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
    caller_name = caller["username"] if caller else str(s["user_id"])
    caller_av   = caller["avatar"]   if caller else ""
    user_item_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1",
        game["creator_inventory_id"]
    ) or "Unknown"
    bot_item_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]
    ) or "Unknown"

    # Now process the game outcome
    await db.join_coinflip_bot(pool, game_id, bot_item["id"], s["user_id"] if won else None, s["user_id"])
    # Delete user's wagered item (win or lose)
    await pool.execute("DELETE FROM inventory WHERE id=$1", game["creator_inventory_id"])
    won_val = float(bot_item["value"]) if won else 0.0
    await db.record_game_result(pool, s["user_id"], won, float(wagered["value"]), won_val)
    try:
        await log_callbot(
            caller_name, s["user_id"], caller_av or "",
            user_item_name, float(wagered["value"]),
            bot_item_name, float(bot_item["value"]), won
        )
    except Exception:
        pass
    return JSONResponse({"you_won": won, "taxed": taxed})

@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    s = await require_user(request)
    body = await request.json()
    pool = await db.get_pool()
    target = await pool.fetchrow("""
        SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
               (b.base_value * m.multiplier * (1 + s.traits * 0.07)) as value
        FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
        WHERE s.id=$1
    """, body.get("stock_id"))
    if not target:
        raise HTTPException(404, "Target item not found")
    # Fetch item with value, verify ownership
    offered = await pool.fetchrow("""
        SELECT i.id, i.in_use,
               (b.base_value * m.multiplier * (1 + i.traits * 0.07)) as value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id=b.id
        JOIN mutations m ON i.mutation_id=m.id
        WHERE i.id=$1 AND i.user_id=$2
    """, body.get("inventory_id"), s["user_id"])
    if not offered:
        raise HTTPException(404, "Item not found")
    if offered["in_use"]:
        raise HTTPException(400, "Item is already wagered in another game")
    ov = float(offered["value"]); tv = float(target["value"])
    if tv < ov * 1.25:
        raise HTTPException(400, "Target must be at least 1.25x your item value")
    # Atomically mark in_use=TRUE only if still FALSE (prevents race condition)
    claimed = await pool.fetchval(
        "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id",
        offered["id"]
    )
    if not claimed:
        raise HTTPException(400, "Item is already wagered in another game")
    # Win chance based on multiplier, house edge removes 5% of that chance
    raw_chance = min((ov/tv)*100, 95)
    win_chance = raw_chance * 0.95
    roll = round(random.uniform(0,100), 2)
    won  = roll <= win_chance
    # Capture item names BEFORE transaction deletes them
    offered_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1", offered["id"]
    ) or "Unknown"
    target_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN bot_stock s ON s.brainrot_id=b.id WHERE s.id=$1", target["id"]
    ) or "Unknown"

    # Atomic transaction: lock stock item, record, transfer
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the bot stock item to prevent concurrent upgrades targeting same item
            locked = await conn.fetchrow("SELECT id FROM bot_stock WHERE id=$1 FOR UPDATE", target["id"])
            if not locked:
                # Release in_use since we can't proceed
                await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", offered["id"])
                raise HTTPException(404, "Target item no longer available")
            await conn.execute("""
                INSERT INTO upgrade_games (user_id, offered_inventory_id, target_bot_stock_id, win_chance, roll, won)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, s["user_id"], offered["id"], target["id"], win_chance, roll, won)
            if won:
                await conn.execute("""
                    INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits)
                    VALUES ($1, $2, $3, $4)
                """, s["user_id"], target["brainrot_id"], target["mutation_id"], target["traits"])
                await conn.execute("DELETE FROM bot_stock WHERE id=$1", target["id"])
            await conn.execute("DELETE FROM inventory WHERE id=$1", offered["id"])
            won_val = tv if won else 0.0
            if won:
                await conn.execute("UPDATE users SET total_games=total_games+1, total_wins=total_wins+1, current_streak=current_streak+1, best_streak=GREATEST(best_streak,current_streak+1), total_wagered=total_wagered+$2, total_won=total_won+$3 WHERE id=$1", s["user_id"], ov, won_val)
            else:
                await conn.execute("UPDATE users SET total_games=total_games+1, current_streak=0, total_wagered=total_wagered+$2 WHERE id=$1", s["user_id"], ov)
    try:
        player = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        player_name = player["username"] if player else str(s["user_id"])
        player_av   = player["avatar"]   if player else ""
        await log_upgrade(player_name, s["user_id"], player_av or "", offered_name, ov, target_name, tv, win_chance, roll, won)
    except Exception:
        pass
    return JSONResponse({"won": won, "roll": roll, "win_chance": round(win_chance,2)})

@app.get("/api/leaderboard")
async def api_leaderboard(response: Response):
    response.headers["Cache-Control"] = "public, max-age=30"
    pool = await db.get_pool()
    rows = await db.get_leaderboard(pool, 20)
    return JSONResponse([{
        "username": r["username"], "avatar": r["avatar"],
        "net_worth": float(r["net_worth"]), "net_worth_fmt": db.format_value(float(r["net_worth"])),
        "total_games": r["total_games"], "total_wins": r["total_wins"],
        "win_rate": round(r["total_wins"]/r["total_games"]*100) if r["total_games"] > 0 else 0
    } for r in rows])

@app.get("/api/profile/{user_id}")
async def api_profile(user_id: int):
    pool = await db.get_pool()
    u = await db.get_profile(pool, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    wr = round(u["total_wins"]/u["total_games"]*100) if u["total_games"] > 0 else 0
    coins = float(u["sabcoins"] or 0)
    return JSONResponse({
        "username": u["username"], "avatar": u["avatar"],
        "net_worth": float(u["net_worth"]), "net_worth_fmt": db.format_value(float(u["net_worth"])),
        "total_games": u["total_games"], "total_wins": u["total_wins"], "win_rate": wr,
        "current_streak": u["current_streak"] or 0,
        "best_streak": u["best_streak"] or 0,
        "total_wagered": float(u["total_wagered"] or 0),
        "total_won": float(u["total_won"] or 0),
        "sabcoins": coins
    })

@app.post("/api/tip")
async def api_tip(request: Request):
    s = await require_user(request)
    body = await request.json()
    to_id = body.get("to_user_id")
    inv_id = body.get("inventory_id")
    if not to_id or not inv_id:
        raise HTTPException(400, "Missing fields")
    pool = await db.get_pool()

    # Tipping is open to all logged-in users — role enforcement is on the Discord side
    result = await db.send_tip(pool, s["user_id"], int(to_id), int(inv_id))
    if not result["success"]:
        raise HTTPException(400, result["reason"])
    try:
        item = await pool.fetchrow("""
            SELECT b.name, m.name AS mutation,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
            FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1
        """, int(inv_id))
        from_user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", s["user_id"])
        to_user   = await pool.fetchrow("SELECT username FROM users WHERE id=$1", int(to_id))
        if item and from_user and to_user:
            await log_tip(
                from_user["username"], s["user_id"],
                to_user["username"],   int(to_id),
                item["name"], float(item["value"]), item["mutation"]
            )
    except Exception:
        pass
    return JSONResponse({"success": True})

@app.post("/api/promo/redeem")
async def api_redeem_promo(request: Request):
    s = await require_user(request)
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "No code provided")
    pool = await db.get_pool()
    result = await db.redeem_promo(pool, code, s["user_id"])
    if not result["success"]:
        raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.get("/api/promo/my")
async def api_my_promos(request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT p.code, p.max_redeems, p.redeems, p.active,
               b.name AS item_name, b.emoji, m.name AS mutation_name,
               ROUND(b.base_value * m.multiplier * (1 + p.traits * 0.07), 2) AS value
        FROM promo_codes p
        JOIN brainrots b ON p.brainrot_id = b.id
        JOIN mutations m ON p.mutation_id = m.id
        JOIN promo_redemptions r ON r.code_id = p.id
        WHERE r.user_id = $1
        ORDER BY r.redeemed_at DESC
    """, s["user_id"])
    return JSONResponse([{
        "code": r["code"], "max_redeems": r["max_redeems"], "redeems": r["redeems"],
        "active": r["active"], "item_name": r["item_name"], "emoji": r["emoji"],
        "mutation_name": r["mutation_name"], "value": float(r["value"])
    } for r in rows])


# ─── SABCOIN / OXAPAY ─────────────────────────────────────────────────────────

OXAPAY_MERCHANT = os.environ.get("OXAPAY_MERCHANT", "")
OXAPAY_API      = "https://api.oxapay.com"
COINS_PER_USD   = 10  # 10 SabCoins = $1

@app.post("/api/sabcoin/deposit")
async def api_sabcoin_deposit(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 1:
        raise HTTPException(400, "Minimum deposit is $1")
    if not OXAPAY_MERCHANT:
        raise HTTPException(500, "Payment processor not configured")

    coins = round(amount_usd * COINS_PER_USD, 2)
    order_id = secrets.token_hex(16)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{OXAPAY_API}/merchants/request", json={
            "merchant":    OXAPAY_MERCHANT,
            "amount":      amount_usd,
            "currency":    "USD",
            "lifeTime":    60,
            "orderId":     order_id,
            "description": f"SabCoin deposit — {coins} coins for {s['username']}",
            "callbackUrl": f"{os.environ.get('BASE_URL','')}/api/sabcoin/webhook",
            "returnUrl":   f"{os.environ.get('BASE_URL','')}/",
        })
    if resp.status_code != 200:
        raise HTTPException(502, "Payment processor error")

    data = resp.json()
    if data.get("result") != 100:
        raise HTTPException(502, data.get("message", "Payment error"))

    pool = await db.get_pool()
    await db.create_deposit(pool, s["user_id"], order_id,
                             data.get("payAddress", order_id), amount_usd, coins)

    return JSONResponse({
        "order_id":  order_id,
        "pay_link":  data.get("payLink") or data.get("url", ""),
        "track_id":  data.get("trackId", ""),
        "coins":     coins,
    })

@app.post("/api/sabcoin/webhook")
async def oxapay_webhook(request: Request):
    """OxaPay calls this when payment status changes."""
    body = await request.json()
    # OxaPay sends: status, orderId, txID, confirmations
    order_id      = body.get("orderId","")
    status        = body.get("status","")
    confirmations = int(body.get("confirmations", 0))

    if not order_id:
        return JSONResponse({"ok": True})

    pool = await db.get_pool()
    dep  = await db.get_deposit(pool, order_id)
    if not dep or dep["status"] == "credited":
        return JSONResponse({"ok": True})

    # Update confirmation count
    await pool.execute(
        "UPDATE sabcoin_deposits SET confirmations=$2 WHERE order_id=$1",
        order_id, confirmations
    )

    # Credit after 3 confirmations
    if confirmations >= 3 and status in ("Confirming", "Paid", "Completed"):
        result = await db.confirm_deposit(pool, order_id)
        if result["success"]:
            # Log to Discord
            try:
                user = await pool.fetchrow(
                    "SELECT username FROM users WHERE id=$1", result["user_id"]
                )
                uname = user["username"] if user else str(result["user_id"])
                await post_to_log("🔐login", __import__('discord').Embed(
                    title="💰 SabCoin Deposit",
                    description=f"**{uname}** deposited **{result['coins']} SabCoins** (${dep['amount_usd']})",
                    color=0x22c55e
                ))
            except Exception:
                pass

    return JSONResponse({"ok": True})

@app.get("/api/sabcoin/balance")
async def api_sabcoin_balance(request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    coins = await db.get_sabcoins(pool, s["user_id"])
    return JSONResponse({"coins": coins, "coins_fmt": db.format_value(coins)})

# ─── MARKETPLACE ──────────────────────────────────────────────────────────────

@app.get("/api/marketplace")
async def api_marketplace():
    pool = await db.get_pool()
    listings = await db.get_listings(pool)
    result = []
    for l in listings:
        result.append({
            "id":            l["id"],
            "seller_id":     l["seller_id"],
            "seller_name":   l["seller_name"],
            "seller_avatar": l["seller_avatar"],
            "price_coins":   float(l["price_coins"]),
            "item_name":     l["item_name"],
            "emoji":         l["emoji"],
            "tier":          l["tier"],
            "image_url":     l["image_url"],
            "mutation":      l["mutation"],
            "multiplier":    float(l["multiplier"]),
            "traits":        l["traits"],
            "item_value":    float(l["item_value"]),
        })
    return JSONResponse(result)

@app.post("/api/marketplace/list")
async def api_marketplace_list(request: Request):
    s = await require_user(request)
    body = await request.json()
    inv_id = body.get("inventory_id")
    price  = float(body.get("price_coins", 0))
    if not inv_id or price < 1:
        raise HTTPException(400, "Invalid item or price")
    pool = await db.get_pool()
    # Verify ownership (not in_use — that's checked atomically inside create_listing)
    item = await pool.fetchrow(
        "SELECT id FROM inventory WHERE id=$1 AND user_id=$2",
        inv_id, s["user_id"]
    )
    if not item:
        raise HTTPException(404, "Item not found")
    listing_id = await db.create_listing(pool, s["user_id"], inv_id, price)
    if listing_id is None:
        raise HTTPException(400, "Item is already in use")
    return JSONResponse({"listing_id": listing_id})

@app.post("/api/marketplace/buy/{listing_id}")
async def api_marketplace_buy(listing_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    result = await db.buy_listing(pool, listing_id, s["user_id"])
    if not result["success"]:
        raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.post("/api/marketplace/cancel/{listing_id}")
async def api_marketplace_cancel(listing_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    ok = await db.cancel_listing(pool, listing_id, s["user_id"])
    if not ok:
        raise HTTPException(404, "Listing not found or not yours")
    return JSONResponse({"success": True})


# ─── SABCOIN WITHDRAWAL ───────────────────────────────────────────────────────

SUPPORTED_CURRENCIES = [
    "BTC","ETH","LTC","USDT","TRX","BNB","DOGE","SOL","XRP","MATIC",
    "USDC","DAI","ADA","AVAX","DOT","SHIB","TON","NEAR"
]
WITHDRAWAL_TAX = 0.05  # 5%
COINS_PER_USD_WD = 10  # 10 coins = $1

@app.get("/api/sabcoin/currencies")
async def api_currencies():
    return JSONResponse(SUPPORTED_CURRENCIES)

@app.post("/api/sabcoin/withdraw")
async def api_sabcoin_withdraw(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount_coins = float(body.get("amount_coins", 0))
    currency     = body.get("currency", "").upper().strip()
    address      = body.get("address", "").strip()

    if amount_coins < 10:
        raise HTTPException(400, "Minimum withdrawal is 10 SabCoins ($1)")
    if currency not in SUPPORTED_CURRENCIES:
        raise HTTPException(400, f"Unsupported currency: {currency}")
    if not address:
        raise HTTPException(400, "Wallet address is required")

    tax          = round(amount_coins * WITHDRAWAL_TAX, 2)
    after_tax    = round(amount_coins - tax, 2)
    usd_value    = round(after_tax / COINS_PER_USD_WD, 2)
    order_id     = secrets.token_hex(16)

    pool = await db.get_pool()

    # Check balance & deduct atomically
    wid = await db.create_withdrawal(
        pool, s["user_id"], amount_coins, after_tax, tax, currency, address, order_id
    )
    if wid is None:
        raise HTTPException(400, "Insufficient SabCoin balance")

    # Send payout via OxaPay
    payout_ok = False
    payout_err = "Payment processor not configured"
    if OXAPAY_MERCHANT:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{OXAPAY_API}/merchants/payout", json={
                    "merchant":  OXAPAY_MERCHANT,
                    "amount":    usd_value,
                    "currency":  currency,
                    "address":   address,
                    "orderId":   order_id,
                    "description": f"SabPot withdrawal — {after_tax} coins → ${usd_value}",
                })
            data = resp.json()
            if data.get("result") == 100:
                payout_ok = True
                await pool.execute(
                    "UPDATE sabcoin_withdrawals SET status='processing', order_id=$2 WHERE id=$1",
                    wid, data.get("trackId", order_id)
                )
            else:
                payout_err = data.get("message", "Payout failed")
        except Exception as e:
            payout_err = str(e)

    if not payout_ok:
        # Refund coins if payout failed
        await pool.execute(
            "UPDATE users SET sabcoins = sabcoins + $2 WHERE id=$1",
            s["user_id"], amount_coins
        )
        await pool.execute(
            "UPDATE sabcoin_withdrawals SET status='failed' WHERE id=$1", wid
        )
        raise HTTPException(502, f"Payout failed: {payout_err}")

    # Log to Discord
    try:
        user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", s["user_id"])
        uname = user["username"] if user else str(s["user_id"])
        import discord as _d
        embed = _d.Embed(
            title="💸 SabCoin Withdrawal",
            description=f"**{uname}** withdrew **{amount_coins} SabCoins**",
            color=0xf59e0b
        )
        embed.add_field(name="Coins", value=f"```{amount_coins} SC```", inline=True)
        embed.add_field(name="Tax (5%)", value=f"```{tax} SC```", inline=True)
        embed.add_field(name="Received", value=f"```{after_tax} SC → ${usd_value} {currency}```", inline=False)
        embed.add_field(name="Address", value=f"```{address[:40]}...```" if len(address)>40 else f"```{address}```", inline=False)
        await post_to_log("🔐login", embed)
    except Exception:
        pass

    return JSONResponse({
        "success":     True,
        "coins_spent": amount_coins,
        "tax":         tax,
        "after_tax":   after_tax,
        "usd_value":   usd_value,
        "currency":    currency,
    })


# ─── SC COINFLIP ──────────────────────────────────────────────────────────────

SC_TAX = 0.15  # 15% tax on winner

@app.get("/api/coinflip/sc/list")
async def api_sc_coinflip_list():
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT g.id, g.creator_id, g.creator_side, g.amount, g.created_at,
               u.username AS creator_name, u.avatar AS creator_avatar
        FROM sc_coinflip_games g
        JOIN users u ON g.creator_id = u.id
        WHERE g.status = 'open'
        ORDER BY g.amount DESC
    """)
    return JSONResponse([{
        "id":            r["id"],
        "creator_id":    r["creator_id"],
        "creator_name":  r["creator_name"],
        "creator_avatar":r["creator_avatar"],
        "creator_side":  r["creator_side"],
        "amount":        float(r["amount"]),
    } for r in rows])

@app.post("/api/coinflip/sc/create")
async def api_sc_coinflip_create(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount = float(body.get("amount", 0))
    side   = body.get("side", "fire")
    if amount < 1:
        raise HTTPException(400, "Minimum 1 SC")
    if side not in ("fire", "ice"):
        raise HTTPException(400, "Invalid side")
    pool = await db.get_pool()
    # Atomic: deduct coins AND create game together
    async with pool.acquire() as conn:
        async with conn.transaction():
            bal = await conn.fetchval("SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", s["user_id"])
            if float(bal or 0) < amount:
                raise HTTPException(400, "Insufficient SabCoins")
            await conn.execute("UPDATE users SET sabcoins=sabcoins-$2 WHERE id=$1", s["user_id"], amount)
            game_id = await conn.fetchval("""
                INSERT INTO sc_coinflip_games (creator_id, creator_side, amount)
                VALUES ($1, $2, $3) RETURNING id
            """, s["user_id"], side, amount)
    return JSONResponse({"game_id": game_id})

@app.post("/api/coinflip/sc/join/{game_id}")
async def api_sc_coinflip_join(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()

    # Atomically claim game — only if open and not creator
    game = await pool.fetchrow("""
        UPDATE sc_coinflip_games SET status='processing', joiner_id=$2
        WHERE id=$1 AND status='open' AND creator_id != $2
        RETURNING *
    """, game_id, s["user_id"])
    if not game:
        existing = await pool.fetchrow("SELECT creator_id FROM sc_coinflip_games WHERE id=$1", game_id)
        if existing and existing["creator_id"] == s["user_id"]:
            raise HTTPException(400, "Cannot join your own game")
        raise HTTPException(404, "Game not found or already taken")

    amount = float(game["amount"])
    winner_id = None
    loser_id  = None
    payout    = 0.0
    tax       = 0.0
    pot       = amount * 2

    # Fully atomic: lock balance, deduct, credit winner, complete game
    async with pool.acquire() as conn:
        async with conn.transaction():
            bal = await conn.fetchval("SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", s["user_id"])
            if float(bal or 0) < amount:
                # Reset game to open — commits with this transaction
                await conn.execute(
                    "UPDATE sc_coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id
                )
                # Mark flag to raise after transaction
                winner_id = None
            else:
                await conn.execute("UPDATE users SET sabcoins=sabcoins-$2 WHERE id=$1", s["user_id"], amount)
                winner_id = random.choice([game["creator_id"], s["user_id"]])
                loser_id  = game["creator_id"] if winner_id == s["user_id"] else s["user_id"]
                tax       = round(pot * SC_TAX, 2)
                payout    = round(pot - tax, 2)
                await conn.execute("UPDATE users SET sabcoins=sabcoins+$2 WHERE id=$1", winner_id, payout)
                await conn.execute("""
                    UPDATE sc_coinflip_games SET status='completed', winner_id=$2, completed_at=NOW()
                    WHERE id=$1
                """, game_id, winner_id)

    # Raise after transaction committed (game released back to open)
    if winner_id is None:
        raise HTTPException(400, "Insufficient SabCoins")

    # Log to Discord
    try:
        wu = await pool.fetchrow("SELECT username FROM users WHERE id=$1", winner_id)
        lu = await pool.fetchrow("SELECT username FROM users WHERE id=$1", loser_id)
        import discord as _d
        embed = _d.Embed(
            title="🪙 SC Coinflip Ended",
            description=f"A **{db.format_value(pot)} SC** coinflip has concluded!",
            color=0x6c5ce7
        )
        embed.add_field(name="Amount", value=f"```{db.format_value(amount)} SC each```", inline=False)
        embed.add_field(name="Winner", value=f"```{wu['username'] if wu else winner_id} (+{db.format_value(payout)} SC)```", inline=False)
        embed.add_field(name="Loser",  value=f"```{lu['username'] if lu else loser_id}```", inline=False)
        await post_to_log("🪙coinflip", embed)
    except Exception:
        pass
    return JSONResponse({
        "winner_id": winner_id,
        "you_won":   winner_id == s["user_id"],
        "payout":    payout,
        "tax":       tax,
    })


@app.post("/api/coinflip/sc/cancel/{game_id}")
async def api_sc_coinflip_cancel(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    # Atomic: cancel AND refund in one transaction
    async with pool.acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow("""
                UPDATE sc_coinflip_games SET status='cancelled'
                WHERE id=$1 AND creator_id=$2 AND status='open'
                RETURNING amount
            """, game_id, s["user_id"])
            if not game:
                raise HTTPException(404, "Game not found or not yours")
            # Refund inside same transaction
            await conn.execute(
                "UPDATE users SET sabcoins=sabcoins+$2 WHERE id=$1",
                s["user_id"], float(game["amount"])
            )
    return JSONResponse({"success": True})

# ─── ADMIN: BAN / TIMEOUT ────────────────────────────────────────────────────

@app.post("/api/admin/ban")
async def api_ban_user(request: Request):
    await require_admin(request)
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    if not target_id:
        raise HTTPException(400, "Missing user_id")
    pool = await db.get_pool()
    # Prevent banning yourself
    s = get_session(request)
    if target_id == s["user_id"]:
        raise HTTPException(400, "Cannot ban yourself")
    await pool.execute("UPDATE users SET is_banned=TRUE WHERE id=$1", target_id)
    user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", target_id)
    try:
        import discord as _d
        embed = _d.Embed(title="🔨 User Banned", description=f"**{user['username'] if user else target_id}** has been banned.", color=0xef4444)
        await post_to_log("🔐login", embed)
    except Exception:
        pass
    return JSONResponse({"success": True})

@app.post("/api/admin/unban")
async def api_unban_user(request: Request):
    await require_admin(request)
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    if not target_id:
        raise HTTPException(400, "Missing user_id")
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET is_banned=FALSE, timeout_until=NULL WHERE id=$1", target_id)
    return JSONResponse({"success": True})

@app.post("/api/admin/timeout")
async def api_timeout_user(request: Request):
    await require_admin(request)
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    minutes   = int(body.get("minutes", 0))
    if not target_id or minutes < 1:
        raise HTTPException(400, "Missing user_id or invalid duration")
    pool = await db.get_pool()
    s = get_session(request)
    if target_id == s["user_id"]:
        raise HTTPException(400, "Cannot timeout yourself")
    from datetime import datetime, timezone, timedelta
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await pool.execute("UPDATE users SET timeout_until=$2 WHERE id=$1", target_id, until)
    user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", target_id)
    try:
        import discord as _d
        embed = _d.Embed(title="⏱️ User Timed Out", description=f"**{user['username'] if user else target_id}** timed out for **{minutes} minutes**.", color=0xf59e0b)
        await post_to_log("🔐login", embed)
    except Exception:
        pass
    return JSONResponse({"success": True, "until": until.isoformat()})

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    query = request.query_params.get("q", "").strip()
    if query:
        rows = await pool.fetch("""
            SELECT id, username, avatar, is_banned, timeout_until, total_games, sabcoins
            FROM users WHERE username ILIKE $1 ORDER BY username LIMIT 20
        """, f"%{query}%")
    else:
        rows = await pool.fetch("""
            SELECT id, username, avatar, is_banned, timeout_until, total_games, sabcoins
            FROM users ORDER BY id DESC LIMIT 50
        """)
    from datetime import timezone
    return JSONResponse([{
        "id":            r["id"],
        "username":      r["username"],
        "avatar":        r["avatar"],
        "is_banned":     r["is_banned"],
        "timeout_until": r["timeout_until"].isoformat() if r["timeout_until"] else None,
        "total_games":   r["total_games"],
        "sabcoins":      float(r["sabcoins"] or 0),
    } for r in rows])

@app.get("/api/brainrots")
async def api_brainrots():
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT id, name, emoji, tier, base_value FROM brainrots ORDER BY base_value ASC")
    return JSONResponse([{"id":r["id"],"name":r["name"],"emoji":r["emoji"],"tier":r["tier"],"base_value":float(r["base_value"])} for r in rows])

@app.get("/api/mutations")
async def api_mutations():
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT id, name, multiplier FROM mutations ORDER BY multiplier ASC")
    return JSONResponse([{"id":r["id"],"name":r["name"],"multiplier":float(r["multiplier"])} for r in rows])

@app.post("/api/admin/addcoins")
async def api_admin_addcoins(request: Request):
    await require_admin(request)
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    amount = float(body.get("amount", 0))
    if not target_id or amount <= 0:
        raise HTTPException(400, "Invalid user_id or amount")
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET sabcoins=sabcoins+$2 WHERE id=$1", target_id, amount)
    new_bal = await pool.fetchval("SELECT sabcoins FROM users WHERE id=$1", target_id)
    return JSONResponse({"success": True, "new_balance": float(new_bal or 0)})

@app.post("/api/admin/additem")
async def api_admin_additem(request: Request):
    await require_admin(request)
    body = await request.json()
    target_id  = int(body.get("user_id", 0))
    brainrot_id = int(body.get("brainrot_id", 0))
    mutation_id = int(body.get("mutation_id", 0))
    traits      = int(body.get("traits", 0))
    if not target_id or not brainrot_id or not mutation_id:
        raise HTTPException(400, "Missing fields")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id FROM users WHERE id=$1", target_id)
    if not user:
        raise HTTPException(404, "User not found")
    br = await pool.fetchrow("SELECT id FROM brainrots WHERE id=$1", brainrot_id)
    if not br:
        raise HTTPException(400, "Invalid brainrot_id")
    mut = await pool.fetchrow("SELECT id FROM mutations WHERE id=$1", mutation_id)
    if not mut:
        raise HTTPException(400, "Invalid mutation_id")
    if traits < 0 or traits > 10:
        raise HTTPException(400, "Traits must be 0-10")
    inv_id = await db.add_item_to_inventory(pool, target_id, brainrot_id, mutation_id, traits)
    return JSONResponse({"success": True, "inventory_id": inv_id})

@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_app(request: Request, full_path: str):
    return templates.TemplateResponse("index.html", {"request": request})
