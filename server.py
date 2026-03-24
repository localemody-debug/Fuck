import os, random, secrets
from contextlib import asynccontextmanager
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

# ─── POOL ─────────────────────────────────────────────────────────────────────
# FIX: server.py owns its own pool, created in lifespan on uvicorn's event loop.
# Never shares pool with bot.py (different threads/loops).
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await db.init_pool()
    return _pool

# ─── APP ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global _pool
    _pool = await db.init_pool()

    try:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path) as f:
            schema_sql = f.read()
        async with _pool.acquire() as conn:
            await conn.execute(schema_sql)
    except Exception as e:
        print(f"⚠️ Schema warning (non-fatal): {e}")

    await _cache_log_channels()

    # Release stuck in_use items
    await _pool.execute("""
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

    # Cancel stuck processing games
    await _pool.execute("""
        UPDATE coinflip_games SET status = 'cancelled'
        WHERE status = 'processing'
        AND created_at < NOW() - INTERVAL '5 minutes'
    """)

    yield

    # Shutdown
    if _pool:
        await db.close_pool(_pool)


app = FastAPI(title="SabPot", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)

_pets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pets")
os.makedirs(_pets_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")

templates = Jinja2Templates(directory="templates")

# ─── RATE LIMITER ─────────────────────────────────────────────────────────────

import collections
_rate_store: dict = collections.defaultdict(list)
RATE_LIMIT  = 120
RATE_WINDOW = 60
_rate_last_cleanup = time.time()

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            global _rate_last_cleanup
            ip = request.headers.get("x-forwarded-for","").split(",")[0].strip() \
                 or (request.client.host if request.client else "unknown")
            now = time.time()
            hits = _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
            if len(hits) >= RATE_LIMIT:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            _rate_store[ip].append(now)
            if now - _rate_last_cleanup > 300:
                _rate_last_cleanup = now
                stale = [k for k,v in list(_rate_store.items()) if not v or now - v[-1] > RATE_WINDOW]
                for k in stale:
                    del _rate_store[k]
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

# ─── AUTH ─────────────────────────────────────────────────────────────────────

SECRET_KEY      = os.environ["SECRET_KEY"]
CLIENT_ID       = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET   = os.environ["DISCORD_CLIENT_SECRET"]
REDIRECT_URI    = os.environ["REDIRECT_URI"]
DISCORD_API     = "https://discord.com/api/v10"

serializer = URLSafeTimedSerializer(SECRET_KEY)

def set_session(response, data):
    token = serializer.dumps(data)
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        max_age=60*60*24*7, path="/")

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
    pool = await get_pool()
    user = await pool.fetchrow(
        "SELECT is_banned, timeout_until FROM users WHERE id=$1", s["user_id"]
    )
    if user:
        if user["is_banned"]:
            raise HTTPException(403, "Your account has been banned")
        if user["timeout_until"] and user["timeout_until"] > __import__('datetime').datetime.now(__import__('datetime').timezone.utc):
            raise HTTPException(403, f"You are timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    return s

ADMIN_USER_CODE     = "2963"
ADMIN_DISCORD_ID    = 1482825709331157134

async def require_admin(request):
    s = await require_user(request)
    pool = await get_pool()
    row = await pool.fetchrow("SELECT login_code, username FROM users WHERE id=$1", s["user_id"])
    if not row:
        raise HTTPException(403, "Admin only")
    is_admin = (row["login_code"] == ADMIN_USER_CODE or
                row["username"] == ".mody51777" or
                s.get("username") == ".mody51777")
    if not is_admin:
        raise HTTPException(403, "Admin only")
    return s

# ─── DISCORD LOGGING ──────────────────────────────────────────────────────────

_log_channel_ids: dict = {}

async def post_to_log(channel_name: str, embed):
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
    try:
        token    = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN","")
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
                    if ch.get("type") == 0:
                        _log_channel_ids[ch["name"]] = ch["id"]
    except Exception:
        pass

async def log_coinflip(creator_name, creator_id, joiner_name, joiner_id,
                        creator_item, creator_val, joiner_item, joiner_val,
                        winner_id, creator_side, mode="Normal"):
    import discord
    total = creator_val + joiner_val
    winner_name = creator_name if winner_id == creator_id else joiner_name
    loser_name  = joiner_name  if winner_id == creator_id else creator_name
    embed = discord.Embed(title="Coinflip Ended",
                          description=f"A **{db.format_value(total)} value** coinflip has concluded!",
                          color=0x6c5ce7)
    embed.add_field(name=f"Starter Items ({db.format_value(creator_val)})",
                    value=f"```{creator_item} ({db.format_value(creator_val)})```", inline=False)
    embed.add_field(name=f"Joiner Items ({db.format_value(joiner_val)})",
                    value=f"```{joiner_item} ({db.format_value(joiner_val)})```", inline=False)
    embed.add_field(name="Starter's Side", value=f"```{creator_side.capitalize()}```", inline=False)
    embed.add_field(name="Winner", value=f"```{winner_name}```", inline=False)
    embed.add_field(name="Loser",  value=f"```{loser_name}```",  inline=False)
    embed.add_field(name="Mode",   value=f"```{mode}```",        inline=False)
    await post_to_log("🪙coinflip", embed)

async def log_callbot(username, user_id, avatar, user_item, user_val, bot_item, bot_val, won):
    import discord
    embed = discord.Embed(title="Coinflip Ended",
                          description=f"A **{db.format_value(user_val + bot_val)} value** coinflip vs Bot concluded!",
                          color=0x22c55e if won else 0xef4444)
    embed.add_field(name=f"Starter Items ({db.format_value(user_val)})",
                    value=f"```{user_item} ({db.format_value(user_val)})```", inline=False)
    embed.add_field(name=f"Bot Items ({db.format_value(bot_val)})",
                    value=f"```{bot_item} ({db.format_value(bot_val)})```", inline=False)
    embed.add_field(name="Winner", value=f"```{'Bot' if not won else username}```", inline=False)
    embed.add_field(name="Loser",  value=f"```{username if not won else 'Bot'}```", inline=False)
    embed.add_field(name="Mode",   value="```Call Bot```", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🪙coinflip", embed)

async def log_upgrade(username, user_id, avatar, offered_item, offered_val,
                       target_item, target_val, win_chance, roll, won):
    import discord
    embed = discord.Embed(title="Upgrade Won" if won else "Upgrade Lost",
                          description=f"**{username}** {'won' if won else 'lost'} an upgrade. "
                                      f"Chance **{win_chance:.2f}%**, roll **{roll:.2f}%**.",
                          color=0x22c55e if won else 0xef4444)
    embed.add_field(name=f"Selected Items ({db.format_value(offered_val)})",
                    value=f"```{offered_item} ({db.format_value(offered_val)})```", inline=False)
    embed.add_field(name=f"Desired Items ({db.format_value(target_val)})",
                    value=f"```{target_item} ({db.format_value(target_val)})```", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("💥upgrader", embed)

async def log_tip(from_name, from_id, to_name, to_id, item_name, item_val, mutation):
    import discord
    display = f"{item_name} [{mutation}]" if mutation and mutation != "Base" else item_name
    embed = discord.Embed(title="Item Tipped",
                          description=f"**{from_name}** tipped **{to_name}** an item.",
                          color=0x22c55e)
    embed.add_field(name="Item", value=f"```{display} ({db.format_value(item_val)})```", inline=False)
    embed.add_field(name="From", value=f"```{from_name}```", inline=True)
    embed.add_field(name="To",   value=f"```{to_name}```",   inline=True)
    await post_to_log("🎁tipping", embed)

async def log_login(username, user_id, avatar):
    import discord
    embed = discord.Embed(title="User Login",
                          description=f"**{username}** logged into SabPot.",
                          color=0x6c5ce7)
    embed.add_field(name="User",       value=f"```{username}```", inline=True)
    embed.add_field(name="Discord ID", value=f"```{user_id}```",  inline=True)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🔐login", embed)

# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────

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

    pool   = await get_pool()
    avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get('avatar') else None
    await db.ensure_user(pool, int(u["id"]), u["username"], avatar)

    if u.get("username") == ".mody51777":
        await pool.execute("UPDATE users SET login_code='2963' WHERE id=$1", int(u["id"]))
        await pool.execute("UPDATE users SET login_code=NULL WHERE login_code='2963' AND id!=$1", int(u["id"]))
        await pool.execute("DELETE FROM users WHERE id=1 AND id!=$1", int(u["id"]))

    resp = RedirectResponse("/", status_code=302)
    set_session(resp, {"user_id": int(u["id"]), "username": u["username"], "avatar": avatar})
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
    body = await request.json()
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(400, "No code provided")
    pool = await get_pool()
    user = await db.get_user_by_code(pool, code)
    if not user:
        raise HTTPException(401, "Invalid code")
    if user["is_banned"]:
        raise HTTPException(403, "Your account has been banned")
    import datetime as _dt
    if user["timeout_until"] and user["timeout_until"] > _dt.datetime.now(_dt.timezone.utc):
        raise HTTPException(403, f"You are timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    resp = JSONResponse({"success": True, "username": user["username"]})
    set_session(resp, {"user_id": user["id"], "username": user["username"], "avatar": user["avatar"]})
    try:
        await log_login(user["username"], user["id"], user["avatar"] or "")
    except Exception:
        pass
    return resp

# ─── API: ME / INVENTORY / BOTSTOCK ───────────────────────────────────────────

@app.get("/api/me")
async def api_me(request: Request):
    s = get_session(request)
    if not s:
        return JSONResponse({"logged_in": False})
    pool = await get_pool()
    me   = await db.get_me_data(pool, s["user_id"])
    return JSONResponse({"logged_in": True, "user_id": s["user_id"], "username": s["username"],
                         "avatar": s["avatar"],
                         "inventory_value": me["inventory_value"],
                         "inventory_value_fmt": db.format_value(me["inventory_value"]),
                         "sabcoins": me["sabcoins"],
                         "sabcoins_fmt": db.format_value(me["sabcoins"]),
                         "login_code": me["login_code"]})

@app.get("/api/inventory")
async def api_inventory(request: Request):
    s    = await require_user(request)
    pool = await get_pool()
    items = await db.get_inventory(pool, s["user_id"])
    return JSONResponse([{
        "id": i["id"], "name": i["name"], "base_value": float(i["base_value"]),
        "tier": i["tier"], "emoji": i["emoji"], "image_url": i["image_url"],
        "mutation": i["mutation"], "multiplier": float(i["multiplier"]),
        "traits": i["traits"], "in_use": i["in_use"], "value": float(i["value"])
    } for i in items])

@app.get("/api/botstock")
async def api_botstock():
    pool  = await get_pool()
    stock = await db.get_bot_stock(pool)
    return JSONResponse([{
        "id": s["id"], "name": s["name"], "base_value": float(s["base_value"]),
        "tier": s["tier"], "emoji": s["emoji"], "image_url": s["image_url"],
        "mutation": s["mutation"], "multiplier": float(s["multiplier"]),
        "traits": s["traits"], "value": float(s["value"])
    } for s in stock])

# ─── API: COINFLIPS ───────────────────────────────────────────────────────────

@app.get("/api/coinflips")
async def api_coinflips():
    pool  = await get_pool()
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
    s    = await require_user(request)
    body = await request.json()
    inv_id = body.get("inventory_id")
    side   = body.get("side")
    if side not in ("fire","ice"):
        raise HTTPException(400, "Invalid side")

    pool = await get_pool()
    item = await pool.fetchrow("SELECT * FROM inventory WHERE id=$1 AND user_id=$2", inv_id, s["user_id"])
    if not item:
        raise HTTPException(404, "Item not found")

    claimed = await pool.fetchval(
        "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", inv_id
    )
    if not claimed:
        raise HTTPException(400, "Item is already wagered in another game")

    vs_bot = body.get("vs_bot", False)
    if vs_bot:
        item_value = await pool.fetchval("""
            SELECT ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE i.id = $1
        """, inv_id)

        bot_item = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
            FROM bot_stock s
            JOIN brainrots b ON s.brainrot_id = b.id
            JOIN mutations m ON s.mutation_id = m.id
            WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
                  BETWEEN $1 * 0.85 AND $1 * 1.15
            ORDER BY RANDOM() LIMIT 1
        """, float(item_value))

        if not bot_item:
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
        won     = random.random() < 0.475

        vs_bot_item_name = await pool.fetchval(
            "SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]
        )

        await db.join_coinflip_bot(pool, game_id, bot_item["id"],
                                    s["user_id"] if won else None, s["user_id"])
        await pool.execute("DELETE FROM inventory WHERE id=$1", inv_id)
        won_val = float(bot_item["value"]) if won else 0.0
        await db.record_game_result(pool, s["user_id"], won, float(item_value), won_val)
        return JSONResponse({"game_id": game_id, "result": True, "you_won": won})

    game_id = await db.create_coinflip(pool, s["user_id"], inv_id, side)
    return JSONResponse({"game_id": game_id})

