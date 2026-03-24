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
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=500)

_pets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pets")
os.makedirs(_pets_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")

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
            ip = request.headers.get("x-forwarded-for","").split(",")[0].strip() or (request.client.host if request.client else "unknown")
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

SECRET_KEY = os.environ["SECRET_KEY"]
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
DISCORD_API = "https://discord.com/api/v10"

serializer = URLSafeTimedSerializer(SECRET_KEY)

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

async def require_admin(request):
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
    loser_name = joiner_name if winner_id == creator_id else creator_name
    embed = discord.Embed(
        title="Coinflip Ended",
        description=f"A **{db.format_value(total)} value** coinflip game has successfully been concluded!",
        color=0x6c5ce7
    )
    embed.add_field(name=f"Starter Items ({db.format_value(creator_val)})", value=f"```{creator_item} ({db.format_value(creator_val)})```", inline=False)
    embed.add_field(name=f"Joiner Items ({db.format_value(joiner_val)})", value=f"```{joiner_item} ({db.format_value(joiner_val)})```", inline=False)
    embed.add_field(name="Starter's Side", value=f"```{creator_side.capitalize()}```", inline=False)
    embed.add_field(name="Winner", value=f"```{winner_name}```", inline=False)
    embed.add_field(name="Loser", value=f"```{loser_name}```", inline=False)
    embed.add_field(name="Mode", value=f"```{mode}```", inline=False)
    await post_to_log("🪙coinflip", embed)

async def log_callbot(username, user_id, avatar, user_item, user_val, bot_item, bot_val, won):
    import discord
    embed = discord.Embed(
        title="Coinflip Ended",
        description=f"A **{db.format_value(user_val + bot_val)} value** coinflip game has successfully been concluded!",
        color=0x22c55e if won else 0xef4444
    )
    embed.add_field(name=f"Starter Items ({db.format_value(user_val)})", value=f"```{user_item} ({db.format_value(user_val)})```", inline=False)
    embed.add_field(name=f"Bot Items ({db.format_value(bot_val)})", value=f"```{bot_item} ({db.format_value(bot_val)})```", inline=False)
    embed.add_field(name="Winner", value=f"```{'Bot' if not won else username}```", inline=False)
    embed.add_field(name="Loser", value=f"```{username if not won else 'Bot'}```", inline=False)
    embed.add_field(name="Mode", value="```Call Bot```", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🪙coinflip", embed)

async def log_upgrade(username, user_id, avatar, offered_item, offered_val, target_item, target_val, win_chance, roll, won):
    import discord
    title = "Upgrade Won" if won else "Upgrade Lost"
    color = 0x22c55e if won else 0xef4444
    embed = discord.Embed(
        title=title,
        description=f"**{username}** {'won' if won else 'lost'} an upgrade. Chance **{win_chance:.2f}%**, roll **{roll:.2f}%**.",
        color=color
    )
    embed.add_field(name=f"Selected Items ({db.format_value(offered_val)})", value=f"```{offered_item} ({db.format_value(offered_val)})```", inline=False)
    embed.add_field(name=f"Desired Items ({db.format_value(target_val)})", value=f"```{target_item} ({db.format_value(target_val)})```", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("💥upgrader", embed)

async def log_tip(from_name, from_id, to_name, to_id, item_name, item_val, mutation):
    import discord
    display = f"{item_name} [{mutation}]" if mutation and mutation != "Base" else item_name
    embed = discord.Embed(title="Item Tipped", description=f"**{from_name}** tipped **{to_name}** an item.", color=0x22c55e)
    embed.add_field(name="Item", value=f"```{display} ({db.format_value(item_val)})```", inline=False)
    embed.add_field(name="From", value=f"```{from_name}```", inline=True)
    embed.add_field(name="To", value=f"```{to_name}```", inline=True)
    await post_to_log("🎁tipping", embed)

async def log_login(username, user_id, avatar):
    import discord
    embed = discord.Embed(title="User Login", description=f"**{username}** logged into SabPot.", color=0x6c5ce7)
    embed.add_field(name="User", value=f"```{username}```", inline=True)
    embed.add_field(name="Discord ID", value=f"```{user_id}```", inline=True)
    if avatar:
        embed.set_thumbnail(url=avatar)
    await post_to_log("🔐login", embed)

# ─── FIX: STARTUP — runs each SQL statement individually so one failure never kills the server ───

@app.on_event("startup")
async def startup():
    pool = await db.get_pool()

    # Run schema statement-by-statement — a single bad statement won't abort everything
    try:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path) as f:
            raw_sql = f.read()

        # Split on semicolons, skip empty/whitespace-only chunks
        # Note: we rejoin DO $$ ... $$ blocks which contain internal semicolons
        statements = []
        current = []
        in_dollar_block = False
        for line in raw_sql.splitlines():
            stripped = line.strip()
            if stripped.startswith('DO $$') or stripped.startswith("DO $"):
                in_dollar_block = True
            if in_dollar_block:
                current.append(line)
                if stripped == '$$;' or stripped == "END $$;":
                    in_dollar_block = False
                    statements.append('\n'.join(current))
                    current = []
            else:
                if ';' in line:
                    current.append(line)
                    statements.append('\n'.join(current))
                    current = []
                else:
                    current.append(line)

        async with pool.acquire() as conn:
            for stmt in statements:
                clean = stmt.strip()
                if not clean:
                    continue
                try:
                    await conn.execute(clean)
                except Exception as e:
                    # Log but never crash — most "errors" here are harmless (e.g. index already exists)
                    err_str = str(e)
                    if 'already exists' not in err_str and 'duplicate' not in err_str.lower():
                        print(f"⚠️ Schema stmt warning: {err_str[:120]}")

    except FileNotFoundError:
        print("⚠️ schema.sql not found — skipping schema run")
    except Exception as e:
        print(f"⚠️ Schema load error (non-fatal): {e}")

    # Cache Discord log channel IDs for this worker
    await _cache_log_channels()

    # Clean up any stuck state from previous crashes
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
        u = ur.json()

    pool = await db.get_pool()
    avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get('avatar') else None
    await db.ensure_user(pool, int(u["id"]), u["username"], avatar)

    # FIX: Admin code assignment — do this AFTER ensure_user, atomically
    if u.get("username") == ".mody51777":
        try:
            # Release 2963 from anyone else first, then assign to this user
            await pool.execute(
                "UPDATE users SET login_code = NULL WHERE login_code = '2963' AND id != $1",
                int(u["id"])
            )
            await pool.execute(
                "UPDATE users SET login_code = '2963' WHERE id = $1",
                int(u["id"])
            )
        except Exception:
            pass

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
        "user_id": user["id"],
        "username": user["username"],
        "avatar": user["avatar"]
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
    side = body.get("side")
    if side not in ("fire","ice"):
        raise HTTPException(400, "Invalid side")
    pool = await db.get_pool()
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
            # Fallback: closest within 3x range only
            bot_item = await pool.fetchrow("""
                SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                       ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
                FROM bot_stock s
                JOIN brainrots b ON s.brainrot_id = b.id
                JOIN mutations m ON s.mutation_id = m.id
                WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
                      BETWEEN $1 * 0.34 AND $1 * 3.0
                ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
                LIMIT 1
            """, float(item_value))
        if not bot_item:
            await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1", inv_id)
            raise HTTPException(400, f"No bot stock item found near your item value (⬡{db.format_value(float(item_value))}). Ask an admin to restock the bot.")
        game_id = await db.create_coinflip(pool, s["user_id"], inv_id, side)
        won = random.random() < 0.475
        vs_bot_item_name = await pool.fetchval(
            "SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]
        )
        await db.join_coinflip_bot(pool, game_id, bot_item["id"], s["user_id"] if won else None, s["user_id"])
        await pool.execute("DELETE FROM inventory WHERE id=$1", inv_id)
        won_val = float(bot_item["value"]) if won else 0.0
        await db.record_game_result(pool, s["user_id"], won, float(item_value), won_val)
        return JSONResponse({"game_id": game_id, "result": True, "you_won": won})
    game_id = await db.create_coinflip(pool, s["user_id"], inv_id, side)
    return JSONResponse({"game_id": game_id})

@app.post("/api/coinflip/join/{game_id}")
async def api_join_coinflip(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
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
            pot_val = float(item_val) if item_val else 0.0
            loser_id = game["creator_id"] if winner_id == s["user_id"] else s["user_id"]
            taxed = False
            if item_val:
                tax_val = float(item_val) * 0.15
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
            await conn.execute("UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id=$2", winner_id, game["creator_inventory_id"])
            await conn.execute("UPDATE coinflip_games SET winner_id=$2, status='completed', completed_at=NOW() WHERE id=$1", game_id, winner_id)
            await conn.execute("UPDATE users SET total_games=total_games+1 WHERE id=ANY($1)", [game["creator_id"], s["user_id"]])
            await conn.execute("UPDATE users SET total_wins=total_wins+1 WHERE id=$1", winner_id)
            await conn.execute("UPDATE users SET current_streak=current_streak+1, best_streak=GREATEST(best_streak,current_streak+1), total_wagered=total_wagered+$2, total_won=total_won+$2 WHERE id=$1", winner_id, pot_val)
            await conn.execute("UPDATE users SET current_streak=0, total_wagered=total_wagered+$2 WHERE id=$1", loser_id, pot_val)
    try:
        creator_user = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", game["creator_id"])
        joiner_user = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
        creator_name = creator_user["username"] if creator_user else str(game["creator_id"])
        joiner_name = joiner_user["username"] if joiner_user else str(s["user_id"])
        creator_item_row = await pool.fetchrow("""
            SELECT b.name, ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS val
            FROM inventory i JOIN brainrots b ON i.brainrot_id=b.id JOIN mutations m ON i.mutation_id=m.id
            WHERE i.id=$1
        """, game["creator_inventory_id"])
        c_item = creator_item_row["name"] if creator_item_row else "Unknown"
        c_val = float(creator_item_row["val"]) if creator_item_row else float(item_val or 0)
        await log_coinflip(creator_name, game["creator_id"], joiner_name, s["user_id"], c_item, c_val, c_item, 0.0, winner_id, game["creator_side"])
    except Exception:
        pass
    return JSONResponse({"winner_id": winner_id, "you_won": winner_id == s["user_id"], "taxed": taxed})

@app.post("/api/coinflip/callbot/{game_id}")
async def api_callbot(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
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
        # Rollback game status so it's not stuck
        await pool.execute("UPDATE coinflip_games SET status='open' WHERE id=$1", game_id)
        raise HTTPException(400, "Wagered item not found")

    wagered_val = float(wagered["value"])

    # Try ±15% range first
    bot_item = await pool.fetchrow("""
        SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
        WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
              BETWEEN $1 * 0.85 AND $1 * 1.15
        ORDER BY RANDOM() LIMIT 1
    """, wagered_val)

    if not bot_item:
        # Fallback: find closest item but only if it's within 3x the wagered value
        # (prevents a $1 item matching a $500 bot item)
        bot_item = await pool.fetchrow("""
            SELECT s.id, s.brainrot_id, s.mutation_id, s.traits,
                   ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
            FROM bot_stock s JOIN brainrots b ON s.brainrot_id=b.id JOIN mutations m ON s.mutation_id=m.id
            WHERE ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2)
                  BETWEEN $1 * 0.34 AND $1 * 3.0
            ORDER BY ABS(ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) - $1)
            LIMIT 1
        """, wagered_val)

    if not bot_item:
        # No item anywhere close — rollback and return clear error
        await pool.execute("UPDATE coinflip_games SET status='open' WHERE id=$1", game_id)
        raise HTTPException(400, f"No bot stock item found near your item value (⬡{db.format_value(wagered_val)}). Ask an admin to restock the bot.")

    won = random.random() < 0.475
    TAX_RATE = 0.15
    taxed = False
    if won:
        tax_val = wagered_val * TAX_RATE
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
        if tax_item and float(tax_item["val"]) <= wagered_val:
            await pool.execute("INSERT INTO bot_stock (brainrot_id, mutation_id, traits) VALUES ($1, $2, $3)", tax_item["brainrot_id"], tax_item["mutation_id"], tax_item["traits"])
            await pool.execute("DELETE FROM inventory WHERE id=$1", tax_item["id"])
            taxed = True
    caller = await pool.fetchrow("SELECT username, avatar FROM users WHERE id=$1", s["user_id"])
    caller_name = caller["username"] if caller else str(s["user_id"])
    caller_av = caller["avatar"] if caller else ""
    user_item_name = await pool.fetchval("SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1", game["creator_inventory_id"]) or "Unknown"
    bot_item_name = await pool.fetchval("SELECT b.name FROM brainrots b WHERE id=$1", bot_item["brainrot_id"]) or "Unknown"
    await db.join_coinflip_bot(pool, game_id, bot_item["id"], s["user_id"] if won else None, s["user_id"])
    await pool.execute("DELETE FROM inventory WHERE id=$1", game["creator_inventory_id"])
    won_val = float(bot_item["value"]) if won else 0.0
    await db.record_game_result(pool, s["user_id"], won, wagered_val, won_val)
    try:
        await log_callbot(caller_name, s["user_id"], caller_av or "", user_item_name, wagered_val, bot_item_name, float(bot_item["value"]), won)
    except Exception:
        pass
    return JSONResponse({"you_won": won, "taxed": taxed, "bot_item_name": bot_item_name, "bot_item_value": float(bot_item["value"])})

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
    claimed = await pool.fetchval("UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id", offered["id"])
    if not claimed:
        raise HTTPException(400, "Item is already wagered in another game")
    raw_chance = min((ov/tv)*100, 95)
    win_chance = raw_chance * 0.95
    roll = round(random.uniform(0,100), 2)
    won = roll <= win_chance
    offered_name = await pool.fetchval("SELECT b.name FROM brainrots b JOIN inventory i ON i.brainrot_id=b.id WHERE i.id=$1", offered["id"]) or "Unknown"
    target_name = await pool.fetchval("SELECT b.name FROM brainrots b JOIN bot_stock s ON s.brainrot_id=b.id WHERE s.id=$1", target["id"]) or "Unknown"
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
                await conn.execute("INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits) VALUES ($1, $2, $3, $4)", s["user_id"], target["brainrot_id"], target["mutation_id"], target["traits"])
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
        player_av = player["avatar"] if player else ""
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
        to_user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", int(to_id))
        if item and from_user and to_user:
            await log_tip(from_user["username"], s["user_id"], to_user["username"], int(to_id), item["name"], float(item["value"]), item["mutation"])
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
OXAPAY_API = "https://api.oxapay.com"
COINS_PER_USD = 10

@app.post("/api/sabcoin/deposit")
async def api_sabcoin_deposit(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 1:
        raise HTTPException(400, "Minimum deposit is $1")
    if not OXAPAY_MERCHANT:
        raise HTTPException(500, "Payment processor not configured — contact admin")
    coins = round(amount_usd * COINS_PER_USD, 2)
    order_id = secrets.token_hex(16)
    base_url = os.environ.get("BASE_URL", "")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{OXAPAY_API}/merchants/request", json={
            "merchant": OXAPAY_MERCHANT,
            "amount": amount_usd,
            "currency": "USD",
            "lifeTime": 60,
            "orderId": order_id,
            "description": f"SabPot deposit — {coins} SC for {s['username']}",
            "callbackUrl": f"{base_url}/api/sabcoin/webhook",
            "returnUrl": f"{base_url}/",
        })
        if resp.status_code != 200:
            raise HTTPException(502, f"Payment processor error ({resp.status_code})")
        data = resp.json()
        if data.get("result") != 100:
            raise HTTPException(502, data.get("message", "Payment error"))
    # OxaPay v1 uses payLink, v2 uses trackId+payAddress
    pay_link = data.get("payLink") or data.get("link") or data.get("url") or ""
    pay_address = data.get("payAddress", "")
    track_id = data.get("trackId", "")
    pool = await db.get_pool()
    await db.create_deposit(pool, s["user_id"], order_id, pay_address or order_id, amount_usd, coins)
    return JSONResponse({
        "order_id": order_id,
        "pay_link": pay_link,
        "pay_address": pay_address,
        "track_id": track_id,
        "coins": coins,
    })

@app.post("/api/sabcoin/webhook")
async def oxapay_webhook(request: Request):
    body = await request.json()
    order_id = body.get("orderId","")
    status = body.get("status","")
    confirmations = int(body.get("confirmations", 0))
    if not order_id:
        return JSONResponse({"ok": True})
    pool = await db.get_pool()
    dep = await db.get_deposit(pool, order_id)
    if not dep or dep["status"] == "credited":
        return JSONResponse({"ok": True})
    await pool.execute("UPDATE sabcoin_deposits SET confirmations=$2 WHERE order_id=$1", order_id, confirmations)
    if (confirmations >= 3 and status in ("Confirming", "Paid", "Completed")) or status == "Completed":
        result = await db.confirm_deposit(pool, order_id)
        if result["success"]:
            try:
                user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", result["user_id"])
                uname = user["username"] if user else str(result["user_id"])
                import discord
                await post_to_log("🔐login", discord.Embed(
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
            "id": l["id"],
            "seller_id": l["seller_id"],
            "seller_name": l["seller_name"],
            "seller_avatar": l["seller_avatar"],
            "price_coins": float(l["price_coins"]),
            "item_name": l["item_name"],
            "emoji": l["emoji"],
            "tier": l["tier"],
            "image_url": l["image_url"],
            "mutation": l["mutation"],
            "multiplier": float(l["multiplier"]),
            "traits": l["traits"],
            "item_value": float(l["item_value"]),
        })
    return JSONResponse(result)

@app.post("/api/marketplace/list")
async def api_marketplace_list(request: Request):
    s = await require_user(request)
    body = await request.json()
    inv_id = body.get("inventory_id")
    price = float(body.get("price_coins", 0))
    if price <= 0:
        raise HTTPException(400, "Price must be positive")
    pool = await db.get_pool()
    listing_id = await db.create_listing(pool, s["user_id"], inv_id, price)
    if not listing_id:
        raise HTTPException(400, "Item is already in use or not found")
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

# ─── SABCOIN WITHDRAW ─────────────────────────────────────────────────────────

@app.post("/api/sabcoin/withdraw")
async def api_sabcoin_withdraw(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount_coins = float(body.get("amount_coins", 0))
    currency = body.get("currency", "LTC")
    address = body.get("address", "").strip()
    if amount_coins < 10:
        raise HTTPException(400, "Minimum withdrawal is 10 SabCoins")
    if not address:
        raise HTTPException(400, "Address required")
    TAX = 0.10
    after_tax = round(amount_coins * (1 - TAX), 2)
    tax_burned = round(amount_coins * TAX, 2)
    # Convert SC to USD (10 SC = $1)
    usd_amount = round(after_tax / 10, 2)
    order_id = secrets.token_hex(16)
    pool = await db.get_pool()
    # Deduct coins first atomically
    wid = await db.create_withdrawal(pool, s["user_id"], amount_coins, after_tax, tax_burned, currency, address, order_id)
    if not wid:
        raise HTTPException(400, "Insufficient SabCoins")
    # Attempt actual OxaPay payout
    oxapay_success = False
    oxapay_error = None
    if OXAPAY_MERCHANT:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                payout_resp = await client.post(
                    f"{OXAPAY_API}/merchants/request/payout",
                    json={
                        "merchant": OXAPAY_MERCHANT,
                        "amount": usd_amount,
                        "currency": currency,
                        "address": address,
                        "orderId": order_id,
                        "description": f"SabPot withdrawal — {after_tax} SC for {s['username']}",
                    }
                )
                payout_data = payout_resp.json()
                if payout_resp.status_code == 200 and payout_data.get("result") == 100:
                    oxapay_success = True
                    # Mark as processing in DB
                    await pool.execute(
                        "UPDATE sabcoin_withdrawals SET status='processing' WHERE order_id=$1",
                        order_id
                    )
                else:
                    oxapay_error = payout_data.get("message", "Payout API error")
        except Exception as e:
            oxapay_error = str(e)
    # Log to Discord
    try:
        import discord as _discord
        status_txt = "✅ Payout sent" if oxapay_success else f"⚠️ Manual required: {oxapay_error or 'No merchant key'}"
        embed = _discord.Embed(
            title="💸 SC Withdrawal",
            description=f"**{s['username']}** withdrew **{amount_coins} SC** → ${usd_amount} {currency}",
            color=0x22c55e if oxapay_success else 0xf59e0b
        )
        embed.add_field(name="Address", value=f"`{address}`", inline=False)
        embed.add_field(name="After Tax", value=f"{after_tax} SC (~${usd_amount})", inline=True)
        embed.add_field(name="Status", value=status_txt, inline=True)
        await post_to_log("🔐login", embed)
    except Exception:
        pass
    return JSONResponse({
        "success": True,
        "withdrawal_id": wid,
        "after_tax": after_tax,
        "tax": tax_burned,
        "usd_value": usd_amount,
        "currency": currency,
        "payout_sent": oxapay_success,
    })

# ─── ADMIN ENDPOINTS ──────────────────────────────────────────────────────────

# ─── BRAINROTS / MUTATIONS (used by admin panel) ──────────────────────────────

@app.get("/api/brainrots")
async def api_brainrots():
    pool = await db.get_pool()
    rows = await db.get_all_brainrots(pool)
    return JSONResponse([{
        "id": r["id"], "name": r["name"], "base_value": float(r["base_value"]),
        "tier": r["tier"], "emoji": r["emoji"], "image_url": r["image_url"]
    } for r in rows])

@app.get("/api/mutations")
async def api_mutations():
    pool = await db.get_pool()
    rows = await db.get_all_mutations(pool)
    return JSONResponse([{
        "id": r["id"], "name": r["name"], "multiplier": float(r["multiplier"])
    } for r in rows])

# ─── ITEM WITHDRAW TICKET ─────────────────────────────────────────────────────

@app.get("/api/sabcoin/currencies")
async def api_sabcoin_currencies():
    """Returns supported withdrawal currencies via OxaPay."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.oxapay.com/merchants/allowedCoins",
                json={"merchant": OXAPAY_MERCHANT} if OXAPAY_MERCHANT else {}
            )
            if resp.status_code == 200:
                data = resp.json()
                currencies = []
                if isinstance(data, dict) and "data" in data:
                    currencies = [c if isinstance(c, str) else c.get("currency","") for c in data["data"]]
                elif isinstance(data, list):
                    currencies = [c if isinstance(c, str) else c.get("currency","") for c in data]
                currencies = [c for c in currencies if c]
                if currencies:
                    return JSONResponse(currencies)
    except Exception:
        pass
    return JSONResponse(["LTC", "BTC", "ETH", "USDT", "TRX", "BNB", "DOGE"])

@app.post("/api/withdraw/items")
async def api_withdraw_items(request: Request):
    """User selects up to 10 inventory items to withdraw — opens a Discord ticket listing them."""
    s = await require_user(request)
    body = await request.json()
    inv_ids = body.get("inventory_ids", [])
    if not inv_ids or not isinstance(inv_ids, list):
        raise HTTPException(400, "No items selected")
    if len(inv_ids) > 10:
        raise HTTPException(400, "Maximum 10 items per withdrawal")

    pool = await db.get_pool()

    # Verify all items belong to user and are not in_use
    items = await pool.fetch("""
        SELECT i.id, b.name, b.emoji, m.name AS mutation, i.traits,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.id = ANY($1) AND i.user_id = $2 AND i.in_use = FALSE
    """, inv_ids, s["user_id"])

    if not items:
        raise HTTPException(400, "No valid items found")
    if len(items) != len(inv_ids):
        raise HTTPException(400, "Some items are invalid, not yours, or currently in use")

    user = await pool.fetchrow("SELECT username FROM users WHERE id=$1", s["user_id"])
    username = user["username"] if user else str(s["user_id"])

    # Build item list
    item_lines = []
    total_val = 0.0
    for it in items:
        mut = f" [{it['mutation']}]" if it['mutation'] != 'Base' else ""
        traits_str = f" +{it['traits']}T" if it['traits'] > 0 else ""
        val = float(it['value'])
        total_val += val
        item_lines.append(f"• {it['emoji']} **{it['name']}**{mut}{traits_str} — ⬡{db.format_value(val)}")

    # Create Discord ticket via HTTP API (single client block — fixed)
    guild_id = int(os.environ.get("DISCORD_GUILD_ID", 0))
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN", "")
    ticket_channel_id = None

    if token and guild_id:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Get channels to find ticket category
                guild_resp = await client.get(
                    f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                    headers={"Authorization": f"Bot {token}"}
                )
                channels = guild_resp.json() if guild_resp.status_code == 200 else []
                category_id = None
                for ch in channels:
                    if ch.get("type") == 4 and "ticket" in ch.get("name", "").lower():
                        category_id = ch["id"]
                        break

                # Create ticket channel
                overwrites = [
                    {"id": str(guild_id), "type": 0, "deny": "1024"},
                    {"id": str(s["user_id"]), "type": 1, "allow": "3072"},
                ]
                ch_payload = {
                    "name": f"withdraw-{username[:20]}",
                    "type": 0,
                    "permission_overwrites": overwrites,
                }
                if category_id:
                    ch_payload["parent_id"] = category_id

                ch_resp = await client.post(
                    f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                    json=ch_payload,
                    headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                )

                if ch_resp.status_code in (200, 201):
                    ch_data = ch_resp.json()
                    ticket_channel_id = ch_data["id"]

                    embed = {
                        "title": "📤 Item Withdrawal Request",
                        "description": (
                            f"<@{s['user_id']}> wants to withdraw "
                            f"**{len(items)} item{'s' if len(items)>1 else ''}** "
                            f"(Total: ⬡{db.format_value(total_val)})\n\n"
                            + "\n".join(item_lines)
                        ),
                        "color": 0xf59e0b,
                        "fields": [
                            {"name": "Username", "value": f"`{username}`", "inline": True},
                            {"name": "Discord ID", "value": f"`{s['user_id']}`", "inline": True},
                            {"name": "Total Value", "value": f"⬡{db.format_value(total_val)}", "inline": True},
                        ],
                        "footer": {"text": "An admin will process this withdrawal shortly."}
                    }
                    await client.post(
                        f"https://discord.com/api/v10/channels/{ticket_channel_id}/messages",
                        json={"content": f"<@{s['user_id']}>", "embeds": [embed]},
                        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                    )
        except Exception as e:
            print(f"⚠️ Item withdraw ticket error: {e}")

    # Record ticket in DB
    await db.create_ticket(pool, s["user_id"], "withdraw",
                           int(ticket_channel_id) if ticket_channel_id else 0)

    return JSONResponse({
        "success": True,
        "ticket_created": ticket_channel_id is not None,
        "item_count": len(items)
    })

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    await require_admin(request)
    q = request.query_params.get("q", "").strip()
    pool = await db.get_pool()
    if q:
        rows = await pool.fetch("""
            SELECT id, username, avatar, login_code, is_banned, timeout_until,
                   total_games, total_wins, sabcoins, created_at
            FROM users
            WHERE username ILIKE $1
            ORDER BY created_at DESC LIMIT 100
        """, f"%{q}%")
    else:
        rows = await pool.fetch("""
            SELECT id, username, avatar, login_code, is_banned, timeout_until,
                   total_games, total_wins, sabcoins, created_at
            FROM users ORDER BY created_at DESC LIMIT 200
        """)
    return JSONResponse([{
        "id": r["id"], "username": r["username"], "avatar": r["avatar"],
        "login_code": r["login_code"], "is_banned": r["is_banned"],
        "timeout_until": r["timeout_until"].isoformat() if r["timeout_until"] else None,
        "total_games": r["total_games"], "total_wins": r["total_wins"],
        "sabcoins": float(r["sabcoins"] or 0),
        "created_at": r["created_at"].isoformat()
    } for r in rows])

@app.post("/api/admin/ban")
async def api_admin_ban(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id = body.get("user_id")
    banned = body.get("banned", True)
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET is_banned=$2 WHERE id=$1", int(user_id), banned)
    return JSONResponse({"success": True})

@app.post("/api/admin/unban")
async def api_admin_unban(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id = body.get("user_id")
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE users SET is_banned=FALSE, timeout_until=NULL WHERE id=$1", int(user_id)
    )
    return JSONResponse({"success": True})

@app.post("/api/admin/addcoins")
async def api_admin_addcoins(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    amount = float(body.get("amount", 0))
    if not user_id or amount <= 0:
        raise HTTPException(400, "Invalid user_id or amount")
    pool = await db.get_pool()
    user = await pool.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
    if not user:
        raise HTTPException(404, "User not found")
    await db.credit_sabcoins(pool, user_id, amount)
    new_bal = await db.get_sabcoins(pool, user_id)
    return JSONResponse({"success": True, "new_balance": new_bal})

@app.post("/api/admin/additem")
async def api_admin_additem(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    brainrot_id = int(body.get("brainrot_id", 0))
    mutation_id = int(body.get("mutation_id", 0))
    traits = int(body.get("traits", 0))
    if not user_id or not brainrot_id or not mutation_id:
        raise HTTPException(400, "Missing fields")
    pool = await db.get_pool()
    inv_id = await db.add_item_to_inventory(pool, user_id, brainrot_id, mutation_id, traits)
    return JSONResponse({"success": True, "inventory_id": inv_id})

@app.post("/api/admin/timeout")
async def api_admin_timeout(request: Request):
    await require_admin(request)
    body = await request.json()
    user_id = body.get("user_id")
    minutes = int(body.get("minutes", 0))
    import datetime as _dt
    until = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=minutes) if minutes > 0 else None
    pool = await db.get_pool()
    await pool.execute("UPDATE users SET timeout_until=$2 WHERE id=$1", int(user_id), until)
    return JSONResponse({"success": True})

@app.get("/api/admin/promos")
async def api_admin_promos(request: Request):
    await require_admin(request)
    pool = await db.get_pool()
    rows = await db.get_all_promos(pool)
    return JSONResponse([{
        "id": r["id"], "code": r["code"], "max_redeems": r["max_redeems"],
        "redeems": r["redeems"], "active": r["active"],
        "item_name": r["item_name"], "emoji": r["emoji"],
        "mutation_name": r["mutation_name"], "value": float(r["value"]),
        "created_at": r["created_at"].isoformat()
    } for r in rows])

@app.post("/api/admin/promo/deactivate")
async def api_admin_deactivate_promo(request: Request):
    await require_admin(request)
    body = await request.json()
    pool = await db.get_pool()
    await pool.execute("UPDATE promo_codes SET active=FALSE WHERE id=$1", body.get("promo_id"))
    return JSONResponse({"success": True})

# ─── SC COINFLIP ──────────────────────────────────────────────────────────────

@app.get("/api/coinflip/sc/list")
async def api_sc_coinflip_list():
    pool = await db.get_pool()
    games = await pool.fetch("""
        SELECT g.id, g.creator_id, g.creator_side, g.amount, g.status, g.created_at,
               u.username AS creator_name, u.avatar AS creator_avatar
        FROM sc_coinflip_games g
        JOIN users u ON g.creator_id = u.id
        WHERE g.status = 'open'
        ORDER BY g.created_at DESC
        LIMIT 50
    """)
    return JSONResponse([{
        "id": g["id"],
        "creator_id": g["creator_id"],
        "creator_name": g["creator_name"],
        "creator_avatar": g["creator_avatar"],
        "creator_side": g["creator_side"],
        "value": float(g["amount"]),
        "amount": float(g["amount"]),
        "status": g["status"],
    } for g in games], headers={"Cache-Control": "public, max-age=3"})

@app.post("/api/coinflip/sc/create")
async def api_sc_coinflip_create(request: Request):
    s = await require_user(request)
    body = await request.json()
    amount = float(body.get("amount", 0))
    side = body.get("side", "fire")
    if side not in ("fire", "ice"):
        raise HTTPException(400, "Invalid side")
    if amount < 1:
        raise HTTPException(400, "Minimum bet is 1 SC")
    pool = await db.get_pool()
    # Deduct coins atomically
    ok = await db.debit_sabcoins(pool, s["user_id"], amount)
    if not ok:
        raise HTTPException(400, "Insufficient SabCoins")
    game_id = await pool.fetchval("""
        INSERT INTO sc_coinflip_games (creator_id, creator_side, amount)
        VALUES ($1, $2, $3) RETURNING id
    """, s["user_id"], side, amount)
    return JSONResponse({"game_id": game_id})

@app.post("/api/coinflip/sc/join/{game_id}")
async def api_sc_coinflip_join(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow("""
                UPDATE sc_coinflip_games
                SET joiner_id=$2, status='processing'
                WHERE id=$1 AND status='open'
                RETURNING *
            """, game_id, s["user_id"])
            if not game:
                raise HTTPException(404, "Game not found or already taken")
            if game["creator_id"] == s["user_id"]:
                await conn.execute("UPDATE sc_coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
                raise HTTPException(400, "Cannot join your own game")
            amount = float(game["amount"])
            # Deduct joiner's coins
            bal = await conn.fetchval("SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", s["user_id"])
            if float(bal or 0) < amount:
                await conn.execute("UPDATE sc_coinflip_games SET status='open', joiner_id=NULL WHERE id=$1", game_id)
                raise HTTPException(400, "Insufficient SabCoins")
            await conn.execute("UPDATE users SET sabcoins=sabcoins-$2 WHERE id=$1", s["user_id"], amount)
            # Pick winner 50/50, house takes 15%
            winner_id = random.choice([game["creator_id"], s["user_id"]])
            loser_id = game["creator_id"] if winner_id == s["user_id"] else s["user_id"]
            payout = round(amount * 2 * 0.85, 2)
            await conn.execute("UPDATE users SET sabcoins=sabcoins+$2 WHERE id=$1", winner_id, payout)
            await conn.execute("UPDATE users SET total_games=total_games+1 WHERE id=ANY($1)", [game["creator_id"], s["user_id"]])
            await conn.execute("UPDATE users SET total_wins=total_wins+1 WHERE id=$1", winner_id)
            await conn.execute("""
                UPDATE sc_coinflip_games
                SET winner_id=$2, status='completed', completed_at=NOW()
                WHERE id=$1
            """, game_id, winner_id)
    return JSONResponse({
        "winner_id": winner_id,
        "you_won": winner_id == s["user_id"],
        "payout": payout if winner_id == s["user_id"] else 0
    })

@app.post("/api/coinflip/sc/cancel/{game_id}")
async def api_sc_coinflip_cancel(game_id: int, request: Request):
    s = await require_user(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow("""
                UPDATE sc_coinflip_games SET status='cancelled'
                WHERE id=$1 AND status='open' AND creator_id=$2
                RETURNING *
            """, game_id, s["user_id"])
            if not game:
                raise HTTPException(404, "Game not found or not yours")
            # Refund creator
            await conn.execute("UPDATE users SET sabcoins=sabcoins+$2 WHERE id=$1", s["user_id"], float(game["amount"]))
    return JSONResponse({"success": True})

# ─── PAGES ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/{page}", response_class=HTMLResponse)
async def catch_all(request: Request, page: str):
    try:
        return templates.TemplateResponse(f"{page}.html", {"request": request})
    except Exception:
        return templates.TemplateResponse("index.html", {"request": request})
