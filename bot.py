import discord
from discord.ext import commands
from discord import app_commands
import os, asyncio
from dotenv import load_dotenv
import db

load_dotenv()

GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
TICKET_CATEGORY_ID = int(os.environ.get("TICKET_CATEGORY_ID", 0))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))

ADMIN_ROLE_NAMES = [
    "Owner", "Head of Operations", "Operations Manager", "Withdraw Staff", "Tipping",
]
STAFF_ROLE_NAMES = ADMIN_ROLE_NAMES

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

GREEN  = 0x22c55e
RED    = 0xef4444
GOLD   = 0xf59e0b
BLUE   = 0x3b82f6
PURPLE = 0x6c5ce7

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def has_staff_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool({r.name for r in member.roles} & set(STAFF_ROLE_NAMES))

def is_admin():
    async def predicate(interaction: discord.Interaction):
        return has_staff_role(interaction.user)
    return app_commands.check(predicate)

# BUG FIX: always get a fresh pool reference; never share bare connections.
async def safe_pool():
    return await db.get_pool()

# ─── VIEWS ────────────────────────────────────────────────────────────────────

class BrainrotSelect(discord.ui.Select):
    def __init__(self, brainrots, placeholder="Select brainrot (1-25)"):
        options = [
            discord.SelectOption(
                label=b["name"][:100], value=str(b["id"]),
                emoji=b["emoji"],
                description=f"base {b['base_value']} | {b['tier'].upper()}"
            ) for b in brainrots[:25]
        ]
        super().__init__(placeholder=placeholder, options=options)

class BrainrotSelectPage2(discord.ui.Select):
    def __init__(self, brainrots):
        options = [
            discord.SelectOption(
                label=b["name"][:100], value=str(b["id"]),
                emoji=b["emoji"],
                description=f"base {b['base_value']} | {b['tier'].upper()}"
            ) for b in brainrots[25:]
        ]
        super().__init__(placeholder="Select brainrot (26+)", options=options)

class MutationSelect(discord.ui.Select):
    def __init__(self, mutations):
        options = [
            discord.SelectOption(
                label=m["name"], value=str(m["id"]),
                description=f"{m['multiplier']}x multiplier"
            ) for m in mutations
        ]
        super().__init__(placeholder="Select mutation", options=options)

class AddItemView(discord.ui.View):
    def __init__(self, brainrots, mutations, target_user_id: int, callback_fn):
        super().__init__(timeout=180)
        self.selected_brainrot = None
        self.selected_mutation = None
        self.traits = 0
        self.target_user_id = target_user_id
        self.callback_fn = callback_fn
        self.brainrots = brainrots
        self.mutations = mutations

        self.br_select  = BrainrotSelect(brainrots)
        self.mut_select = MutationSelect(mutations)

        async def br_cb(interaction):
            self.selected_brainrot = int(self.br_select.values[0])
            await interaction.response.defer()

        async def br_cb2(interaction):
            self.selected_brainrot = int(self.br2_select.values[0])
            await interaction.response.defer()

        async def mut_cb(interaction):
            self.selected_mutation = int(self.mut_select.values[0])
            await interaction.response.defer()

        self.br_select.callback  = br_cb
        self.mut_select.callback = mut_cb
        self.add_item(self.br_select)

        if len(brainrots) > 25:
            self.br2_select = BrainrotSelectPage2(brainrots)
            self.br2_select.callback = br_cb2
            self.add_item(self.br2_select)

        self.add_item(self.mut_select)

    @discord.ui.button(label="Traits: 0", style=discord.ButtonStyle.secondary, row=3)
    async def trait_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.traits = (self.traits + 1) % 11
        button.label = f"Traits: {self.traits}"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=3)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_brainrot or not self.selected_mutation:
            await interaction.response.send_message("Select a brainrot AND mutation first!", ephemeral=True)
            return
        await self.callback_fn(interaction, self.target_user_id, self.selected_brainrot, self.selected_mutation, self.traits)
        self.stop()

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = await safe_pool()
        await db.close_ticket(pool, interaction.channel.id)
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