@app.post("/api/coinflip/join/{game_id}")
async def api_join_coinflip(game_id: int, request: Request):
    s    = await require_user(request)
    pool = await get_pool()

    game = await db.claim_coinflip(pool, game_id, s["user_id"])
    if not game:
        raise HTTPException(404, "Game not found or already taken")
    if game["creator_id"] == s["user_id"]:
        await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400, "Cannot join your own game")

    winner_id = random.choice([game["creator_id"], s["user_id"]])

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

            await conn.execute("UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id=$2",
                               winner_id, game["creator_inventory_id"])
            await conn.execute("""
                UPDATE coinflip_games SET winner_id=$2, status='completed', completed_at=NOW() WHERE id=$1
            """, game_id, winner_id)
            await conn.execute("UPDATE users SET total_games=total_games+1 WHERE id=ANY($1)",
                               [game["creator_id"], s["user_id"]])
            await conn.execute("UPDATE users SET total_wins=total_wins+1 WHERE id=$1", winner_id)
            await conn.execute("""
                UPDATE users SET current_streak=current_streak+1,
                    best_streak=GREATEST(best_streak,current_streak+1),
                    total_wagered=total_wagered+$2, total_won=total_won+$2
                WHERE id=$1
            """, winner_id, pot_val)
            await conn.execute("""
                UPDATE users SET current_streak=0, total_wagered=total_wagered+$2 WHERE id=$1
            """, loser_id, pot_val)

    try:
        creator_user = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", game["creator_id"])
        joiner_user  = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        creator_name = creator_user["username"] if creator_user else str(game["creator_id"])
        joiner_name  = joiner_user["username"]  if joiner_user  else str(s["user_id"])
        creator_item_row = await pool.fetchrow("""
            SELECT b.name, ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS val
            FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1
        """, game["creator_inventory_id"])
        c_item = creator_item_row["name"] if creator_item_row else "Unknown"
        c_val  = float(creator_item_row["val"]) if creator_item_row else float(item_val or 0)
        await log_coinflip(creator_name, game["creator_id"], joiner_name, s["user_id"],
                           c_item, c_val, c_item, 0.0, winner_id, game["creator_side"])
    except Exception:
        pass

    return JSONResponse({"winner_id": winner_id, "you_won": winner_id == s["user_id"], "taxed": taxed})

