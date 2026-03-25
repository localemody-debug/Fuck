import asyncpg
import os
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"],
            min_size=5,
            max_size=20,
            max_inactive_connection_lifetime=300,
            command_timeout=10,
            statement_cache_size=200,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool and not _pool._closed:
        await _pool.close()
    _pool = None


# ─── VALUE HELPERS ────────────────────────────────────────────────────────────

def calc_value(base: float, mutation_mult: float, traits: int) -> float:
    return round(float(base) * float(mutation_mult) * (1 + traits * 0.07), 2)


def format_value(v) -> str:
    v = float(v)
    if v >= 1000:
        return f"{v/1000:.1f}k"
    if v % 1 == 0:
        return str(int(v))
    return f"{v:.1f}"


# ─── USER ─────────────────────────────────────────────────────────────────────

async def get_user(pool: asyncpg.Pool, user_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE id=$1", user_id)


async def ensure_user(pool_arg, user_id: int, username: str, avatar: str = None):
    """Create user if not exists, assign login_code atomically without collision."""
    import random as _random

    existing = await pool_arg.fetchval("SELECT login_code FROM users WHERE id=$1", user_id)
    if existing is not None:
        await pool_arg.execute(
            "UPDATE users SET username=$2, avatar=$3 WHERE id=$1",
            user_id, username, avatar
        )
        return

    # New user — find a unique code and insert atomically
    for attempt in range(50):
        code = str(_random.randint(1000, 9999))
        try:
            await pool_arg.execute("""
                INSERT INTO users (id, username, avatar, login_code)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE
                    SET username = EXCLUDED.username,
                        avatar   = EXCLUDED.avatar,
                        login_code = COALESCE(users.login_code, EXCLUDED.login_code)
            """, user_id, username, avatar, code)
            return
        except Exception as e:
            if "login_code" in str(e) and attempt < 49:
                continue
            await pool_arg.execute("""
                INSERT INTO users (id, username, avatar)
                VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET username=$2, avatar=$3
            """, user_id, username, avatar)
            return


async def get_user_by_code(pool, code: str):
    return await pool.fetchrow("SELECT * FROM users WHERE login_code=$1", code.strip())


async def get_all_users(pool) -> list:
    """Return all users for admin dropdowns — id, username, avatar."""
    return await pool.fetch(
        "SELECT id, username, avatar FROM users ORDER BY username ASC"
    )


# ─── STAT HELPERS ────────────────────────────────────────────────────────────

async def record_game_result(pool, user_id: int, won: bool,
                              wagered_value: float, won_value: float = 0):
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT current_streak, best_streak FROM users WHERE id=$1", user_id
            )
            if not user:
                return
            if won:
                new_streak = (user["current_streak"] or 0) + 1
                new_best = max(user["best_streak"] or 0, new_streak)
                await conn.execute("""
                    UPDATE users SET
                        total_games    = total_games + 1,
                        total_wins     = total_wins + 1,
                        current_streak = $2,
                        best_streak    = $3,
                        total_wagered  = total_wagered + $4,
                        total_won      = total_won + $5
                    WHERE id = $1
                """, user_id, new_streak, new_best, wagered_value, won_value)
            else:
                await conn.execute("""
                    UPDATE users SET
                        total_games    = total_games + 1,
                        current_streak = 0,
                        total_wagered  = total_wagered + $2
                    WHERE id = $1
                """, user_id, wagered_value)


async def get_profile(pool, user_id: int):
    return await pool.fetchrow("""
        SELECT u.id, u.username, u.avatar, u.total_games, u.total_wins,
               u.current_streak, u.best_streak, u.total_wagered, u.total_won,
               u.sabcoins, u.login_code,
               COALESCE(SUM(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)), 0) AS net_worth
        FROM users u
        LEFT JOIN inventory i ON i.user_id = u.id
        LEFT JOIN brainrots b ON i.brainrot_id = b.id
        LEFT JOIN mutations m ON i.mutation_id = m.id
        WHERE u.id = $1
        GROUP BY u.id, u.username, u.avatar, u.total_games, u.total_wins,
                 u.current_streak, u.best_streak, u.total_wagered, u.total_won,
                 u.sabcoins, u.login_code
    """, user_id)