# ─── EVENTS ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    # BUG FIX: old code called pool.execute(schema) which shared a connection
    # with the pool-creation handshake causing "another operation is in progress".
    # Fix: acquire() a DEDICATED connection for schema execution.
    try:
        pool = await safe_pool()
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path) as f:
            schema_sql = f.read()
        async with pool.acquire() as conn:
            await conn.execute(schema_sql)
    except Exception as e:
        print(f"Schema error (non-fatal): {e}")

    try:
        synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    except Exception as e:
        print(f"Command sync failed: {e}")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            await auto_setup(guild)
        except Exception as e:
            print(f"Auto-setup error: {e}")

    print(f"SabPot bot ready as {bot.user}")

async def auto_setup(guild: discord.Guild):
    existing_roles = {r.name for r in guild.roles}
    role_colors = {
        "Owner":               discord.Color.from_rgb(255, 170, 0),
        "Head of Operations":  discord.Color.from_rgb(239, 68, 68),
        "Operations Manager":  discord.Color.from_rgb(108, 92, 231),
        "Withdraw Staff":      discord.Color.from_rgb(56, 189, 248),
        "Tipping":             discord.Color.from_rgb(34, 197, 94),
    }
    for role_name, color in role_colors.items():
        if role_name not in existing_roles:
            try:
                await guild.create_role(name=role_name, color=color, hoist=True, mentionable=True, reason="SabPot auto-setup")
            except Exception as e:
                print(f"Failed to create role {role_name}: {e}")

    existing_channels = {c.name for c in guild.text_channels}
    log_channels = [
        ("coinflip-logs",  "Coinflip game logs"),
        ("upgrader-logs",  "Upgrader game logs"),
        ("tipping-logs",   "Tipping logs"),
        ("login-logs",     "Site login logs"),
    ]
    category = discord.utils.get(guild.categories, name="SabPot Logs")
    if not category:
        try:
            category = await guild.create_category(
                "SabPot Logs",
                overwrites={guild.default_role: discord.PermissionOverwrite(read_messages=False)},
                reason="SabPot auto-setup"
            )
        except Exception as e:
            print(f"Failed to create category: {e}")
            category = None

    for ch_name, topic in log_channels:
        if ch_name not in existing_channels:
            try:
                ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
                for rn in STAFF_ROLE_NAMES:
                    r = discord.utils.get(guild.roles, name=rn)
                    if r:
                        ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
                await guild.create_text_channel(ch_name, category=category, topic=topic, overwrites=ow, reason="SabPot auto-setup")
            except Exception as e:
                print(f"Failed to create channel {ch_name}: {e}")

# ─── LOG HELPER ───────────────────────────────────────────────────────────────

async def log_to_channel(channel_name: str, embed: discord.Embed):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name=channel_name)
    if ch:
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