@app.post("/api/coinflip/callbot/{game_id}")
async def api_callbot(game_id: int, request: Request):
    s    = await require_user(request)
    pool = await get_pool()

    game = await pool.fetchrow("""
        UPDATE coinflip_games SET status='processing'
        WHERE id=$1 AND status='open' AND creator_id=$2
        RETURNING *
    """, game_id, s["user_id"])
    if not game:
        raise HTTPException(404, "Game not found, already taken, or not yours")

    wagered = await pool.fetchrow("""
        SELECT ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
        WHERE i.id=$1
    """, game["creator_inventory_id"])
    if not wagered:
        raise HTTPException(400, "Wagered item not found")

    bot_item = await pool.fetchrow("""
        SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
        WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
              BETWEEN $1 * 0.85 AND $1 * 1.15
        ORDER BY RANDOM() LIMIT 1
    """, float(wagered["value"]))

    if not bot_item:
        bot_item = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
            FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
            ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
            LIMIT 1
        """, float(wagered["value"]))

    if not bot_item:
        raise HTTPException(400, "Bot stock is empty")

    won      = random.random() < 0.475
    TAX_RATE = 0.15
    taxed    = False

    if won:
        tax_val  = float(wagered["value"]) * TAX_RATE
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

    caller        = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
    caller_name   = caller["username"] if caller else str(s["user_id"])
    caller_av     = caller["avatar"]   if caller else ""
    user_item_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1",
        game["creator_inventory_id"]
    ) or "Unknown"
    bot_item_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]
    ) or "Unknown"

    await db.join_coinflip_bot(pool, game_id, bot_item["id"],
                                s["user_id"] if won else None, s["user_id"])
    await pool.execute("DELETE FROM inventory WHERE id=$1", game["creator_inventory_id"])
    won_val = float(bot_item["value"]) if won else 0.0
    await db.record_game_result(pool, s["user_id"], won, float(wagered["value"]), won_val)

    try:
        await log_callbot(caller_name, s["user_id"], caller_av or "",
                          user_item_name, float(wagered["value"]),
                          bot_item_name, float(bot_item["value"]), won)
    except Exception:
        pass

    return JSONResponse({"you_won": won, "taxed": taxed})

# ─── API: UPGRADE ─────────────────────────────────────────────────────────────

@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    s    = await require_user(request)
    body = await request.json()
    pool = await get_pool()

    target = await pool.fetchrow("""
        SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
               (b.base_value * m.multiplier * (1 + s.traits * 0.07)) as value
        FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
        WHERE s.id=$1
    """, body.get("stock_id"))
    if not target:
        raise HTTPException(404, "Target item not found")

    offered = await pool.fetchrow("""
        SELECT i.id, i.in_use, (b.base_value * m.multiplier * (1 + i.traits * 0.07)) as value
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

    claimed = await pool.fetchval(
        "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", offered["id"]
    )
    if not claimed:
        raise HTTPException(400, "Item is already wagered in another game")

    raw_chance = min((ov/tv)*100, 95)
    win_chance = raw_chance * 0.95
    roll       = round(random.uniform(0,100), 2)
    won        = roll <= win_chance

    offered_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1", offered["id"]
    ) or "Unknown"
    target_name = await pool.fetchval(
        "SELECT b.name FROM brainrots b JOIN bot_stock s ON s.brainrot_id=b.id WHERE s.id=$1", target["id"]
    ) or "Unknown"

    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow("SELECT id FROM bot_stock WHERE id=$1 FOR UPDATE", target["id"])
            if not locked:
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
                await conn.execute("""
                    UPDATE users SET total_games=total_games+1, total_wins=total_wins+1,
                        current_streak=current_streak+1,
                        best_streak=GREATEST(best_streak,current_streak+1),
                        total_wagered=total_wagered+$2, total_won=total_won+$3
                    WHERE id=$1
                """, s["user_id"], ov, won_val)
            else:
                await conn.execute("""
                    UPDATE users SET total_games=total_games+1, current_streak=0,
                        total_wagered=total_wagered+$2
                    WHERE id=$1
                """, s["user_id"], ov)

    try:
        player      = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        player_name = player["username"] if player else str(s["user_id"])
        player_av   = player["avatar"]   if player else ""
        await log_upgrade(player_name, s["user_id"], player_av or "",
                          offered_name, ov, target_name, tv, win_chance, roll, won)
    except Exception:
        pass

    return JSONResponse({"won": won, "roll": roll, "win_chance": round(win_chance,2)})

