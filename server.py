import os, random, secrets
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
import httpx
from dotenv import load_dotenv
import db
import datetime

load_dotenv()

from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import time

app = FastAPI(title="SabPot", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)

# ALL errors return JSON — never HTML
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})

_pets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pets")
os.makedirs(_pets_dir, exist_ok=True)
app.mount("/static", StaticFiles(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
), name="static")

templates = Jinja2Templates(directory="templates")

import collections
_rate_store: dict = collections.defaultdict(list)
RATE_LIMIT = 120
RATE_WINDOW = 60
_rate_last_cleanup = time.time()

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            global _rate_last_cleanup
            ip = (request.headers.get("x-forwarded-for","").split(",")[0].strip()
                  or (request.client.host if request.client else "unknown"))
            now = time.time()
            hits = _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
            if len(hits) >= RATE_LIMIT:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            _rate_store[ip].append(now)
            if now - _rate_last_cleanup > 300:
                _rate_last_cleanup = now
                stale = [k for k,v in list(_rate_store.items()) if not v or now - v[-1] > RATE_WINDOW]
                for k in stale: del _rate_store[k]
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

SECRET_KEY    = os.environ["SECRET_KEY"]
CLIENT_ID     = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
REDIRECT_URI  = os.environ["REDIRECT_URI"]
DISCORD_API   = "https://discord.com/api/v10"
ADMIN_USER_CODE = "2963"

serializer = URLSafeTimedSerializer(SECRET_KEY)

def set_session(response, data):
    token = serializer.dumps(data)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60*60*24*7, path="/")

def get_session(request):
    token = request.cookies.get("session")
    if not token: return None
    try: return serializer.loads(token, max_age=60*60*24*7)
    except Exception: return None

async def require_user(request):
    s = get_session(request)
    if not s: raise HTTPException(401, "Not logged in")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT is_banned, timeout_until FROM users WHERE id=$1", s["user_id"])
    if user:
        if user["is_banned"]: raise HTTPException(403, "Your account has been banned")
        if user["timeout_until"] and user["timeout_until"] > datetime.datetime.now(datetime.timezone.utc):
            raise HTTPException(403, f"You are timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    return s

async def require_admin(request):
    s = await require_user(request)
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT login_code, username FROM users WHERE id=$1", s["user_id"])
    if not row: raise HTTPException(403, "Admin only")
    if not (row["login_code"] == ADMIN_USER_CODE or row["username"] == ".mody51777" or s.get("username") == ".mody51777"):
        raise HTTPException(403, "Admin only")
    return s

# ─── DISCORD LOG HELPERS ──────────────────────────────────────────────────────

_log_channel_ids: dict = {}

async def post_to_log(channel_name: str, embed):
    try:
        token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN","")
        if not token: return
        channel_id = _log_channel_ids.get(channel_name)
        if not channel_id: return
        import discord
        if not isinstance(embed, discord.Embed): return
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json={"embeds": [embed.to_dict()]},
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"})
    except Exception: pass

async def _cache_log_channels():
    try:
        token    = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN","")
        guild_id = os.environ.get("DISCORD_GUILD_ID","")
        if not token or not guild_id: return
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                                    headers={"Authorization": f"Bot {token}"})
            if resp.status_code == 200:
                for ch in resp.json():
                    if ch.get("type") == 0: _log_channel_ids[ch["name"]] = ch["id"]
    except Exception: pass

async def log_coinflip(creator_name, creator_id, joiner_name, joiner_id,
                       creator_items_str, creator_val, joiner_items_str, joiner_val,
                       winner_id, creator_side, mode="Normal"):
    import discord
    total = creator_val + joiner_val
    winner_name = creator_name if winner_id == creator_id else joiner_name
    loser_name  = joiner_name  if winner_id == creator_id else creator_name
    embed = discord.Embed(title="Coinflip Ended",
        description=f"A **{db.format_value(total)} value** coinflip concluded!", color=0x6c5ce7)
    embed.add_field(name=f"Starter ({db.format_value(creator_val)})",
                    value=f"```{creator_items_str}```", inline=False)
    embed.add_field(name=f"Joiner ({db.format_value(joiner_val)})",
                    value=f"```{joiner_items_str}```", inline=False)
    embed.add_field(name="Side",   value=f"```{creator_side.capitalize()}```", inline=True)
    embed.add_field(name="Winner", value=f"```{winner_name}```", inline=True)
    embed.add_field(name="Loser",  value=f"```{loser_name}```",  inline=True)
    embed.add_field(name="Mode",   value=f"```{mode}```",        inline=True)
    await post_to_log("🪙coinflip", embed)

async def log_callbot(username, user_id, avatar, user_items_str, user_val, bot_items_str, bot_val, won):
    import discord
    embed = discord.Embed(title="Coinflip Ended",
        description=f"A **{db.format_value(user_val+bot_val)} value** coinflip concluded!",
        color=0x22c55e if won else 0xef4444)
    embed.add_field(name=f"Player ({db.format_value(user_val)})", value=f"```{user_items_str}```", inline=False)
    embed.add_field(name=f"Bot ({db.format_value(bot_val)})",     value=f"```{bot_items_str}```",  inline=False)
    embed.add_field(name="Winner", value=f"```{'Bot' if not won else username}```", inline=True)
    embed.add_field(name="Mode",   value="```Call Bot```", inline=True)
    if avatar: embed.set_thumbnail(url=avatar)
    await post_to_log("🪙coinflip", embed)

async def log_upgrade(username, user_id, avatar, offered_str, offered_val,
                      target_str, target_val, win_chance, roll, won):
    import discord
    embed = discord.Embed(
        title="Upgrade Won" if won else "Upgrade Lost",
        description=f"**{username}** {'won' if won else 'lost'} — Chance **{win_chance:.1f}%**, Roll **{roll:.1f}%**",
        color=0x22c55e if won else 0xef4444)
    embed.add_field(name=f"Offered ({db.format_value(offered_val)})", value=f"```{offered_str}```", inline=False)
    embed.add_field(name=f"Target ({db.format_value(target_val)})",   value=f"```{target_str}```",  inline=False)
    if avatar: embed.set_thumbnail(url=avatar)
    await post_to_log("💥upgrader", embed)

async def log_tip(from_name, from_id, to_name, to_id, item_name, item_val, mutation):
    import discord
    display = f"{item_name} [{mutation}]" if mutation and mutation != "Base" else item_name
    embed = discord.Embed(title="Item Tipped", description=f"**{from_name}** → **{to_name}**", color=0x22c55e)
    embed.add_field(name="Item", value=f"```{display} ({db.format_value(item_val)})```", inline=False)
    await post_to_log("🎁tipping", embed)

async def log_login(username, user_id, avatar):
    import discord
    embed = discord.Embed(title="User Login", description=f"**{username}** logged in.", color=0x6c5ce7)
    embed.add_field(name="User", value=f"```{username}```", inline=True)
    embed.add_field(name="ID",   value=f"```{user_id}```",  inline=True)
    if avatar: embed.set_thumbnail(url=avatar)
    await post_to_log("🔐login", embed)