# ─── GLOBAL ERROR HANDLER ─────────────────────────────────────────────────────

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        except Exception:
            pass
    else:
        cmd_name = interaction.command.name if interaction.command else "unknown"
        print(f"Slash command error in /{cmd_name}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
        except Exception:
            pass

# ─── ADMIN: ADDITEM ───────────────────────────────────────────────────────────

@tree.command(name="additem", description="[ADMIN] Add a brainrot item to a user's inventory", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def additem(interaction: discord.Interaction, user: discord.Member):
    pool = await safe_pool()
    brainrots = await pool.fetch("SELECT * FROM brainrots ORDER BY base_value ASC, name ASC")
    mutations  = await pool.fetch("SELECT * FROM mutations ORDER BY multiplier ASC")
    await db.ensure_user(pool, user.id, str(user), str(user.display_avatar))

    embed = discord.Embed(title=f"Add Item to {user.display_name}", color=GREEN, description="Select brainrot, mutation, and traits below.")

    async def do_add(inter, target_uid, brainrot_id, mutation_id, traits):
        inv_id = await pool.fetchval(
            "INSERT INTO inventory (user_id, brainrot_id, mutation_id, traits) VALUES ($1,$2,$3,$4) RETURNING id",
            target_uid, brainrot_id, mutation_id, traits
        )
        b = next(x for x in brainrots if x["id"] == brainrot_id)
        m = next(x for x in mutations if x["id"] == mutation_id)
        val = db.calc_value(float(b["base_value"]), float(m["multiplier"]), traits)
        result = discord.Embed(title="Item Added", color=GREEN)
        result.add_field(name="Item",     value=f"{b['emoji']} **{b['name']}**",        inline=True)
        result.add_field(name="Mutation", value=f"{m['name']} ({m['multiplier']}x)",    inline=True)
        result.add_field(name="Traits",   value=str(traits),                            inline=True)
        result.add_field(name="Value",    value=f"{db.format_value(val)}",              inline=True)
        result.add_field(name="User",     value=f"<@{target_uid}>",                     inline=True)
        result.add_field(name="Inv ID",   value=str(inv_id),                            inline=True)
        await inter.response.edit_message(embed=result, view=None)
        await log_to_channel("coinflip-logs", result)

    await interaction.response.send_message(embed=embed, view=AddItemView(brainrots, mutations, user.id, do_add), ephemeral=True)

# ─── ADMIN: REMOVEITEM ────────────────────────────────────────────────────────

class RemoveItemSelect(discord.ui.Select):
    def __init__(self, items, user, pool):
        self.pool = pool
        self.user = user
        options = [
            discord.SelectOption(
                label=f"{r['emoji']} {r['name']} [{r['mutation']}]{' +'+str(r['traits'])+'T' if r['traits'] else ''}"[:100],
                value=str(r["id"]),
                description=f"{db.format_value(float(r['value']))} - ID:{r['id']}"
            ) for r in items[:25]
        ]
        super().__init__(placeholder="Select item to remove", options=options)

    async def callback(self, interaction: discord.Interaction):
        inv_id = int(self.values[0])
        item = next((r for r in self.view.items if r["id"] == inv_id), None)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        await db.remove_item_from_inventory(self.pool, inv_id)
        embed = discord.Embed(title="Item Removed", color=RED)
        embed.add_field(name="Item",     value=f"{item['emoji']} {item['name']}", inline=True)
        embed.add_field(name="Mutation", value=item["mutation"],                  inline=True)
        embed.add_field(name="Value",    value=db.format_value(float(item["value"])), inline=True)
        embed.add_field(name="From",     value=self.user.mention,                 inline=True)
        await interaction.response.edit_message(embed=embed, view=None)
        await log_to_channel("coinflip-logs", embed)

class RemoveItemView(discord.ui.View):
    def __init__(self, items, user, pool):
        super().__init__(timeout=120)
        self.items = items
        self.add_item(RemoveItemSelect(items, user, pool))

@tree.command(name="removeitem", description="[ADMIN] Remove an item from a user's inventory", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def removeitem(interaction: discord.Interaction, user: discord.Member):
    pool = await safe_pool()
    items = await pool.fetch("""
        SELECT i.id, b.name, b.emoji, m.name AS mutation, i.traits,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) AS value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.user_id = $1
        ORDER BY value DESC
    """, user.id)
    if not items:
        await interaction.response.send_message(f"{user.display_name} has no items.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Remove Item from {user.display_name}", description=f"**{len(items)} items** - select one.", color=RED)
    await interaction.response.send_message(embed=embed, view=RemoveItemView(items, user, pool), ephemeral=True)

# ─── ADMIN: ADDBOTSTOCK (BUG FIXED) ──────────────────────────────────────────

@tree.command(name="addbotstock", description="[ADMIN] Add an item to the bot's stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def addbotstock(interaction: discord.Interaction):
    # ROOT CAUSE FIX: the previous version fetched pool inside the callback,
    # which ran while the outer pool fetch was still active on the same connection.
    # Now we fetch pool ONCE at the start and reuse it.
    pool = await safe_pool()
    brainrots = await pool.fetch("SELECT * FROM brainrots ORDER BY base_value ASC, name ASC")
    mutations  = await pool.fetch("SELECT * FROM mutations ORDER BY multiplier ASC")

    embed = discord.Embed(title="Add to Bot Stock", color=GOLD, description="Select brainrot, mutation, and traits.")

    async def do_add_stock(inter, _, brainrot_id, mutation_id, traits):
        stock_id = await pool.fetchval(
            "INSERT INTO bot_stock (brainrot_id, mutation_id, traits) VALUES ($1,$2,$3) RETURNING id",
            brainrot_id, mutation_id, traits
        )
        b = next(x for x in brainrots if x["id"] == brainrot_id)
        m = next(x for x in mutations if x["id"] == mutation_id)
        val = db.calc_value(float(b["base_value"]), float(m["multiplier"]), traits)
        result = discord.Embed(title="Added to Bot Stock", color=GOLD)
        result.add_field(name="Item",     value=f"{b['emoji']} **{b['name']}**",     inline=True)
        result.add_field(name="Mutation", value=f"{m['name']} ({m['multiplier']}x)", inline=True)
        result.add_field(name="Traits",   value=str(traits),                         inline=True)
        result.add_field(name="Value",    value=db.format_value(val),                inline=True)
        result.add_field(name="Stock ID", value=str(stock_id),                       inline=True)
        await inter.response.edit_message(embed=result, view=None)
        await log_to_channel("upgrader-logs", result)

    await interaction.response.send_message(embed=embed, view=AddItemView(brainrots, mutations, 0, do_add_stock), ephemeral=True)

# ─── ADMIN: REMOVESTOCK ───────────────────────────────────────────────────────

class RemoveStockSelect(discord.ui.Select):
    def __init__(self, items, pool):
        self.pool = pool
        options = [
            discord.SelectOption(
                label=f"{r['emoji']} {r['name']} [{r['mutation']}]{' +'+str(r['traits'])+'T' if r['traits'] else ''}"[:100],
                value=str(r["id"]),
                description=f"{db.format_value(float(r['value']))} - Stock ID:{r['id']}"
            ) for r in items[:25]
        ]
        super().__init__(placeholder="Select stock item to remove", options=options)

    async def callback(self, interaction: discord.Interaction):
        stock_id = int(self.values[0])
        item = next((r for r in self.view.items if r["id"] == stock_id), None)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        await db.remove_from_bot_stock(self.pool, stock_id)
        embed = discord.Embed(title="Removed from Bot Stock", color=RED)
        embed.add_field(name="Item",     value=f"{item['emoji']} {item['name']}", inline=True)
        embed.add_field(name="Mutation", value=item["mutation"],                  inline=True)
        embed.add_field(name="Value",    value=db.format_value(float(item["value"])), inline=True)
        await interaction.response.edit_message(embed=embed, view=None)
        await log_to_channel("upgrader-logs", embed)

class RemoveStockView(discord.ui.View):
    def __init__(self, items, pool):
        super().__init__(timeout=120)
        self.items = items
        self.add_item(RemoveStockSelect(items, pool))

@tree.command(name="removestock", description="[ADMIN] Remove an item from bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def removestock(interaction: discord.Interaction):
    pool = await safe_pool()
    items = await pool.fetch("""
        SELECT s.id, b.name, b.emoji, m.name AS mutation, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        ORDER BY value DESC
    """)
    if not items:
        await interaction.response.send_message("Bot stock is empty.", ephemeral=True)
        return
    embed = discord.Embed(title="Remove from Bot Stock", description=f"**{len(items)} items** - select one.", color=RED)
    await interaction.response.send_message(embed=embed, view=RemoveStockView(items, pool), ephemeral=True)

# ─── ADMIN: ADDSCCOINS (NEW - was completely missing) ─────────────────────────

@tree.command(name="addsccoins", description="[ADMIN] Add SabCoins to a user's balance", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def addsccoins(interaction: discord.Interaction, user: discord.Member, amount: float):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    pool = await safe_pool()
    await db.ensure_user(pool, user.id, str(user), str(user.display_avatar))
    await db.credit_sabcoins(pool, user.id, amount)
    new_bal = await db.get_sabcoins(pool, user.id)
    embed = discord.Embed(title="SabCoins Added", color=GREEN)
    embed.add_field(name="User",        value=user.mention,                   inline=True)
    embed.add_field(name="Added",       value=db.format_value(amount),        inline=True)
    embed.add_field(name="New Balance", value=db.format_value(new_bal),       inline=True)
    await interaction.response.send_message(embed=embed)
    await log_to_channel("login-logs", embed)

# ─── ADMIN: REMOVESCCOINS ─────────────────────────────────────────────────────

@tree.command(name="removesccoins", description="[ADMIN] Remove SabCoins from a user", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def removesccoins(interaction: discord.Interaction, user: discord.Member, amount: float):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    pool = await safe_pool()
    success = await db.debit_sabcoins(pool, user.id, amount)
    if not success:
        bal = await db.get_sabcoins(pool, user.id)
        await interaction.response.send_message(f"Insufficient balance. {user.display_name} only has {db.format_value(bal)} SC.", ephemeral=True)
        return
    new_bal = await db.get_sabcoins(pool, user.id)
    embed = discord.Embed(title="SabCoins Removed", color=RED)
    embed.add_field(name="User",        value=user.mention,             inline=True)
    embed.add_field(name="Removed",     value=db.format_value(amount),  inline=True)
    embed.add_field(name="New Balance", value=db.format_value(new_bal), inline=True)
    await interaction.response.send_message(embed=embed)
    await log_to_channel("login-logs", embed)

# ─── ADMIN: SETSCCOINS ────────────────────────────────────────────────────────

@tree.command(name="setsccoins", description="[ADMIN] Set a user's SabCoins to exact amount", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def setsccoins(interaction: discord.Interaction, user: discord.Member, amount: float):
    if amount < 0:
        await interaction.response.send_message("Amount cannot be negative.", ephemeral=True)
        return
    pool = await safe_pool()
    await db.ensure_user(pool, user.id, str(user), str(user.display_avatar))
    await pool.execute("UPDATE users SET sabcoins = $2 WHERE id = $1", user.id, amount)
    embed = discord.Embed(title="SabCoins Set", color=GOLD)
    embed.add_field(name="User",    value=user.mention,            inline=True)
    embed.add_field(name="Balance", value=db.format_value(amount), inline=True)
    await interaction.response.send_message(embed=embed)
    await log_to_channel("login-logs", embed)

# ─── ADMIN: BOTSTOCK ──────────────────────────────────────────────────────────

@tree.command(name="botstock", description="[ADMIN] View all items in bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def botstock(interaction: discord.Interaction):
    pool = await safe_pool()
    rows = await pool.fetch("""
        SELECT s.id, b.name, b.emoji, b.tier, m.name AS mutation, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        ORDER BY value DESC
    """)
    if not rows:
        await interaction.response.send_message("Bot stock is empty.", ephemeral=True)
        return
    embed = discord.Embed(title="Bot Stock", color=BLUE, description=f"**{len(rows)} items** in stock\n\u200b")
    chunk = 15
    for i in range(0, min(len(rows), 45), chunk):
        batch = rows[i:i+chunk]
        lines = []
        for r in batch:
            t = f" {r['traits']}T" if r["traits"] > 0 else ""
            mu = f" [{r['mutation']}]" if r["mutation"] != "Base" else ""
            lines.append(f"`#{r['id']}` {r['emoji']} **{r['name']}**{mu}{t} - {db.format_value(float(r['value']))}")
        embed.add_field(name=f"Items {i+1}-{min(i+chunk, len(rows))}", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Total: {len(rows)} items")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── ADMIN: CREATEPROMO ───────────────────────────────────────────────────────

class PromoStockSelect(discord.ui.Select):
    def __init__(self, stock_items):
        options = [
            discord.SelectOption(
                label=f"{s['name']} [{s['mutation']}]{' +'+str(s['traits'])+'T' if s['traits'] > 0 else ''} x{s['qty']}"[:100],
                value=f"{s['stock_id']}|{s['brainrot_id']}|{s['mutation_id']}|{s['traits']}",
                description=f"{db.format_value(float(s['value']))} qty:{s['qty']}"[:100]
            ) for s in stock_items[:25]
        ]
        super().__init__(placeholder="Select item for promo code", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected = self.values[0]
        parts = self.values[0].split("|")
        self.view.stock_id    = int(parts[0])
        self.view.brainrot_id = int(parts[1])
        self.view.mutation_id = int(parts[2])
        self.view.traits      = int(parts[3])
        label = self.options[[o.value for o in self.options].index(self.values[0])].label
        self.view.item_label  = label
        await interaction.response.defer()

class RedeemModal(discord.ui.Modal, title="Set Promo Code Details"):
    max_redeems = discord.ui.TextInput(label="Max Redeems", placeholder="e.g. 10", min_length=1, max_length=4)
    custom_code = discord.ui.TextInput(label="Custom Code (blank = auto)", placeholder="e.g. SABPOT2025", required=False, max_length=20)

    def __init__(self, stock_id, brainrot_id, mutation_id, traits, item_label):
        super().__init__()
        self.stock_id = stock_id; self.brainrot_id = brainrot_id
        self.mutation_id = mutation_id; self.traits = traits; self.item_label = item_label

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_r = int(self.max_redeems.value)
            if max_r < 1: raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid number.", ephemeral=True)
            return
        import random, string
        code = self.custom_code.value.strip().upper() if self.custom_code.value.strip() \
            else "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        pool = await safe_pool()
        try:
            await db.create_promo(pool, code, self.stock_id, self.brainrot_id, self.mutation_id, self.traits, max_r)
        except Exception:
            await interaction.response.send_message("Code already exists. Try a different one.", ephemeral=True)
            return
        embed = discord.Embed(title="Promo Code Created", color=GREEN)
        embed.add_field(name="Code",     value=f"`{code}`",     inline=True)
        embed.add_field(name="Item",     value=self.item_label, inline=True)
        embed.add_field(name="Max Uses", value=str(max_r),      inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await log_to_channel("login-logs", embed)

class PromoView(discord.ui.View):
    def __init__(self, stock_items):
        super().__init__(timeout=120)
        self.selected = self.stock_id = self.brainrot_id = self.mutation_id = self.traits = self.item_label = None
        self.add_item(PromoStockSelect(stock_items))

    @discord.ui.button(label="Confirm & Set Redeems", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected:
            await interaction.response.send_message("Select an item first.", ephemeral=True)
            return
        await interaction.response.send_modal(RedeemModal(self.stock_id, self.brainrot_id, self.mutation_id, self.traits, self.item_label))

@tree.command(name="createpromo", description="[ADMIN] Create a promo code from bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def createpromo(interaction: discord.Interaction):
    pool = await safe_pool()
    rows = await pool.fetch("""
        SELECT MIN(s.id) AS stock_id, s.brainrot_id, s.mutation_id, s.traits,
               b.name, b.emoji, m.name AS mutation, m.id AS mutation_id,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value,
               COUNT(*) AS qty
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        GROUP BY s.brainrot_id, s.mutation_id, s.traits, b.name, b.emoji, m.name, m.id, b.base_value, m.multiplier
        ORDER BY value DESC
    """)
    if not rows:
        await interaction.response.send_message("Bot stock is empty.", ephemeral=True)
        return
    stock_items = [dict(r) for r in rows]
    embed = discord.Embed(title="Create Promo Code", color=GOLD, description="Select an item from bot stock.")
    embed.add_field(
        name="Current Stock",
        value="\n".join(
            f"{s['emoji']} **{s['name']}** [{s['mutation']}]{' +'+str(s['traits'])+'T' if s['traits']>0 else ''} - {db.format_value(float(s['value']))} x{s['qty']}"
            for s in stock_items[:15]
        ) or "Empty",
        inline=False
    )
    await interaction.response.send_message(embed=embed, view=PromoView(stock_items), ephemeral=True)

# ─── DEPOSIT / WITHDRAW ───────────────────────────────────────────────────────

async def _make_ticket(interaction: discord.Interaction, ticket_type: str):
    pool = await safe_pool()
    await db.ensure_user(pool, interaction.user.id, str(interaction.user), str(interaction.user.display_avatar))
    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user:   discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for rn in STAFF_ROLE_NAMES:
        r = discord.utils.get(guild.roles, name=rn)
        if r:
            overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    channel = await guild.create_text_channel(name=f"{ticket_type}-{interaction.user.name}", category=category, overwrites=overwrites)
    await db.create_ticket(pool, interaction.user.id, ticket_type, channel.id)
    return channel

@tree.command(name="deposit", description="Open a deposit ticket", guild=discord.Object(id=GUILD_ID))
async def deposit(interaction: discord.Interaction):
    channel = await _make_ticket(interaction, "deposit")
    embed = discord.Embed(title="Deposit Ticket", color=GREEN)
    embed.description = f"Welcome {interaction.user.mention}!\n\nSend your Roblox username and items to deposit.\nAn admin will assist you shortly."
    await channel.send(embed=embed, view=CloseTicketView())
    await interaction.response.send_message(f"Deposit ticket created: {channel.mention}", ephemeral=True)

@tree.command(name="withdraw", description="Open a withdraw ticket", guild=discord.Object(id=GUILD_ID))
async def withdraw(interaction: discord.Interaction):
    channel = await _make_ticket(interaction, "withdraw")
    embed = discord.Embed(title="Withdraw Ticket", color=GOLD)
    embed.description = f"Welcome {interaction.user.mention}!\n\nSend your Roblox username and which items to withdraw.\nAn admin will assist you shortly."
    await channel.send(embed=embed, view=CloseTicketView())
    await interaction.response.send_message(f"Withdraw ticket created: {channel.mention}", ephemeral=True)

# ─── RUN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