# ─── API: LEADERBOARD / PROFILE ───────────────────────────────────────────────

@app.get("/api/leaderboard")
async def api_leaderboard(response: Response):
    response.headers["Cache-Control"] = "public, max-age=30"
    pool = await get_pool()
    rows = await db.get_leaderboard(pool, 20)
    return JSONResponse([{
        "username": r["username"], "avatar": r["avatar"],
        "net_worth": float(r["net_worth"]), "net_worth_fmt": db.format_value(float(r["net_worth"])),
        "total_games": r["total_games"], "total_wins": r["total_wins"],
        "win_rate": round(r["total_wins"]/r["total_games"]*100) if r["total_games"] > 0 else 0
    } for r in rows])

@app.get("/api/profile/{user_id}")
async def api_profile(user_id: int):
    pool = await get_pool()
    u    = await db.get_profile(pool, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    wr    = round(u["total_wins"]/u["total_games"]*100) if u["total_games"] > 0 else 0
    coins = float(u["sabcoins"] or 0)
    return JSONResponse({
        "username": u["username"], "avatar": u["avatar"],
        "net_worth": float(u["net_worth"]), "net_worth_fmt": db.format_value(float(u["net_worth"])),
        "total_games": u["total_games"], "total_wins": u["total_wins"], "win_rate": wr,
        "current_streak": u["current_streak"] or 0, "best_streak": u["best_streak"] or 0,
        "total_wagered": float(u["total_wagered"] or 0),
        "total_won": float(u["total_won"] or 0),
        "sabcoins": coins
    })

# ─── API: TIP / PROMO ─────────────────────────────────────────────────────────

@app.post("/api/tip")
async def api_tip(request: Request):
    s    = await require_user(request)
    body = await request.json()
    to_id  = body.get("to_user_id")
    inv_id = body.get("inventory_id")
    if not to_id or not inv_id:
        raise HTTPException(400, "Missing fields")
    pool   = await get_pool()
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
            await log_tip(from_user["username"], s["user_id"],
                          to_user["username"], int(to_id),
                          item["name"], float(item["value"]), item["mutation"])
    except Exception:
        pass
    return JSONResponse({"success": True})

@app.post("/api/promo/redeem")
async def api_redeem_promo(request: Request):
    s    = await require_user(request)
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "No code provided")
    pool   = await get_pool()
    result = await db.redeem_promo(pool, code, s["user_id"])
    if not result["success"]:
        raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.get("/api/promo/my")