# ─── STARTUP ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    pool = await db.get_pool()
    try:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path) as f:
            await pool.execute(f.read())
    except Exception as e:
        print(f"⚠️ Schema warning: {e}")
    await _cache_log_channels()

    # NEW SCHEMA MIGRATIONS for multi-item coinflip/upgrade
    migrations = [
        # coinflip_items table — one row per item in a coinflip game (replaces single creator_inventory_id)
        """CREATE TABLE IF NOT EXISTS coinflip_items (
            id         SERIAL PRIMARY KEY,
            game_id    INT     NOT NULL REFERENCES coinflip_games(id) ON DELETE CASCADE,
            inventory_id INT   NOT NULL REFERENCES inventory(id),
            owner_id   BIGINT  NOT NULL REFERENCES users(id),
            side       TEXT    NOT NULL CHECK (side IN ('creator','joiner')),
            added_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_coinflip_items_game ON coinflip_items(game_id)",
        # upgrade_items — multiple offered items, multiple target stock items
        """CREATE TABLE IF NOT EXISTS upgrade_items (
            id           SERIAL PRIMARY KEY,
            game_id      INT  NOT NULL REFERENCES upgrade_games(id) ON DELETE CASCADE,
            inventory_id INT  REFERENCES inventory(id),
            stock_id     INT  REFERENCES bot_stock(id),
            side         TEXT NOT NULL CHECK (side IN ('offered','target'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_upgrade_items_game ON upgrade_items(game_id)",
        # chat
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            message    TEXT   NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at DESC)",
        # Add total_value column to coinflip_games for multi-item pots
        "ALTER TABLE coinflip_games ADD COLUMN IF NOT EXISTS creator_total_value NUMERIC(14,2) DEFAULT 0",
        "ALTER TABLE coinflip_games ADD COLUMN IF NOT EXISTS joiner_total_value  NUMERIC(14,2) DEFAULT 0",
        # Keep old creator_inventory_id nullable for backward compat
        "ALTER TABLE coinflip_games ALTER COLUMN creator_inventory_id DROP NOT NULL" ,
    ]
    for sql in migrations:
        try:
            await pool.execute(sql)
        except Exception as e:
            print(f"⚠️ Migration skipped ({e})")

    # Release stuck in_use locks
    await pool.execute("""
        UPDATE inventory SET in_use = FALSE
        WHERE in_use = TRUE
        AND id NOT IN (
            SELECT inventory_id FROM coinflip_items ci
            JOIN coinflip_games cg ON ci.game_id = cg.id
            WHERE cg.status IN ('open','processing')
        )
        AND id NOT IN (
            SELECT inventory_id FROM marketplace_listings WHERE status='active' AND inventory_id IS NOT NULL
        )
    """)
    # Cancel stuck games
    await pool.execute("""
        UPDATE coinflip_games SET status='cancelled'
        WHERE status='processing' AND created_at < NOW() - INTERVAL '5 minutes'
    """)
    await pool.execute("""
        UPDATE sc_coinflip_games SET status='cancelled'
        WHERE status='processing' AND created_at < NOW() - INTERVAL '5 minutes'
    """)
    # Trim chat
    try:
        await pool.execute("""
            DELETE FROM chat_messages WHERE id NOT IN (
                SELECT id FROM chat_messages ORDER BY created_at DESC LIMIT 500
            )
        """)
    except Exception: pass

@app.on_event("shutdown")
async def shutdown():
    await db.close_pool()

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def auth_login():
    url = (f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}"
           f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify")
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = None, error: str = None):
    if error or not code: return RedirectResponse("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            tr = await client.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
            })
            if tr.status_code != 200: return RedirectResponse("/")
            at = tr.json().get("access_token")
            if not at: return RedirectResponse("/")
            ur = await client.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {at}"})
            u = ur.json()
    except Exception: return RedirectResponse("/")
    if not u.get("id"): return RedirectResponse("/")
    pool = await db.get_pool()
    avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get("avatar") else None
    await db.ensure_user(pool, int(u["id"]), u["username"], avatar)
    if u.get("username") == ".mody51777":
        await pool.execute("UPDATE users SET login_code='2963' WHERE id=$1", int(u["id"]))
        await pool.execute("UPDATE users SET login_code=NULL WHERE login_code='2963' AND id!=$1", int(u["id"]))
    resp = RedirectResponse("/", status_code=302)
    set_session(resp, {"user_id": int(u["id"]), "username": u["username"], "avatar": avatar})
    try: await log_login(u["username"], int(u["id"]), avatar or "")
    except Exception: pass
    return resp

@app.get("/auth/logout")
async def auth_logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("session", path="/")
    return resp

@app.post("/auth/code")
async def auth_by_code(request: Request):
    body = await request.json()
    code = str(body.get("code","")).strip()
    if not code: raise HTTPException(400, "No code provided")
    pool = await db.get_pool()
    user = await db.get_user_by_code(pool, code)
    if not user: raise HTTPException(401, "Invalid code")
    if user["is_banned"]: raise HTTPException(403, "Your account has been banned")
    if user["timeout_until"] and user["timeout_until"] > datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(403, f"Timed out until {user['timeout_until'].strftime('%Y-%m-%d %H:%M UTC')}")
    resp = JSONResponse({"success": True, "username": user["username"]})
    set_session(resp, {"user_id": user["id"], "username": user["username"], "avatar": user["avatar"]})
    try: await log_login(user["username"], user["id"], user["avatar"] or "")
    except Exception: pass
    return resp

# ─── PUBLIC API ───────────────────────────────────────────────────────────────

@app.get("/api/me")
async def api_me(request: Request):
    s = get_session(request)
    if not s: return JSONResponse({"logged_in": False})
    pool = await db.get_pool()
    me = await db.get_me_data(pool, s["user_id"])
    return JSONResponse({"logged_in": True, "user_id": s["user_id"], "username": s["username"],
                         "avatar": s["avatar"], "inventory_value": me["inventory_value"],
                         "inventory_value_fmt": db.format_value(me["inventory_value"]),
                         "sabcoins": me["sabcoins"], "sabcoins_fmt": db.format_value(me["sabcoins"]),
                         "login_code": me["login_code"]})