# ─── INVENTORY ────────────────────────────────────────────────────────────────

async def get_inventory(pool: asyncpg.Pool, user_id: int):
    return await pool.fetch("""
        SELECT i.id,
               b.name, b.base_value, b.tier, b.emoji, b.image_url,
               m.name AS mutation, m.multiplier, i.traits, i.in_use,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.user_id = $1
        ORDER BY value DESC
    """, user_id)


async def get_inventory_total(pool: asyncpg.Pool, user_id: int) -> float:
    result = await pool.fetchval("""
        SELECT COALESCE(SUM(
            ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)
        ), 0)
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.user_id = $1
    """, user_id)
    return float(result)


async def get_me_data(pool: asyncpg.Pool, user_id: int) -> dict:
    row = await pool.fetchrow("""
        SELECT
            u.login_code,
            u.sabcoins,
            COALESCE(SUM(ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)), 0) AS inventory_value
        FROM users u
        LEFT JOIN inventory i ON i.user_id = u.id
        LEFT JOIN brainrots b ON i.brainrot_id = b.id
        LEFT JOIN mutations m ON i.mutation_id = m.id
        WHERE u.id = $1
        GROUP BY u.login_code, u.sabcoins
    """, user_id)
    if not row:
        return {"inventory_value": 0.0, "sabcoins": 0.0, "login_code": None}
    return {
        "inventory_value": float(row["inventory_value"] or 0),
        "sabcoins": float(row["sabcoins"] or 0),
        "login_code": row["login_code"],
    }