async def api_my_promos(request: Request):
    s    = await require_user(request)
    pool = await get_pool()
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
COINS_PER_USD   = 10

@app.post("/api/sabcoin/deposit")
async def api_sabcoin_deposit(request: Request):
    s    = await require_user(request)
    body = await request.json()
    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 1:
        raise HTTPException(400, "Minimum deposit is $1")
    if not OXAPAY_MERCHANT:
        raise HTTPException(500, "Payment processor not configured")
    coins    = round(amount_usd * COINS_PER_USD, 2)
    order_id = secrets.token_hex(16)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{OXAPAY_API}/merchants/request", json={
            "merchant": OXAPAY_MERCHANT, "amount": amount_usd, "currency": "USD",
            "lifeTime": 60, "orderId": order_id,
            "description": f"SabCoin deposit — {coins} coins for {s['username']}",
            "callbackUrl": f"{os.environ.get('BASE_URL','')}/api/sabcoin/webhook",
            "returnUrl":   f"{os.environ.get('BASE_URL','')}/",
        })
        if resp.status_code != 200:
            raise HTTPException(502, "Payment processor error")
        data = resp.json()
        if data.get("result") != 100:
            raise HTTPException(502, data.get("message", "Payment error"))
    pool = await get_pool()
    await db.create_deposit(pool, s["user_id"], order_id,
                             data.get("payAddress", order_id), amount_usd, coins)
    return JSONResponse({"order_id": order_id,
                         "pay_link": data.get("payLink") or data.get("url", ""),
                         "track_id": data.get("trackId", ""), "coins": coins})