@app.get("/api/inventory")
async def api_inventory(request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    items = await db.get_inventory(pool, s["user_id"])
    return JSONResponse([{"id": i["id"], "name": i["name"], "base_value": float(i["base_value"]),
                          "tier": i["tier"], "emoji": i["emoji"], "image_url": i["image_url"],
                          "mutation": i["mutation"], "multiplier": float(i["multiplier"]),
                          "traits": i["traits"], "in_use": i["in_use"], "value": float(i["value"])}
                         for i in items])

@app.get("/api/botstock")
async def api_botstock():
    pool = await db.get_pool()
    stock = await db.get_bot_stock(pool)
    return JSONResponse([{"id": s["id"], "name": s["name"], "base_value": float(s["base_value"]),
                          "tier": s["tier"], "emoji": s["emoji"], "image_url": s["image_url"],
                          "mutation": s["mutation"], "multiplier": float(s["multiplier"]),
                          "traits": s["traits"], "value": float(s["value"])}
                         for s in stock])

# ─── COINFLIP — MULTI-ITEM ────────────────────────────────────────────────────

@app.get("/api/coinflips")
async def api_coinflips():
    """Return all open coinflips with their item lists."""
    pool = await db.get_pool()
    games = await pool.fetch("""
        SELECT g.id, g.creator_id, g.creator_side, g.creator_total_value, g.status,
               u.username AS creator_name, u.avatar AS creator_avatar,
               g.created_at
        FROM coinflip_games g
        JOIN users u ON g.creator_id = u.id
        WHERE g.status = 'open'
        ORDER BY g.creator_total_value DESC
    """)
    result = []
    for g in games:
        # Fetch items for this game
        items = await pool.fetch("""
            SELECT ci.inventory_id, ci.side,
                   b.name, b.emoji, b.tier, b.image_url,
                   m.name AS mutation, m.multiplier, i.traits,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
            FROM coinflip_items ci
            JOIN inventory i ON ci.inventory_id = i.id
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE ci.game_id = $1
        """, g["id"])
        result.append({
            "id":               g["id"],
            "creator_id":       g["creator_id"],
            "creator_name":     g["creator_name"],
            "creator_avatar":   g["creator_avatar"],
            "creator_side":     g["creator_side"],
            "creator_total_value": float(g["creator_total_value"] or 0),
            "items": [{
                "inventory_id": i["inventory_id"],
                "side":         i["side"],
                "name":         i["name"],
                "emoji":        i["emoji"],
                "tier":         i["tier"],
                "image_url":    i["image_url"],
                "mutation":     i["mutation"],
                "value":        float(i["value"]),
                "traits":       i["traits"],
            } for i in items],
        })
    return JSONResponse(result, headers={"Cache-Control": "public, max-age=3"})

@app.post("/api/coinflip/create")
async def api_create_coinflip(request: Request):
    """
    Create a multi-item coinflip.
    Body: { side: 'fire'|'ice', inventory_ids: [1,2,3], vs_bot: false }
    """
    s    = await require_user(request)
    body = await request.json()
    side         = body.get("side")
    inventory_ids = body.get("inventory_ids", [])
    vs_bot       = body.get("vs_bot", False)

    if side not in ("fire","ice"):
        raise HTTPException(400, "Invalid side — must be 'fire' or 'ice'")
    if not inventory_ids or not isinstance(inventory_ids, list):
        raise HTTPException(400, "inventory_ids must be a non-empty list")
    if len(inventory_ids) > 10:
        raise HTTPException(400, "Maximum 10 items per coinflip")

    pool = await db.get_pool()

    # Verify all items belong to user and compute total value
    total_value = 0.0
    item_rows = []
    for inv_id in inventory_ids:
        row = await pool.fetchrow("""
            SELECT i.id, i.in_use,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value,
                   b.name
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE i.id=$1 AND i.user_id=$2
        """, int(inv_id), s["user_id"])
        if not row:
            raise HTTPException(404, f"Item {inv_id} not found or not yours")
        if row["in_use"]:
            raise HTTPException(400, f"Item {row['name']} is already in a game")
        item_rows.append(row)
        total_value += float(row["value"])

    # Atomic: claim all items
    for row in item_rows:
        claimed = await pool.fetchval(
            "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", row["id"]
        )
        if not claimed:
            # Roll back already-claimed items
            for r2 in item_rows:
                await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", r2["id"])
            raise HTTPException(400, f"Item {row['name']} was just claimed by another game")

    if vs_bot:
        # Bot matches total value ±15%
        bot_items = await pool.fetch("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value,
                   b.name, b.emoji
            FROM bot_stock s
            JOIN brainrots b ON s.brainrot_id = b.id
            JOIN mutations m ON s.mutation_id = m.id
            WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
                  BETWEEN $1 * 0.85 AND $1 * 1.15
            ORDER BY RANDOM() LIMIT 5
        """, total_value)
        if not bot_items:
            # Fallback: pick items that sum closest to user value
            bot_items = await pool.fetch("""
                SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                       ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value,
                       b.name, b.emoji
                FROM bot_stock s
                JOIN brainrots b ON s.brainrot_id = b.id
                JOIN mutations m ON s.mutation_id = m.id
                ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
                LIMIT 1
            """, total_value)
        if not bot_items:
            for row in item_rows:
                await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", row["id"])
            raise HTTPException(400, "Bot stock is empty or has nothing in your item's value range (±15%) — add items to bot stock first")

        bot_total = sum(float(b["value"]) for b in bot_items)

        # Create game
        game_id = await pool.fetchval("""
            INSERT INTO coinflip_games (creator_id, creator_side, creator_total_value)
            VALUES ($1, $2, $3) RETURNING id
        """, s["user_id"], side, total_value)

        # Insert creator items
        for row in item_rows:
            await pool.execute(
                "INSERT INTO coinflip_items (game_id, inventory_id, owner_id, side) VALUES ($1,$2,$3,'creator')",
                game_id, row["id"], s["user_id"]
            )

        won = random.random() < 0.475  # 47.5% player win rate

        async with pool.acquire() as conn:
            async with conn.transaction():
                if won:
                    # Give bot items to player
                    for bi in bot_items:
                        await conn.execute(
                            "INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits) VALUES ($1,$2,$3,$4)",
                            s["user_id"], bi["brainrot_id"], bi["mutation_id"], bi["traits"]
                        )
                        await conn.execute("DELETE FROM bot_stock WHERE id=$1", bi["id"])
                # Always delete player's wagered items
                for row in item_rows:
                    await conn.execute("DELETE FROM inventory WHERE id=$1", row["id"])
                await conn.execute(
                    "UPDATE coinflip_games SET status='completed', completed_at=NOW(), "
                    "joiner_total_value=$2 WHERE id=$1", game_id, bot_total
                )

        await db.record_game_result(pool, s["user_id"], won, total_value, bot_total if won else 0.0)

        creator_items_str = ", ".join(r["name"] for r in item_rows)
        bot_items_str     = ", ".join(b["name"] for b in bot_items)
        try:
            caller = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
            await log_callbot(caller["username"] if caller else str(s["user_id"]),
                              s["user_id"], caller["avatar"] if caller else "",
                              creator_items_str, total_value, bot_items_str, bot_total, won)
        except Exception: pass

        return JSONResponse({
            "game_id":   game_id,
            "you_won":   won,
            "bot_items": [{"name": b["name"], "emoji": b["emoji"], "value": float(b["value"])} for b in bot_items],
            "bot_total": bot_total,
        })

    # PvP — create open game
    game_id = await pool.fetchval("""
        INSERT INTO coinflip_games (creator_id, creator_side, creator_total_value)
        VALUES ($1, $2, $3) RETURNING id
    """, s["user_id"], side, total_value)
    for row in item_rows:
        await pool.execute(
            "INSERT INTO coinflip_items (game_id, inventory_id, owner_id, side) VALUES ($1,$2,$3,'creator')",
            game_id, row["id"], s["user_id"]
        )
    return JSONResponse({"game_id": game_id, "total_value": total_value})

@app.post("/api/coinflip/join/{game_id}")
async def api_join_coinflip(game_id: int, request: Request):
    """
    Join an open coinflip with items whose total value is within ±15% of the creator's pot.
    Body: { inventory_ids: [1,2,3] }
    """
    s    = await require_user(request)
    body = await request.json()
    inventory_ids = body.get("inventory_ids", [])

    if not inventory_ids:
        raise HTTPException(400, "inventory_ids required")
    if len(inventory_ids) > 10:
        raise HTTPException(400, "Maximum 10 items")

    pool = await db.get_pool()

    # Atomically claim the game
    game = await pool.fetchrow("""
        UPDATE coinflip_games SET status='processing', joiner_id=$2
        WHERE id=$1 AND status='open'
        RETURNING *
    """, game_id, s["user_id"])
    if not game:
        raise HTTPException(404, "Game not found or already taken")
    if game["creator_id"] == s["user_id"]:
        await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400, "Cannot join your own game")

    creator_total = float(game["creator_total_value"] or 0)

    # Validate joiner items
    joiner_total = 0.0
    joiner_rows  = []
    for inv_id in inventory_ids:
        row = await pool.fetchrow("""
            SELECT i.id, i.in_use,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value,
                   b.name
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id = b.id
            JOIN mutations m ON i.mutation_id = m.id
            WHERE i.id=$1 AND i.user_id=$2
        """, int(inv_id), s["user_id"])
        if not row:
            await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
            raise HTTPException(404, f"Item {inv_id} not found or not yours")
        if row["in_use"]:
            await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
            raise HTTPException(400, f"Item {row['name']} is already in a game")
        joiner_rows.append(row)
        joiner_total += float(row["value"])

    # Enforce ±15% value range
    lo = creator_total * 0.85
    hi = creator_total * 1.15
    if not (lo <= joiner_total <= hi):
        await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400,
            f"Your items total ⬡{db.format_value(joiner_total)} — must be within ±15% of "
            f"⬡{db.format_value(creator_total)} (⬡{db.format_value(lo)} – ⬡{db.format_value(hi)})")

    # Claim joiner items atomically
    for row in joiner_rows:
        claimed = await pool.fetchval(
            "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", row["id"]
        )
        if not claimed:
            # Roll back
            for r2 in joiner_rows: await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", r2["id"])
            await pool.execute("UPDATE coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
            raise HTTPException(400, f"Item {row['name']} just got claimed")

    # Insert joiner items
    for row in joiner_rows:
        await pool.execute(
            "INSERT INTO coinflip_items (game_id, inventory_id, owner_id, side) VALUES ($1,$2,$3,'joiner')",
            game_id, row["id"], s["user_id"]
        )

    # Decide winner — pure 50/50
    winner_id = random.choice([game["creator_id"], s["user_id"]])
    loser_id  = game["creator_id"] if winner_id == s["user_id"] else s["user_id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Transfer ALL items to winner
            await conn.execute(
                "UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id = ANY("
                "    SELECT inventory_id FROM coinflip_items WHERE game_id=$2"
                ")", winner_id, game_id
            )
            await conn.execute("""
                UPDATE coinflip_games
                SET winner_id=$2, status='completed', completed_at=NOW(), joiner_total_value=$3
                WHERE id=$1
            """, game_id, winner_id, joiner_total)
            await conn.execute("UPDATE users SET total_games=total_games+1 WHERE id=ANY($1)",
                               [game["creator_id"], s["user_id"]])
            await conn.execute("UPDATE users SET total_wins=total_wins+1 WHERE id=$1", winner_id)
            pot = creator_total + joiner_total
            await conn.execute("""
                UPDATE users SET current_streak=current_streak+1,
                    best_streak=GREATEST(best_streak,current_streak+1),
                    total_wagered=total_wagered+$2, total_won=total_won+$2
                WHERE id=$1
            """, winner_id, pot)
            await conn.execute("""
                UPDATE users SET current_streak=0, total_wagered=total_wagered+$2 WHERE id=$1
            """, loser_id, pot)

    try:
        creator_row = await pool.fetchrow("SELECT username FROM users WHERE id=$1", game["creator_id"])
        joiner_row  = await pool.fetchrow("SELECT username FROM users WHERE id=$1", s["user_id"])
        c_name = creator_row["username"] if creator_row else str(game["creator_id"])
        j_name = joiner_row["username"]  if joiner_row  else str(s["user_id"])
        creator_items_str = ", ".join(r["name"] for r in await pool.fetch("""
            SELECT b.name FROM coinflip_items ci
            JOIN inventory i ON ci.inventory_id=i.id
            JOIN brainrots b ON i.brainrot_id=b.id
            WHERE ci.game_id=$1 AND ci.side='creator'
        """, game_id))
        joiner_items_str = ", ".join(r["name"] for r in joiner_rows)
        await log_coinflip(c_name, game["creator_id"], j_name, s["user_id"],
                           creator_items_str, creator_total,
                           joiner_items_str, joiner_total,
                           winner_id, game["creator_side"])
    except Exception: pass

    return JSONResponse({
        "winner_id":    winner_id,
        "you_won":      winner_id == s["user_id"],
        "creator_total": creator_total,
        "joiner_total":  joiner_total,
    })

@app.post("/api/coinflip/cancel/{game_id}")
async def api_cancel_coinflip(game_id: int, request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    game = await pool.fetchrow(
        "SELECT * FROM coinflip_games WHERE id=$1 AND creator_id=$2 AND status='open'",
        game_id, s["user_id"]
    )
    if not game:
        raise HTTPException(404, "Game not found or not yours")
    await pool.execute("UPDATE coinflip_games SET status='cancelled' WHERE id=$1", game_id)
    await pool.execute("""
        UPDATE inventory SET in_use=FALSE
        WHERE id IN (SELECT inventory_id FROM coinflip_items WHERE game_id=$1)
    """, game_id)
    return JSONResponse({"success": True})

# ─── UPGRADE — MULTI-ITEM ────────────────────────────────────────────────────

@app.post("/api/upgrade")
async def api_upgrade(request: Request):
    """
    Multi-item upgrade.
    Body: { inventory_ids: [1,2], stock_ids: [5,6] }
    Win chance = (sum_offered / sum_target) * 95 * 0.95, capped at 90%
    Items are NOT removed on loss — only locked while game is in progress.
    On win: target items moved to inventory, offered items deleted.
    On loss: offered items returned (in_use=FALSE), nothing else changes.
    """
    s    = await require_user(request)
    body = await request.json()

    inventory_ids = body.get("inventory_ids", [])
    stock_ids     = body.get("stock_ids", [])

    if not inventory_ids: raise HTTPException(400, "inventory_ids required")
    if not stock_ids:     raise HTTPException(400, "stock_ids required")
    if len(inventory_ids) > 10: raise HTTPException(400, "Max 10 offered items")
    if len(stock_ids)     > 10: raise HTTPException(400, "Max 10 target items")

    pool = await db.get_pool()

    # Validate offered items
    offered_total = 0.0
    offered_rows  = []
    for inv_id in inventory_ids:
        row = await pool.fetchrow("""
            SELECT i.id, i.in_use, i.brainrot_id, i.mutation_id, i.traits,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value,
                   b.name
            FROM inventory i
            JOIN brainrots b ON i.brainrot_id=b.id
            JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1 AND i.user_id=$2
        """, int(inv_id), s["user_id"])
        if not row: raise HTTPException(404, f"Item {inv_id} not found or not yours")
        if row["in_use"]: raise HTTPException(400, f"Item {row['name']} is already wagered")
        offered_rows.append(row)
        offered_total += float(row["value"])

    # Validate target stock items
    target_total = 0.0
    target_rows  = []
    for st_id in stock_ids:
        row = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value,
                   b.name
            FROM bot_stock s
            JOIN brainrots b ON s.brainrot_id=b.id
            JOIN mutations m ON s.mutation_id=m.id
            WHERE s.id=$1
        """, int(st_id))
        if not row: raise HTTPException(404, f"Stock item {st_id} not found")
        target_rows.append(row)
        target_total += float(row["value"])

    if target_total < offered_total * 1.25:
        raise HTTPException(400, f"Target total (⬡{db.format_value(target_total)}) must be "
                                 f"at least 1.25× offered total (⬡{db.format_value(offered_total)})")

    # Claim offered items atomically
    for row in offered_rows:
        claimed = await pool.fetchval(
            "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", row["id"]
        )
        if not claimed:
            for r2 in offered_rows: await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", r2["id"])
            raise HTTPException(400, f"Item {row['name']} was just claimed")

    raw_chance = min((offered_total / target_total) * 100, 95)
    win_chance = raw_chance * 0.95
    roll       = round(random.uniform(0, 100), 2)
    won        = roll <= win_chance

    # Record game
    game_id = await pool.fetchval("""
        INSERT INTO upgrade_games (user_id, offered_inventory_id, target_bot_stock_id, win_chance, roll, won)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
    """, s["user_id"],
        offered_rows[0]["id"],  # keep first for backward compat FK
        target_rows[0]["id"],
        win_chance, roll, won)

    # Record all offered/target items
    for row in offered_rows:
        try:
            await pool.execute(
                "INSERT INTO upgrade_items (game_id, inventory_id, side) VALUES ($1,$2,'offered')",
                game_id, row["id"]
            )
        except Exception: pass
    for row in target_rows:
        try:
            await pool.execute(
                "INSERT INTO upgrade_items (game_id, stock_id, side) VALUES ($1,$2,'target')",
                game_id, row["id"]
            )
        except Exception: pass

    if won:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Lock all target stock items
                for row in target_rows:
                    locked = await conn.fetchval("SELECT id FROM bot_stock WHERE id=$1 FOR UPDATE", row["id"])
                    if not locked:
                        # Release offered items and abort
                        for r2 in offered_rows:
                            await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", r2["id"])
                        raise HTTPException(404, f"Stock item {row['name']} no longer available")
                # Give target items to user
                for row in target_rows:
                    await conn.execute(
                        "INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits) VALUES ($1,$2,$3,$4)",
                        s["user_id"], row["brainrot_id"], row["mutation_id"], row["traits"]
                    )
                    await conn.execute("DELETE FROM bot_stock WHERE id=$1", row["id"])
                # Delete offered items (win = sacrifice offered, gain target)
                for row in offered_rows:
                    await conn.execute("DELETE FROM inventory WHERE id=$1", row["id"])
                await conn.execute("""
                    UPDATE users SET total_games=total_games+1, total_wins=total_wins+1,
                        current_streak=current_streak+1,
                        best_streak=GREATEST(best_streak,current_streak+1),
                        total_wagered=total_wagered+$2, total_won=total_won+$3
                    WHERE id=$1
                """, s["user_id"], offered_total, target_total)
    else:
        # LOSS: release items back, do NOT delete them
        for row in offered_rows:
            await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", row["id"])
        await pool.execute("""
            UPDATE users SET total_games=total_games+1, current_streak=0,
                total_wagered=total_wagered+$2 WHERE id=$1
        """, s["user_id"], offered_total)

    try:
        player = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        offered_str = ", ".join(r["name"] for r in offered_rows)
        target_str  = ", ".join(r["name"] for r in target_rows)
        await log_upgrade(player["username"] if player else str(s["user_id"]), s["user_id"],
                          player["avatar"] if player else "",
                          offered_str, offered_total, target_str, target_total,
                          win_chance, roll, won)
    except Exception: pass

    return JSONResponse({
        "won":        won,
        "roll":       roll,
        "win_chance": round(win_chance, 2),
        "offered_total": offered_total,
        "target_total":  target_total,
    })

# ─── LEADERBOARD & PROFILE ───────────────────────────────────────────────────

@app.get("/api/leaderboard")
async def api_leaderboard(response: Response):
    response.headers["Cache-Control"] = "public, max-age=30"
    pool = await db.get_pool()
    rows = await db.get_leaderboard(pool, 20)
    return JSONResponse([{
        "username":      r["username"], "avatar": r["avatar"],
        "net_worth":     float(r["net_worth"]),
        "net_worth_fmt": db.format_value(float(r["net_worth"])),
        "total_games":   r["total_games"], "total_wins": r["total_wins"],
        "win_rate":      round(r["total_wins"]/r["total_games"]*100) if r["total_games"] > 0 else 0,
    } for r in rows])

@app.get("/api/profile/{user_id}")
async def api_profile(user_id: int):
    pool = await db.get_pool()
    u = await db.get_profile(pool, user_id)
    if not u: raise HTTPException(404, "User not found")
    wr = round(u["total_wins"]/u["total_games"]*100) if u["total_games"] > 0 else 0
    return JSONResponse({
        "username": u["username"], "avatar": u["avatar"],
        "net_worth": float(u["net_worth"]), "net_worth_fmt": db.format_value(float(u["net_worth"])),
        "total_games": u["total_games"], "total_wins": u["total_wins"], "win_rate": wr,
        "current_streak": u["current_streak"] or 0, "best_streak": u["best_streak"] or 0,
        "total_wagered": float(u["total_wagered"] or 0), "total_won": float(u["total_won"] or 0),
        "sabcoins": float(u["sabcoins"] or 0),
    })

# ─── TIPPING ──────────────────────────────────────────────────────────────────

@app.get("/api/users")
async def api_users():
    """Return all users for tip target dropdown."""
    pool = await db.get_pool()
    rows = await pool.fetch("SELECT id, username, avatar FROM users ORDER BY username ASC")
    return JSONResponse([{"id": r["id"], "username": r["username"], "avatar": r["avatar"]} for r in rows])

@app.post("/api/tip")
async def api_tip(request: Request):
    s    = await require_user(request)
    body = await request.json()
    to_id  = body.get("to_user_id")
    inv_id = body.get("inventory_id")
    if not to_id or not inv_id: raise HTTPException(400, "Missing fields")
    pool   = await db.get_pool()
    result = await db.send_tip(pool, s["user_id"], int(to_id), int(inv_id))
    if not result["success"]: raise HTTPException(400, result["reason"])
    try:
        item     = await pool.fetchrow("""
            SELECT b.name, m.name AS mutation,
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
            FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1
        """, int(inv_id))
        from_row = await pool.fetchrow("SELECT username FROM users WHERE id=$1", s["user_id"])
        to_row   = await pool.fetchrow("SELECT username FROM users WHERE id=$1", int(to_id))
        if item and from_row and to_row:
            await log_tip(from_row["username"], s["user_id"], to_row["username"], int(to_id),
                          item["name"], float(item["value"]), item["mutation"])
    except Exception: pass
    return JSONResponse({"success": True})

# ─── PROMO ────────────────────────────────────────────────────────────────────

@app.post("/api/promo/redeem")
async def api_redeem_promo(request: Request):
    s    = await require_user(request)
    body = await request.json()
    code = body.get("code","").strip()
    if not code: raise HTTPException(400, "No code provided")
    pool   = await db.get_pool()
    result = await db.redeem_promo(pool, code, s["user_id"])
    if not result["success"]: raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.get("/api/promo/my")
async def api_my_promos(request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT p.code, p.max_redeems, p.redeems, p.active,
               b.name AS item_name, b.emoji, m.name AS mutation_name,
               ROUND(b.base_value * m.multiplier * (1 + p.traits * 0.07), 2) AS value
        FROM promo_codes p
        JOIN brainrots b ON p.brainrot_id=b.id JOIN mutations m ON p.mutation_id=m.id
        JOIN promo_redemptions r ON r.code_id=p.id
        WHERE r.user_id=$1 ORDER BY r.redeemed_at DESC
    """, s["user_id"])
    return JSONResponse([{
        "code": r["code"], "max_redeems": r["max_redeems"], "redeems": r["redeems"],
        "active": r["active"], "item_name": r["item_name"], "emoji": r["emoji"],
        "mutation_name": r["mutation_name"], "value": float(r["value"]),
    } for r in rows])

# ─── SABCOIN ──────────────────────────────────────────────────────────────────

OXAPAY_MERCHANT = os.environ.get("OXAPAY_MERCHANT","")
OXAPAY_API      = "https://api.oxapay.com"
COINS_PER_USD   = 10

@app.post("/api/sabcoin/deposit")
async def api_sabcoin_deposit(request: Request):
    s    = await require_user(request)
    body = await request.json()
    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 1: raise HTTPException(400, "Minimum deposit is $1")
    if not OXAPAY_MERCHANT: raise HTTPException(500, "Payment processor not configured")
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
    if resp.status_code != 200: raise HTTPException(502, "Payment processor error")
    data = resp.json()
    if data.get("result") != 100: raise HTTPException(502, data.get("message","Payment error"))
    pool = await db.get_pool()
    await db.create_deposit(pool, s["user_id"], order_id, data.get("payAddress", order_id), amount_usd, coins)
    return JSONResponse({"order_id": order_id, "pay_link": data.get("payLink") or data.get("url",""),
                         "track_id": data.get("trackId",""), "coins": coins})

@app.post("/api/sabcoin/webhook")
async def oxapay_webhook(request: Request):
    body          = await request.json()
    order_id      = body.get("orderId","")
    status        = body.get("status","")
    confirmations = int(body.get("confirmations", 0))
    if not order_id: return JSONResponse({"ok": True})
    pool = await db.get_pool()
    dep  = await db.get_deposit(pool, order_id)
    if not dep or dep["status"] == "credited": return JSONResponse({"ok": True})
    await pool.execute("UPDATE sabcoin_deposits SET confirmations=$2 WHERE order_id=$1", order_id, confirmations)
    if confirmations >= 3 and status in ("Confirming","Paid","Completed"):
        result = await db.confirm_deposit(pool, order_id)
        if result["success"]:
            try:
                user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", result["user_id"])
                uname = user["username"] if user else str(result["user_id"])
                import discord
                await post_to_log("🔐login", discord.Embed(
                    title="💰 SabCoin Deposit",
                    description=f"**{uname}** deposited **{result['coins']} SC** (${dep['amount_usd']})",
                    color=0x22c55e))
            except Exception: pass
    return JSONResponse({"ok": True})

@app.get("/api/sabcoin/balance")
async def api_sabcoin_balance(request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    coins = await db.get_sabcoins(pool, s["user_id"])
    return JSONResponse({"coins": coins, "coins_fmt": db.format_value(coins)})

@app.post("/api/sabcoin/withdraw")
async def api_sabcoin_withdraw(request: Request):
    s    = await require_user(request)
    body = await request.json()
    amount_coins = float(body.get("amount_coins", 0))
    currency     = body.get("currency","LTC")
    address      = body.get("address","").strip()
    if amount_coins < 10: raise HTTPException(400, "Minimum withdrawal is 10 SC")
    if not address:       raise HTTPException(400, "Wallet address required")
    TAX              = 0.10
    amount_after_tax = round(amount_coins * (1 - TAX), 2)
    tax_burned       = round(amount_coins * TAX, 2)
    order_id         = secrets.token_hex(16)
    pool = await db.get_pool()
    # Check balance first before creating order
    balance = await db.get_sabcoins(pool, s["user_id"])
    if balance < amount_coins:
        raise HTTPException(400, f"Insufficient SabCoins — you have ⬡{db.format_value(balance)}")
    wid = await db.create_withdrawal(pool, s["user_id"], amount_coins,
                                     amount_after_tax, tax_burned, currency, address, order_id)
    if not wid: raise HTTPException(400, "Insufficient SabCoins")
    return JSONResponse({"success": True, "order_id": order_id,
                         "amount_after_tax": amount_after_tax, "tax": tax_burned})

# ─── SC COINFLIP ──────────────────────────────────────────────────────────────

@app.get("/api/sc_coinflips")
async def api_sc_coinflips():
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT g.id, g.creator_id, g.creator_side, g.amount,
               u.username AS creator_name, u.avatar AS creator_avatar
        FROM sc_coinflip_games g JOIN users u ON g.creator_id=u.id
        WHERE g.status='open' ORDER BY g.created_at DESC
    """)
    return JSONResponse([{
        "id": r["id"], "creator_id": r["creator_id"], "creator_side": r["creator_side"],
        "amount": float(r["amount"]), "creator_name": r["creator_name"],
        "creator_avatar": r["creator_avatar"],
    } for r in rows], headers={"Cache-Control": "public, max-age=3"})

@app.post("/api/sc_coinflip/create")
async def api_sc_coinflip_create(request: Request):
    s    = await require_user(request)
    body = await request.json()
    amount = float(body.get("amount", 0))
    side   = body.get("side")
    if side not in ("fire","ice"): raise HTTPException(400, "Invalid side")
    if amount < 1: raise HTTPException(400, "Minimum bet is 1 SC")
    pool = await db.get_pool()
    ok   = await db.debit_sabcoins(pool, s["user_id"], amount)
    if not ok: raise HTTPException(400, "Insufficient SabCoins")
    game_id = await pool.fetchval(
        "INSERT INTO sc_coinflip_games (creator_id, creator_side, amount) VALUES ($1,$2,$3) RETURNING id",
        s["user_id"], side, amount)
    return JSONResponse({"game_id": game_id})

@app.post("/api/sc_coinflip/join/{game_id}")
async def api_sc_coinflip_join(game_id: int, request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    # Step 1: claim game atomically
    game = await pool.fetchrow("""
        UPDATE sc_coinflip_games SET joiner_id=$2, status='processing'
        WHERE id=$1 AND status='open' RETURNING *
    """, game_id, s["user_id"])
    if not game: raise HTTPException(404, "Game not found or already taken")
    if game["creator_id"] == s["user_id"]:
        await pool.execute("UPDATE sc_coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400, "Cannot join your own game")
    amount = float(game["amount"])
    # Step 2: debit joiner outside transaction to avoid deadlock
    ok = await db.debit_sabcoins(pool, s["user_id"], amount)
    if not ok:
        await pool.execute("UPDATE sc_coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
        raise HTTPException(400, "Insufficient SabCoins")
    # Step 3: resolve
    winner_id   = random.choice([game["creator_id"], s["user_id"]])
    house_cut   = round(amount * 2 * 0.05, 2)
    winner_gets = round(amount * 2 - house_cut, 2)
    await db.credit_sabcoins(pool, winner_id, winner_gets)
    await pool.execute("""
        UPDATE sc_coinflip_games SET winner_id=$2, status='completed', completed_at=NOW() WHERE id=$1
    """, game_id, winner_id)
    return JSONResponse({"winner_id": winner_id, "you_won": winner_id == s["user_id"],
                         "winner_gets": winner_gets})

@app.post("/api/sc_coinflip/cancel/{game_id}")
async def api_sc_coinflip_cancel(game_id: int, request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    game = await pool.fetchrow(
        "SELECT * FROM sc_coinflip_games WHERE id=$1 AND creator_id=$2 AND status='open'",
        game_id, s["user_id"])
    if not game: raise HTTPException(404, "Game not found or not yours")
    await pool.execute("UPDATE sc_coinflip_games SET status='cancelled' WHERE id=$1", game_id)
    await db.credit_sabcoins(pool, s["user_id"], float(game["amount"]))
    return JSONResponse({"success": True})

# ─── MARKETPLACE ──────────────────────────────────────────────────────────────

@app.get("/api/marketplace")
async def api_marketplace():
    pool     = await db.get_pool()
    listings = await db.get_listings(pool)
    return JSONResponse([{
        "id": l["id"], "seller_id": l["seller_id"], "seller_name": l["seller_name"],
        "seller_avatar": l["seller_avatar"], "price_coins": float(l["price_coins"]),
        "item_name": l["item_name"], "emoji": l["emoji"], "tier": l["tier"],
        "image_url": l["image_url"], "mutation": l["mutation"],
        "multiplier": float(l["multiplier"]), "traits": l["traits"],
        "item_value": float(l["item_value"]),
    } for l in listings])

@app.post("/api/marketplace/list")
async def api_marketplace_list(request: Request):
    s    = await require_user(request)
    body = await request.json()
    inventory_id = body.get("inventory_id")
    price_coins  = body.get("price_coins")
    if not inventory_id or price_coins is None:
        raise HTTPException(400, "Missing inventory_id or price_coins")
    pool = await db.get_pool()
    listing_id = await db.create_listing(pool, s["user_id"], int(inventory_id), float(price_coins))
    if not listing_id: raise HTTPException(400, "Item is already in use or listed")
    return JSONResponse({"success": True, "listing_id": listing_id})

@app.post("/api/marketplace/buy/{listing_id}")
async def api_marketplace_buy(listing_id: int, request: Request):
    s      = await require_user(request)
    pool   = await db.get_pool()
    result = await db.buy_listing(pool, listing_id, s["user_id"])
    if not result["success"]: raise HTTPException(400, result["reason"])
    return JSONResponse(result)

@app.post("/api/marketplace/cancel/{listing_id}")
async def api_marketplace_cancel(listing_id: int, request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    ok   = await db.cancel_listing(pool, listing_id, s["user_id"])
    if not ok: raise HTTPException(404, "Listing not found or not yours")
    return JSONResponse({"success": True})

# ─── CHAT ─────────────────────────────────────────────────────────────────────

@app.get("/api/chat/history")
async def api_chat_history():
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT m.id, m.user_id, u.username, u.avatar, m.message, m.created_at
        FROM chat_messages m JOIN users u ON m.user_id=u.id
        ORDER BY m.created_at DESC LIMIT 50
    """)
    return JSONResponse([{
        "id": r["id"], "user_id": r["user_id"], "username": r["username"],
        "avatar": r["avatar"], "message": r["message"],
        "created_at": r["created_at"].isoformat(),
    } for r in reversed(rows)])

@app.post("/api/chat/send")
async def api_chat_send(request: Request):
    s    = await require_user(request)
    body = await request.json()
    msg  = str(body.get("message","")).strip()
    if not msg:        raise HTTPException(400, "Message cannot be empty")
    if len(msg) > 500: raise HTTPException(400, "Message too long (max 500 chars)")
    pool = await db.get_pool()
    recent = await pool.fetchval(
        "SELECT COUNT(*) FROM chat_messages WHERE user_id=$1 AND created_at > NOW() - INTERVAL '1 second'",
        s["user_id"])
    if recent and recent >= 1: raise HTTPException(429, "Slow down!")
    row  = await pool.fetchrow(
        "INSERT INTO chat_messages (user_id, message) VALUES ($1,$2) RETURNING id, created_at",
        s["user_id"], msg)
    user = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
    return JSONResponse({"id": row["id"], "user_id": s["user_id"],
                         "username": user["username"] if user else s["username"],
                         "avatar":   user["avatar"]   if user else s.get("avatar"),
                         "message":  msg, "created_at": row["created_at"].isoformat()})

@app.delete("/api/chat/{message_id}")
async def api_chat_delete(message_id: int, request: Request):
    s    = await require_user(request)
    pool = await db.get_pool()
    row  = await pool.fetchrow("SELECT login_code, username FROM users WHERE id=$1", s["user_id"])
    is_admin = row and (row["login_code"] == ADMIN_USER_CODE or row["username"] == ".mody51777")
    if is_admin:
        deleted = await pool.fetchval("DELETE FROM chat_messages WHERE id=$1 RETURNING id", message_id)
    else:
        deleted = await pool.fetchval(
            "DELETE FROM chat_messages WHERE id=$1 AND user_id=$2 RETURNING id", message_id, s["user_id"])
    if not deleted: raise HTTPException(404, "Message not found or not yours")
    return JSONResponse({"success": True})

# ─── PAGE ROUTES ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    try:
        await require_admin(request)
    except HTTPException:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("admin.html", {"request": request})

# ════════════════════════════════════════════════════════════════════════════
# ADMIN API
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT u.id, u.username, u.avatar,
               COALESCE(u.sabcoins,0) AS sabcoins,
               u.is_banned, u.timeout_until, u.total_games,
               COALESCE(SUM(ROUND(b.base_value*m.multiplier*(1+i.traits*0.07),2)),0) AS net_worth
        FROM users u
        LEFT JOIN inventory i ON i.user_id=u.id
        LEFT JOIN brainrots b ON i.brainrot_id=b.id
        LEFT JOIN mutations m ON i.mutation_id=m.id
        GROUP BY u.id, u.username, u.avatar, u.sabcoins, u.is_banned, u.timeout_until, u.total_games
        ORDER BY u.username ASC
    """)
    return JSONResponse([{
        "id": r["id"], "username": r["username"], "avatar": r["avatar"],
        "sabcoins": float(r["sabcoins"]), "is_banned": r["is_banned"],
        "timeout_until": r["timeout_until"].isoformat() if r["timeout_until"] else None,
        "total_games": r["total_games"], "net_worth": float(r["net_worth"]),
    } for r in rows])

@app.get("/api/admin/inventory")
async def api_admin_inventory(request: Request, user_id: int):
    await require_admin(request)
    pool  = await db.get_pool()
    items = await pool.fetch("""
        SELECT i.id, b.name, b.emoji, b.tier, m.name AS mutation, m.multiplier,
               i.traits, i.in_use,
               ROUND(b.base_value*m.multiplier*(1+i.traits*0.07),2) AS value
        FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
        WHERE i.user_id=$1 ORDER BY value DESC
    """, user_id)
    return JSONResponse([{"id": i["id"], "name": i["name"], "emoji": i["emoji"], "tier": i["tier"],
                          "mutation": i["mutation"], "traits": i["traits"],
                          "in_use": i["in_use"], "value": float(i["value"])} for i in items])

@app.post("/api/admin/additem")
async def api_admin_additem(request: Request):
    await require_admin(request)
    body        = await request.json()
    user_id     = body.get("user_id")
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))
    if not user_id:     raise HTTPException(400, "Missing user_id")
    if not brainrot_id: raise HTTPException(400, "Missing brainrot_id")
    if not mutation_id: raise HTTPException(400, "Missing mutation_id")
    if not (0 <= traits <= 10): raise HTTPException(400, "Traits must be 0–10")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id, username FROM users WHERE id=$1", int(user_id))
    if not user: raise HTTPException(404, "User not found — they must log in first")
    row = await pool.fetchrow("""
        SELECT b.name AS bname, b.emoji, m.name AS mname,
               ROUND(b.base_value*m.multiplier*(1+$3*0.07),2) AS value
        FROM brainrots b, mutations m WHERE b.id=$1 AND m.id=$2
    """, int(brainrot_id), int(mutation_id), traits)
    if not row: raise HTTPException(404, "Invalid brainrot_id or mutation_id")
    inv_id = await db.add_item_to_inventory(pool, int(user_id), int(brainrot_id), int(mutation_id), traits)
    return JSONResponse({"success": True, "inv_id": inv_id, "item_name": row["bname"],
                         "emoji": row["emoji"], "mutation": row["mname"], "value": float(row["value"])})

@app.post("/api/admin/removeitem")
async def api_admin_removeitem(request: Request):
    await require_admin(request)
    body         = await request.json()
    inventory_id = body.get("inventory_id")
    if not inventory_id: raise HTTPException(400, "Missing inventory_id")
    pool = await db.get_pool()
    item = await pool.fetchrow("""
        SELECT i.id, b.name FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id WHERE i.id=$1
    """, int(inventory_id))
    if not item: raise HTTPException(404, "Item not found")
    await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", int(inventory_id))
    await db.remove_item_from_inventory(pool, int(inventory_id))
    return JSONResponse({"success": True, "item_name": item["name"]})

@app.post("/api/admin/addbotstock")
async def api_admin_addbotstock(request: Request):
    await require_admin(request)
    body        = await request.json()
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))
    if not brainrot_id: raise HTTPException(400, "Missing brainrot_id")
    if not mutation_id: raise HTTPException(400, "Missing mutation_id")
    if not (0 <= traits <= 10): raise HTTPException(400, "Traits must be 0–10")
    pool = await db.get_pool()
    row  = await pool.fetchrow("""
        SELECT b.name AS bname, b.emoji, m.name AS mname,
               ROUND(b.base_value*m.multiplier*(1+$3*0.07),2) AS value
        FROM brainrots b, mutations m WHERE b.id=$1 AND m.id=$2
    """, int(brainrot_id), int(mutation_id), traits)
    if not row: raise HTTPException(404, "Invalid brainrot_id or mutation_id")
    stock_id = await db.add_to_bot_stock(pool, int(brainrot_id), int(mutation_id), traits)
    return JSONResponse({"success": True, "stock_id": stock_id, "item_name": row["bname"],
                         "emoji": row["emoji"], "mutation": row["mname"], "value": float(row["value"])})

@app.post("/api/admin/removebotstock")
async def api_admin_removebotstock(request: Request):
    await require_admin(request)
    body     = await request.json()
    stock_id = body.get("stock_id")
    if not stock_id: raise HTTPException(400, "Missing stock_id")
    pool = await db.get_pool()
    item = await pool.fetchrow("""
        SELECT s.id, b.name FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id WHERE s.id=$1
    """, int(stock_id))
    if not item: raise HTTPException(404, "Stock item not found")
    await db.remove_from_bot_stock(pool, int(stock_id))
    return JSONResponse({"success": True, "item_name": item["name"]})

@app.post("/api/admin/addcoins")
async def api_admin_addcoins(request: Request):
    await require_admin(request)
    body    = await request.json()
    user_id = body.get("user_id")
    amount  = float(body.get("amount", 0))
    action  = body.get("action","add")
    if not user_id: raise HTTPException(400, "Missing user_id")
    if amount <= 0: raise HTTPException(400, "Amount must be positive")
    if action not in ("add","set","deduct"): raise HTTPException(400, "action must be add/set/deduct")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id, username, sabcoins FROM users WHERE id=$1", int(user_id))
    if not user: raise HTTPException(404, "User not found")
    if action == "add":
        await db.credit_sabcoins(pool, int(user_id), amount)
    elif action == "set":
        await pool.execute("UPDATE users SET sabcoins=$2 WHERE id=$1", int(user_id), amount)
    elif action == "deduct":
        current = float(user["sabcoins"] or 0)
        if amount > current: raise HTTPException(400, f"User only has ⬡{current}")
        await pool.execute("UPDATE users SET sabcoins=sabcoins-$2 WHERE id=$1", int(user_id), amount)
    new_balance = await db.get_sabcoins(pool, int(user_id))
    return JSONResponse({"success": True, "username": user["username"],
                         "action": action, "new_balance": new_balance})

@app.post("/api/admin/setban")
async def api_admin_setban(request: Request):
    s       = await require_admin(request)
    body    = await request.json()
    user_id = body.get("user_id")
    banned  = bool(body.get("banned", True))
    if not user_id: raise HTTPException(400, "Missing user_id")
    if int(user_id) == s["user_id"]: raise HTTPException(400, "Cannot ban yourself")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id, username FROM users WHERE id=$1", int(user_id))
    if not user: raise HTTPException(404, "User not found")
    await pool.execute("UPDATE users SET is_banned=$2 WHERE id=$1", int(user_id), banned)
    return JSONResponse({"success": True, "username": user["username"],
                         "action": "banned" if banned else "unbanned"})

@app.post("/api/admin/settimeout")
async def api_admin_settimeout(request: Request):
    await require_admin(request)
    body    = await request.json()
    user_id = body.get("user_id")
    hours   = float(body.get("hours", 0))
    remove  = bool(body.get("remove", False))
    if not user_id: raise HTTPException(400, "Missing user_id")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id, username FROM users WHERE id=$1", int(user_id))
    if not user: raise HTTPException(404, "User not found")
    if remove or hours <= 0:
        await pool.execute("UPDATE users SET timeout_until=NULL WHERE id=$1", int(user_id))
        return JSONResponse({"success": True, "username": user["username"],
                             "action": "timeout_removed", "timeout_until": None})
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    await pool.execute("UPDATE users SET timeout_until=$2 WHERE id=$1", int(user_id), until)
    return JSONResponse({"success": True, "username": user["username"],
                         "action": "timed_out", "timeout_until": until.isoformat()})

@app.get("/api/admin/brainrots")
async def api_admin_brainrots(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_brainrots(pool)
    return JSONResponse([{"id": r["id"], "name": r["name"], "emoji": r["emoji"],
                          "tier": r["tier"], "base_value": float(r["base_value"])} for r in rows])

@app.get("/api/admin/mutations")
async def api_admin_mutations(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_mutations(pool)
    return JSONResponse([{"id": r["id"], "name": r["name"], "multiplier": float(r["multiplier"])} for r in rows])

@app.get("/api/admin/promos")
async def api_admin_promos(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_promos(pool)
    return JSONResponse([{
        "code": r["code"], "item_name": r["item_name"], "emoji": r["emoji"],
        "mutation_name": r["mutation_name"], "value": float(r["value"]),
        "max_redeems": r["max_redeems"], "redeems": r["redeems"], "active": r["active"],
    } for r in rows])
# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL — routes to ADD to server.py
# Paste this entire block at the END of server.py (before any if __name__ block)
# ══════════════════════════════════════════════════════════════════════════════

# ─── ADMIN PANEL PAGE ────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    """Serve the admin panel HTML. Requires admin session."""
    await require_admin(request)
    import os as _os
    panel_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates", "admin.html")
    if not _os.path.exists(panel_path):
        raise HTTPException(404, "Admin panel template not found")
    with open(panel_path) as f:
        return HTMLResponse(f.read())


# ─── ADMIN API: LIST USERS ────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await pool.fetch("""
        SELECT u.id, u.username, u.avatar, u.total_games, u.total_wins,
               u.sabcoins, u.is_banned, u.login_code,
               COALESCE(SUM(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)), 0) AS net_worth
        FROM users u
        LEFT JOIN inventory i ON i.user_id = u.id
        LEFT JOIN brainrots b ON i.brainrot_id = b.id
        LEFT JOIN mutations m ON i.mutation_id = m.id
        GROUP BY u.id
        ORDER BY net_worth DESC
    """)
    return JSONResponse([{
        "id": r["id"],
        "username": r["username"],
        "avatar": r["avatar"],
        "total_games": r["total_games"],
        "total_wins": r["total_wins"],
        "sabcoins": float(r["sabcoins"] or 0),
        "is_banned": r["is_banned"],
        "login_code": r["login_code"],
        "net_worth": float(r["net_worth"]),
    } for r in rows])


# ─── ADMIN API: USER INVENTORY ────────────────────────────────────────────────

@app.get("/api/admin/inventory/{user_id}")
async def admin_user_inventory(user_id: int, request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    items = await pool.fetch("""
        SELECT i.id, b.name, b.emoji, b.tier, b.image_url,
               m.name AS mutation, m.multiplier, i.traits, i.in_use,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.user_id = $1
        ORDER BY value DESC
    """, user_id)
    return JSONResponse([{
        "id": i["id"], "name": i["name"], "emoji": i["emoji"], "tier": i["tier"],
        "image_url": i["image_url"], "mutation": i["mutation"],
        "multiplier": float(i["multiplier"]), "traits": i["traits"],
        "in_use": i["in_use"], "value": float(i["value"])
    } for i in items])


# ─── ADMIN API: BRAINROTS / MUTATIONS ────────────────────────────────────────

@app.get("/api/admin/brainrots")
async def admin_brainrots(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_brainrots(pool)
    return JSONResponse([{
        "id": r["id"], "name": r["name"], "base_value": float(r["base_value"]),
        "tier": r["tier"], "emoji": r["emoji"]
    } for r in rows])


@app.get("/api/admin/mutations")
async def admin_mutations(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_mutations(pool)
    return JSONResponse([{
        "id": r["id"], "name": r["name"], "multiplier": float(r["multiplier"])
    } for r in rows])


# ─── ADMIN API: ADD ITEM TO USER ─────────────────────────────────────────────

@app.post("/api/admin/additem")
async def admin_add_item(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id     = body.get("user_id")
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))

    if not all([user_id, brainrot_id, mutation_id]):
        raise HTTPException(400, "Missing fields")
    if traits < 0 or traits > 10:
        raise HTTPException(400, "Traits must be 0–10")

    pool = await db.get_pool()

    # Make sure user exists
    user = await pool.fetchrow("SELECT id FROM users WHERE id = $1", int(user_id))
    if not user:
        raise HTTPException(404, "User not found")

    inv_id = await db.add_item_to_inventory(pool, int(user_id), int(brainrot_id), int(mutation_id), traits)

    # Fetch item details for response
    row = await pool.fetchrow("""
        SELECT b.name, b.emoji, m.name AS mutation, m.multiplier,
               ROUND(b.base_value * m.multiplier * (1 + $3 * 0.07), 2) AS value
        FROM brainrots b, mutations m
        WHERE b.id = $1 AND m.id = $2
    """, int(brainrot_id), int(mutation_id), traits)

    return JSONResponse({
        "inventory_id": inv_id,
        "name": row["name"],
        "emoji": row["emoji"],
        "mutation": row["mutation"],
        "value": float(row["value"]),
    })


# ─── ADMIN API: ADD TO BOT STOCK ─────────────────────────────────────────────

@app.post("/api/admin/addbotstock")
async def admin_add_bot_stock(request: Request):
    await require_admin(request)
    body = await request.json()
    brainrot_id = body.get("brainrot_id")
    mutation_id = body.get("mutation_id")
    traits      = int(body.get("traits", 0))

    if not all([brainrot_id, mutation_id]):
        raise HTTPException(400, "Missing fields")

    pool = await db.get_pool()
    stock_id = await db.add_to_bot_stock(pool, int(brainrot_id), int(mutation_id), traits)

    row = await pool.fetchrow("""
        SELECT b.name, b.emoji, m.name AS mutation,
               ROUND(b.base_value * m.multiplier * (1 + $3 * 0.07), 2) AS value
        FROM brainrots b, mutations m
        WHERE b.id = $1 AND m.id = $2
    """, int(brainrot_id), int(mutation_id), traits)

    return JSONResponse({
        "stock_id": stock_id,
        "name": row["name"],
        "emoji": row["emoji"],
        "mutation": row["mutation"],
        "value": float(row["value"]),
    })


# ─── ADMIN API: REMOVE FROM BOT STOCK ────────────────────────────────────────

@app.delete("/api/admin/removebotstock/{stock_id}")
async def admin_remove_bot_stock(stock_id: int, request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    item = await pool.fetchrow("SELECT id FROM bot_stock WHERE id = $1", stock_id)
    if not item:
        raise HTTPException(404, "Stock item not found")
    await db.remove_from_bot_stock(pool, stock_id)
    return JSONResponse({"success": True})


# ─── ADMIN API: SC COINS ─────────────────────────────────────────────────────

@app.post("/api/admin/sccoins")
async def admin_sc_coins(request: Request):
    await require_admin(request)
    body   = await request.json()
    user_id = body.get("user_id")
    action  = body.get("action")   # "add" | "remove" | "set"
    amount  = float(body.get("amount", 0))

    if not user_id or action not in ("add", "remove", "set"):
        raise HTTPException(400, "Invalid request")
    if amount < 0:
        raise HTTPException(400, "Amount cannot be negative")

    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id FROM users WHERE id = $1", int(user_id))
    if not user:
        raise HTTPException(404, "User not found")

    if action == "add":
        await db.credit_sabcoins(pool, int(user_id), amount)
    elif action == "remove":
        success = await db.debit_sabcoins(pool, int(user_id), amount)
        if not success:
            bal = await db.get_sabcoins(pool, int(user_id))
            raise HTTPException(400, f"Insufficient balance ({db.format_value(bal)} SC)")
    elif action == "set":
        await pool.execute("UPDATE users SET sabcoins = $2 WHERE id = $1", int(user_id), amount)

    new_bal = await db.get_sabcoins(pool, int(user_id))
    return JSONResponse({"success": True, "new_balance": new_bal})


# ─── ADMIN API: CREATE COINFLIP (multi-item) ─────────────────────────────────
# BUG FIX: The original /api/coinflip/create only accepted ONE inventory_id.
# This admin endpoint accepts multiple inventory_ids, bundles their combined
# value, creates a SINGLE coinflip game referencing the FIRST item, and marks
# all selected items as in_use so they can't be double-wagered.
# The joiner takes everything when they win.

@app.post("/api/admin/createcoinflip")
async def admin_create_coinflip(request: Request):
    await require_admin(request)
    body          = await request.json()
    user_id       = body.get("user_id")
    inventory_ids = body.get("inventory_ids", [])
    side          = body.get("side")

    if side not in ("fire", "ice"):
        raise HTTPException(400, "Invalid side — must be 'fire' or 'ice'")
    if not inventory_ids:
        raise HTTPException(400, "No items selected")
    if not user_id:
        raise HTTPException(400, "No user specified")

    pool = await db.get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify all items belong to the user and are not in_use
            items = await conn.fetch("""
                SELECT i.id,
                       ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
                FROM inventory i
                JOIN brainrots b ON i.brainrot_id = b.id
                JOIN mutations m ON i.mutation_id = m.id
                WHERE i.id = ANY($1::int[])
                  AND i.user_id = $2
                  AND i.in_use = FALSE
            """, inventory_ids, int(user_id))

            if len(items) != len(inventory_ids):
                raise HTTPException(400, "Some items not found, not owned by this user, or already in use")

            total_value = sum(float(i["value"]) for i in items)
            primary_id  = inventory_ids[0]

            # Mark all selected items in_use atomically
            await conn.execute(
                "UPDATE inventory SET in_use = TRUE WHERE id = ANY($1::int[])",
                inventory_ids
            )

            # Create the coinflip game using the first item as the primary reference
            game_id = await conn.fetchval("""
                INSERT INTO coinflip_games (creator_id, creator_inventory_id, creator_side)
                VALUES ($1, $2, $3) RETURNING id
            """, int(user_id), primary_id, side)

    return JSONResponse({
        "game_id":     game_id,
        "total_value": total_value,
        "item_count":  len(inventory_ids),
        "side":        side,
    })