async def add_item_to_inventory(pool: asyncpg.Pool, user_id: int,
                                 brainrot_id: int, mutation_id: int, traits: int = 0) -> int:
    return await pool.fetchval("""
        INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, user_id, brainrot_id, mutation_id, traits)


async def remove_item_from_inventory(pool: asyncpg.Pool, inventory_id: int):
    await pool.execute("DELETE FROM inventory WHERE id=$1", inventory_id)


async def transfer_item(pool: asyncpg.Pool, inventory_id: int, to_user_id: int):
    await pool.execute("UPDATE inventory SET user_id=$1 WHERE id=$2", to_user_id, inventory_id)


# ─── BOT STOCK ────────────────────────────────────────────────────────────────

async def get_bot_stock(pool: asyncpg.Pool):
    return await pool.fetch("""
        SELECT s.id,
               b.name, b.base_value, b.tier, b.emoji, b.image_url,
               m.name AS mutation, m.multiplier, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        ORDER BY value DESC
    """)


async def add_to_bot_stock(pool: asyncpg.Pool, brainrot_id: int,
                            mutation_id: int, traits: int = 0) -> int:
    return await pool.fetchval("""
        INSERT INTO bot_stock (brainrot_id, mutation_id, traits)
        VALUES ($1, $2, $3) RETURNING id
    """, brainrot_id, mutation_id, traits)


async def remove_from_bot_stock(pool: asyncpg.Pool, stock_id: int):
    await pool.execute("DELETE FROM bot_stock WHERE id=$1", stock_id)


# ─── BRAINROT / MUTATION LOOKUP ───────────────────────────────────────────────

async def get_all_brainrots(pool: asyncpg.Pool):
    return await pool.fetch("SELECT * FROM brainrots ORDER BY base_value ASC, name ASC")


async def get_all_mutations(pool: asyncpg.Pool):
    return await pool.fetch("SELECT * FROM mutations ORDER BY multiplier ASC")


# ─── COINFLIP ─────────────────────────────────────────────────────────────────

async def create_coinflip(pool: asyncpg.Pool, creator_id: int,
                           inventory_id: int, side: str) -> int:
    return await pool.fetchval("""
        INSERT INTO coinflip_games (creator_id, creator_inventory_id, creator_side)
        VALUES ($1, $2, $3) RETURNING id
    """, creator_id, inventory_id, side)


async def get_open_coinflips(pool: asyncpg.Pool):
    return await pool.fetch("""
        SELECT g.id, g.creator_id, g.creator_inventory_id, g.creator_side,
               u.username AS creator_name, u.avatar AS creator_avatar,
               b.name AS item_name, b.emoji, b.tier, b.image_url,
               m.name AS mutation, m.multiplier, i.traits,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM coinflip_games g
        JOIN users u ON g.creator_id = u.id
        JOIN inventory i ON g.creator_inventory_id = i.id
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE g.status = 'open'
        ORDER BY value DESC
    """)


async def claim_coinflip(pool: asyncpg.Pool, game_id: int, joiner_id: int):
    return await pool.fetchrow("""
        UPDATE coinflip_games
        SET joiner_id=$2, status='processing'
        WHERE id=$1 AND status='open'
        RETURNING *
    """, game_id, joiner_id)


async def complete_coinflip(pool: asyncpg.Pool, game_id: int, winner_id: int):
    await pool.execute("""
        UPDATE coinflip_games
        SET winner_id=$2, status='completed', completed_at=NOW()
        WHERE id=$1
    """, game_id, winner_id)


async def cancel_coinflip(pool: asyncpg.Pool, game_id: int):
    game = await pool.fetchrow(
        "SELECT creator_inventory_id FROM coinflip_games WHERE id=$1 AND status='open'", game_id
    )
    await pool.execute(
        "UPDATE coinflip_games SET status='cancelled' WHERE id=$1 AND status='open'", game_id
    )
    if game and game["creator_inventory_id"]:
        await pool.execute("UPDATE inventory SET in_use=FALSE WHERE id=$1",
                           game["creator_inventory_id"])


async def join_coinflip_bot(pool: asyncpg.Pool, game_id: int, bot_stock_id: int,
                             winner_id, user_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow(
                "SELECT * FROM coinflip_games WHERE id=$1 AND status IN ('open','processing')",
                game_id
            )
            if not game:
                return
            bot_item = await conn.fetchrow("SELECT * FROM bot_stock WHERE id=$1", bot_stock_id)
            if not bot_item:
                return
            if winner_id == user_id:
                await conn.execute("""
                    INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits)
                    VALUES ($1, $2, $3, $4)
                """, user_id, bot_item["brainrot_id"], bot_item["mutation_id"], bot_item["traits"])
                await conn.execute("DELETE FROM bot_stock WHERE id=$1", bot_stock_id)
            await conn.execute(
                "UPDATE coinflip_games SET status='completed', completed_at=NOW() WHERE id=$1",
                game_id
            )


# ─── UPGRADER ─────────────────────────────────────────────────────────────────

async def record_upgrade(pool: asyncpg.Pool, user_id: int, offered_inv_id: int,
                          target_stock_id: int, win_chance: float, roll: float, won: bool):
    await pool.execute("""
        INSERT INTO upgrade_games
            (user_id, offered_inventory_id, target_bot_stock_id, win_chance, roll, won)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, user_id, offered_inv_id, target_stock_id, win_chance, roll, won)


# ─── TICKETS ──────────────────────────────────────────────────────────────────

async def create_ticket(pool: asyncpg.Pool, user_id: int,
                         ticket_type: str, channel_id: int) -> int:
    return await pool.fetchval("""
        INSERT INTO tickets (user_id, type, channel_id)
        VALUES ($1, $2, $3) RETURNING id
    """, user_id, ticket_type, channel_id)


async def close_ticket(pool: asyncpg.Pool, channel_id: int):
    await pool.execute(
        "UPDATE tickets SET status='closed' WHERE channel_id=$1", channel_id
    )


# ─── TIPS ─────────────────────────────────────────────────────────────────────