@app.post("/api/sabcoin/webhook")
async def oxapay_webhook(request: Request):
    body          = await request.json()
    order_id      = body.get("orderId","")
    status        = body.get("status","")
    confirmations = int(body.get("confirmations", 0))
    if not order_id:
        return JSONResponse({"ok": True})
    pool = await get_pool()
    dep  = await db.get_deposit(pool, order_id)
    if not dep or dep["status"] == "credited":
        return JSONResponse({"ok": True})
    await pool.execute("UPDATE sabcoin_deposits SET confirmations=$2 WHERE order_id=$1",
                       order_id, confirmations)
    if confirmations >= 3 and status in ("Confirming", "Paid", "Completed"):
        result = await db.confirm_deposit(pool, order_id)
        if result["success"]:
            try:
                user  = await pool.fetchrow("SELECT username FROM users WHERE id=$1", result["user_id"])
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
    s    = await require_user(request)
    pool = await get_pool()
    coins = await db.get_sabcoins(pool, s["user_id"])
    return JSONResponse({"coins": coins, "coins_fmt": db.format_value(coins)})

# ─── MARKETPLACE ──────────────────────────────────────────────────────────────

@app.get("/api/marketplace")
async def api_marketplace():
    pool     = await get_pool()
    listings = await db.get_listings(pool)
    return JSONResponse([{
        "id": l["id"], "seller_id": l["seller_id"],
        "seller_name": l["seller_name"], "seller_avatar": l["seller_avatar"],
        "price_coins": float(l["price_coins"]),
        "item_name": l["item_name"], "emoji": l["emoji"], "tier": l["tier"],
        "image_url": l["image_url"], "mutation": l["mutation"],
        "multiplier": float(l["multiplier"]), "traits": l["traits"],
        "item_value": float(l["item_value"]),
    } for l in listings])