async def send_tip(pool, from_id: int, to_id: int, inventory_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            item = await conn.fetchrow(
                "SELECT * FROM inventory WHERE id=$1 AND user_id=$2 AND in_use=FALSE FOR UPDATE",
                inventory_id, from_id
            )
            if not item:
                return {"success": False, "reason": "Item not found, not yours, or currently wagered"}
            if from_id == to_id:
                return {"success": False, "reason": "Cannot tip yourself"}
            to_user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", to_id)
            if not to_user:
                return {"success": False, "reason": "Recipient not found"}
            await conn.execute(
                "UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id=$2", to_id, inventory_id
            )
            await conn.execute(
                "INSERT INTO tips (from_user_id, to_user_id, inventory_id) VALUES ($1, $2, $3)",
                from_id, to_id, inventory_id
            )
            return {"success": True}


# ─── LEADERBOARD ──────────────────────────────────────────────────────────────

async def get_leaderboard(pool: asyncpg.Pool, limit: int = 10):
    return await pool.fetch("""
        SELECT u.id, u.username, u.avatar, u.total_games, u.total_wins,
               COALESCE(SUM(
                   ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2)
               ), 0) AS net_worth
        FROM users u
        LEFT JOIN inventory i ON i.user_id = u.id
        LEFT JOIN brainrots b ON i.brainrot_id = b.id
        LEFT JOIN mutations m ON i.mutation_id = m.id
        GROUP BY u.id
        ORDER BY net_worth DESC
        LIMIT $1
    """, limit)


# ─── PROMO CODES ──────────────────────────────────────────────────────────────

async def create_promo(pool: asyncpg.Pool, code: str, stock_id: int,
                        brainrot_id: int, mutation_id: int, traits: int,
                        max_redeems: int) -> int:
    return await pool.fetchval("""
        INSERT INTO promo_codes (code, stock_id, brainrot_id, mutation_id, traits, max_redeems)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
    """, code, stock_id, brainrot_id, mutation_id, traits, max_redeems)


async def get_promo(pool: asyncpg.Pool, code: str):
    return await pool.fetchrow("""
        SELECT p.*, b.name AS item_name, b.emoji, m.name AS mutation_name, m.multiplier,
               ROUND(b.base_value * m.multiplier * (1 + p.traits * 0.07), 2) AS value
        FROM promo_codes p
        JOIN brainrots b ON p.brainrot_id = b.id
        JOIN mutations m ON p.mutation_id = m.id
        WHERE p.code = $1 AND p.active = TRUE
    """, code.upper())


async def redeem_promo(pool: asyncpg.Pool, code: str, user_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            promo = await conn.fetchrow("""
                SELECT p.*, b.name AS item_name, b.emoji, m.name AS mutation_name,
                       p.brainrot_id, p.mutation_id, p.traits
                FROM promo_codes p
                JOIN brainrots b ON p.brainrot_id = b.id
                JOIN mutations m ON p.mutation_id = m.id
                WHERE p.code = $1 AND p.active = TRUE
                FOR UPDATE
            """, code.upper())
            if not promo:
                return {"success": False, "reason": "Invalid or expired code"}
            if promo["redeems"] >= promo["max_redeems"]:
                return {"success": False, "reason": "Code has reached max redeems"}
            already = await conn.fetchval(
                "SELECT 1 FROM promo_redemptions WHERE code_id=$1 AND user_id=$2",
                promo["id"], user_id
            )
            if already:
                return {"success": False, "reason": "You already redeemed this code"}
            await conn.execute("""
                INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits)
                VALUES ($1, $2, $3, $4)
            """, user_id, promo["brainrot_id"], promo["mutation_id"], promo["traits"])
            await conn.execute(
                "UPDATE promo_codes SET redeems=redeems+1 WHERE id=$1", promo["id"]
            )
            await conn.execute(
                "INSERT INTO promo_redemptions (code_id, user_id) VALUES ($1, $2)",
                promo["id"], user_id
            )
            if promo["redeems"] + 1 >= promo["max_redeems"]:
                await conn.execute(
                    "UPDATE promo_codes SET active=FALSE WHERE id=$1", promo["id"]
                )
            return {"success": True, "item_name": promo["item_name"],
                    "emoji": promo["emoji"], "mutation": promo["mutation_name"]}


async def get_all_promos(pool: asyncpg.Pool):
    return await pool.fetch("""
        SELECT p.id, p.code, p.max_redeems, p.redeems, p.active, p.created_at,
               b.name AS item_name, b.emoji, m.name AS mutation_name,
               ROUND(b.base_value * m.multiplier * (1 + p.traits * 0.07), 2) AS value
        FROM promo_codes p
        JOIN brainrots b ON p.brainrot_id = b.id
        JOIN mutations m ON p.mutation_id = m.id
        ORDER BY p.created_at DESC
    """)


# ─── SABCOIN ──────────────────────────────────────────────────────────────────

async def get_sabcoins(pool, user_id: int) -> float:
    val = await pool.fetchval("SELECT sabcoins FROM users WHERE id=$1", user_id)
    return float(val or 0)


async def credit_sabcoins(pool, user_id: int, amount: float):
    await pool.execute(
        "UPDATE users SET sabcoins = sabcoins + $2 WHERE id=$1", user_id, amount
    )


async def debit_sabcoins(pool, user_id: int, amount: float) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            bal = await conn.fetchval(
                "SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", user_id
            )
            if float(bal or 0) < amount:
                return False
            await conn.execute(
                "UPDATE users SET sabcoins = sabcoins - $2 WHERE id=$1", user_id, amount
            )
            return True


async def create_deposit(pool, user_id: int, order_id: str, ltc_address: str,
                          amount_usd: float, coins: float) -> int:
    return await pool.fetchval("""
        INSERT INTO sabcoin_deposits (user_id, order_id, ltc_address, amount_usd, coins_to_credit)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
    """, user_id, order_id, ltc_address, amount_usd, coins)


async def get_deposit(pool, order_id: str):
    return await pool.fetchrow(
        "SELECT * FROM sabcoin_deposits WHERE order_id=$1", order_id
    )


async def confirm_deposit(pool, order_id: str) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            dep = await conn.fetchrow(
                "SELECT * FROM sabcoin_deposits WHERE order_id=$1 AND status='pending' FOR UPDATE",
                order_id
            )
            if not dep:
                return {"success": False, "reason": "Deposit not found or already processed"}
            await conn.execute(
                "UPDATE users SET sabcoins = sabcoins + $2 WHERE id=$1",
                dep["user_id"], dep["coins_to_credit"]
            )
            await conn.execute("""
                UPDATE sabcoin_deposits
                SET status='credited', credited_at=NOW()
                WHERE order_id=$1
            """, order_id)
            return {"success": True, "user_id": dep["user_id"],
                    "coins": float(dep["coins_to_credit"])}


# ─── MARKETPLACE ──────────────────────────────────────────────────────────────

async def create_listing(pool, seller_id: int, inventory_id: int, price: float):
    async with pool.acquire() as conn:
        async with conn.transaction():
            claimed = await conn.fetchval(
                "UPDATE inventory SET in_use=TRUE WHERE id=$1 AND in_use=FALSE RETURNING id",
                inventory_id
            )
            if not claimed:
                return None
            return await conn.fetchval("""
                INSERT INTO marketplace_listings (seller_id, inventory_id, price_coins)
                VALUES ($1, $2, $3) RETURNING id
            """, seller_id, inventory_id, price)


async def get_listings(pool):
    return await pool.fetch("""
        SELECT l.id, l.seller_id, l.price_coins, l.created_at,
               u.username AS seller_name, u.avatar AS seller_avatar,
               b.name AS item_name, b.emoji, b.tier, b.image_url,
               m.name AS mutation, m.multiplier, i.traits,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS item_value
        FROM marketplace_listings l
        JOIN users u ON l.seller_id = u.id
        JOIN inventory i ON l.inventory_id = i.id
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE l.status = 'active'
        ORDER BY l.created_at DESC
    """)


async def buy_listing(pool, listing_id: int, buyer_id: int) -> dict:
    TAX = 0.15
    async with pool.acquire() as conn:
        async with conn.transaction():
            listing = await conn.fetchrow("""
                SELECT l.*, i.brainrot_id, i.mutation_id, i.traits
                FROM marketplace_listings l
                JOIN inventory i ON l.inventory_id = i.id
                WHERE l.id=$1 AND l.status='active'
                FOR UPDATE
            """, listing_id)
            if not listing:
                return {"success": False, "reason": "Listing not found or already sold"}
            if listing["seller_id"] == buyer_id:
                return {"success": False, "reason": "Cannot buy your own listing"}
            price = float(listing["price_coins"])
            bal = await conn.fetchval(
                "SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", buyer_id
            )
            if float(bal or 0) < price:
                return {"success": False, "reason": "Insufficient SabCoins"}
            seller_gets = round(price * (1 - TAX), 2)
            tax_burned = round(price * TAX, 2)
            await conn.execute(
                "UPDATE users SET sabcoins = sabcoins - $2 WHERE id=$1", buyer_id, price
            )
            await conn.execute(
                "UPDATE users SET sabcoins = sabcoins + $2 WHERE id=$1",
                listing["seller_id"], seller_gets
            )
            await conn.execute(
                "UPDATE inventory SET user_id=$1, in_use=FALSE WHERE id=$2",
                buyer_id, listing["inventory_id"]
            )
            await conn.execute(
                "UPDATE marketplace_listings SET status='sold' WHERE id=$1", listing_id
            )
            await conn.execute("""
                INSERT INTO marketplace_sales
                    (listing_id, seller_id, buyer_id, price_coins, seller_receives, tax_burned)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, listing_id, listing["seller_id"], buyer_id, price, seller_gets, tax_burned)
            return {"success": True, "item_name": None,
                    "seller_gets": seller_gets, "tax": tax_burned}


async def cancel_listing(pool, listing_id: int, user_id: int) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            listing = await conn.fetchrow(
                "SELECT * FROM marketplace_listings WHERE id=$1 AND seller_id=$2 AND status='active'",
                listing_id, user_id
            )
            if not listing:
                return False
            await conn.execute(
                "UPDATE marketplace_listings SET status='cancelled' WHERE id=$1", listing_id
            )
            await conn.execute(
                "UPDATE inventory SET in_use=FALSE WHERE id=$1", listing["inventory_id"]
            )
            return True


# ─── SABCOIN WITHDRAWALS ──────────────────────────────────────────────────────

async def create_withdrawal(pool, user_id: int, amount_coins: float,
                             amount_after_tax: float, tax_burned: float,
                             currency: str, address: str, order_id: str):
    async with pool.acquire() as conn:
        async with conn.transaction():
            bal = await conn.fetchval(
                "SELECT sabcoins FROM users WHERE id=$1 FOR UPDATE", user_id
            )
            if float(bal or 0) < amount_coins:
                return None
            await conn.execute(
                "UPDATE users SET sabcoins = sabcoins - $2 WHERE id=$1", user_id, amount_coins
            )
            wid = await conn.fetchval("""
                INSERT INTO sabcoin_withdrawals
                    (user_id, amount_coins, amount_after_tax, tax_burned, currency, address, order_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
            """, user_id, amount_coins, amount_after_tax, tax_burned, currency, address, order_id)
            return wid


# ─── ADMIN HELPERS ────────────────────────────────────────────────────────────

async def admin_add_coins(pool, user_id: int, amount: float):
    await pool.execute(
        "UPDATE users SET sabcoins = sabcoins + $2 WHERE id=$1", user_id, amount
    )


async def admin_ban_user(pool, user_id: int, banned: bool):
    await pool.execute("UPDATE users SET is_banned=$2 WHERE id=$1", user_id, banned)


async def admin_timeout_user(pool, user_id: int, minutes: int):
    import datetime
    if minutes <= 0:
        await pool.execute("UPDATE users SET timeout_until=NULL WHERE id=$1", user_id)
    else:
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
        await pool.execute("UPDATE users SET timeout_until=$2 WHERE id=$1", user_id, until)