@app.post("/api/marketplace/list")
async def api_marketplace_list(request: Request):
    s    = await require_user(request)
    body = await request.json()
    pool = await get_pool()
    listing_id = await db.create_listing(pool, s["user_id"],
                                          int(body.get("inventory_id")),
                                          float(body.get("price")))
    if not listing_id:
        raise HTTPException(400, "Item already in use or not found")
    return JSONResponse({"listing_id": listing_id})

@app.post("/api/marketplace/buy/{listing_id}")
async def api_marketplace_buy(listing_id: int, request: Request):
    s      = await require_user(request)
    pool   = await get_pool()
    result = await db.buy_listing(pool, listing_id, s["user_id"])
    if not result["success"]:
        raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.post("/api/marketplace/cancel/{listing_id}")
async def api_marketplace_cancel(listing_id: int, request: Request):
    s      = await require_user(request)
    pool   = await get_pool()
    ok     = await db.cancel_listing(pool, listing_id, s["user_id"])
    if not ok:
        raise HTTPException(404, "Listing not found or not yours")
    return JSONResponse({"success": True})

# ─── ADMIN: USERS LIST ────────────────────────────────────────────────────────
# FIX: New endpoint so admin panel shows all users in a dropdown instead of
#      requiring manual ID entry.

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    await require_admin(request)
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT id, username, avatar,
               COALESCE(SUM(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)), 0) AS net_worth
        FROM users u
        LEFT JOIN inventory i ON i.user_id = u.id
        LEFT JOIN brainrots b ON i.brainrot_id = b.id
        LEFT JOIN mutations m ON i.mutation_id = m.id
        GROUP BY u.id, u.username, u.avatar
        ORDER BY u.username ASC
    """)
    return JSONResponse([{
        "id": str(r["id"]),
        "username": r["username"],
        "avatar": r["avatar"],
        "net_worth": float(r["net_worth"]),
        "net_worth_fmt": db.format_value(float(r["net_worth"]))
    } for r in rows])

# ─── ADMIN: ADD ITEM ──────────────────────────────────────────────────────────
# FIX: Was silently failing because user_id came in as string and ensure_user
#      wasn't called, so the FK insert failed quietly.

@app.post("/api/admin/additem")
async def api_admin_additem(request: Request):
    await require_admin(request)
    body       = await request.json()
    pool       = await get_pool()

    # Validate all fields present
    user_id    = body.get("user_id")
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))

    if not user_id or not brainrot_id or not mutation_id:
        raise HTTPException(400, "Missing user_id, brainrot_id, or mutation_id")

    user_id     = int(user_id)
    brainrot_id = int(brainrot_id)
    mutation_id = int(mutation_id)

    # FIX: Verify the user actually exists in the DB before inserting
    user = await pool.fetchrow("SELECT id, username, avatar FROM users WHERE id=$1", user_id)
    if not user:
        raise HTTPException(404, f"User {user_id} not found in database. They must log in first.")

    # Verify brainrot and mutation exist
    brainrot = await pool.fetchrow("SELECT id, name FROM brainrots WHERE id=$1", brainrot_id)
    if not brainrot:
        raise HTTPException(404, f"Brainrot {brainrot_id} not found")

    mutation = await pool.fetchrow("SELECT id, name FROM mutations WHERE id=$1", mutation_id)
    if not mutation:
        raise HTTPException(404, f"Mutation {mutation_id} not found")

    # FIX: Actually insert and return the new inventory ID
    try:
        inv_id = await pool.fetchval("""
            INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits)
            VALUES ($1, $2, $3, $4) RETURNING id
        """, user_id, brainrot_id, mutation_id, traits)
    except Exception as e:
        raise HTTPException(500, f"Database insert failed: {str(e)}")

    if not inv_id:
        raise HTTPException(500, "Insert returned no ID — item was not added")

    # Calculate value for response
    val_row = await pool.fetchrow("""
        SELECT ROUND(b.base_value * m.multiplier * (1 + $3 * 0.07), 2) AS value,
               b.name AS brainrot_name, m.name AS mutation_name
        FROM brainrots b, mutations m
        WHERE b.id=$1 AND m.id=$2
    """, brainrot_id, mutation_id, traits)

    return JSONResponse({
        "success": True,
        "inventory_id": inv_id,
        "user": user["username"],
        "item": val_row["brainrot_name"] if val_row else brainrot["name"],
        "mutation": val_row["mutation_name"] if val_row else mutation["name"],
        "traits": traits,
        "value": float(val_row["value"]) if val_row else 0,
        "value_fmt": db.format_value(float(val_row["value"])) if val_row else "0"
    })

@app.post("/api/admin/removeitem")
async def api_admin_removeitem(request: Request):
    await require_admin(request)
    body   = await request.json()
    inv_id = body.get("inventory_id")
    if not inv_id:
        raise HTTPException(400, "Missing inventory_id")
    pool = await get_pool()
    row  = await pool.fetchrow("SELECT id FROM inventory WHERE id=$1", int(inv_id))
    if not row:
        raise HTTPException(404, "Item not found")
    await db.remove_item_from_inventory(pool, int(inv_id))
    return JSONResponse({"success": True})

@app.post("/api/admin/addstock")
async def api_admin_addstock(request: Request):
    await require_admin(request)
    body        = await request.json()
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))
    if not brainrot_id or not mutation_id:
        raise HTTPException(400, "Missing fields")
    pool     = await get_pool()
    stock_id = await db.add_to_bot_stock(pool, int(brainrot_id), int(mutation_id), traits)
    return JSONResponse({"success": True, "stock_id": stock_id})

@app.post("/api/admin/removestock")
async def api_admin_removestock(request: Request):
    await require_admin(request)
    body     = await request.json()
    stock_id = body.get("stock_id")
    if not stock_id:
        raise HTTPException(400, "Missing stock_id")
    pool = await get_pool()
    await db.remove_from_bot_stock(pool, int(stock_id))
    return JSONResponse({"success": True})

@app.get("/api/admin/promos")
async def api_admin_promos(request: Request):
    await require_admin(request)
    pool = await get_pool()
    rows = await db.get_all_promos(pool)
    return JSONResponse([{
        "id": r["id"], "code": r["code"],
        "max_redeems": r["max_redeems"], "redeems": r["redeems"],
        "active": r["active"], "item_name": r["item_name"],
        "emoji": r["emoji"], "mutation_name": r["mutation_name"],
        "value": float(r["value"])
    } for r in rows])

# ─── SABCOIN WITHDRAWAL ───────────────────────────────────────────────────────

@app.post("/api/sabcoin/withdraw")
async def api_sabcoin_withdraw(request: Request):
    s    = await require_user(request)
    body = await request.json()
    pool = await get_pool()
    amount_coins  = float(body.get("amount_coins", 0))
    currency      = body.get("currency", "LTC")
    address       = body.get("address","").strip()
    if amount_coins < 10:
        raise HTTPException(400, "Minimum withdrawal is 10 SabCoins")
    if not address:
        raise HTTPException(400, "Address required")
    TAX           = 0.10
    amount_after  = round(amount_coins * (1 - TAX), 2)
    tax_burned    = round(amount_coins * TAX, 2)
    order_id      = secrets.token_hex(16)
    wid = await db.create_withdrawal(pool, s["user_id"], amount_coins,
                                      amount_after, tax_burned, currency, address, order_id)
    if not wid:
        raise HTTPException(400, "Insufficient SabCoins")
    return JSONResponse({"success": True, "amount_after": amount_after, "tax": tax_burned, "order_id": order_id})

# ─── PAGE ROUTES ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
