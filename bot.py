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

# Role names (created automatically on setup)
ADMIN_ROLE_NAMES = [
    "Owner",
    "Head of Operations",
    "Operations Manager",
    "Withdraw Staff",
    "Tipping",
]

STAFF_ROLE_NAMES = ADMIN_ROLE_NAMES

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

GREEN  = 0x22c55e
RED    = 0xef4444
GOLD   = 0xf59e0b
BLUE   = 0x3b82f6
PURPLE = 0x6c5ce7

# ─── BOT CLASS ────────────────────────────────────────────────────────────────

class SabPotBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.pool = None  # assigned in setup_hook

    async def setup_hook(self):
        # FIX: Pool is created HERE — inside the correct event loop.
        # This runs before the bot connects, so it is always on the right loop.
        self.pool = await db.init_pool()

        # Run schema (sequentially on a single connection to avoid concurrent-op warnings)
        try:
            schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
            with open(schema_path) as f:
                schema_sql = f.read()
            async with self.pool.acquire() as conn:
                await conn.execute(schema_sql)
        except Exception as e:
            print(f"⚠️ Schema error (non-fatal): {e}")

        # Sync slash commands
        try:
            synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
            print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
        except Exception as e:
            print(f"❌ Command sync failed: {e}")

    async def close(self):
        await super().close()
        if self.pool:
            await db.close_pool(self.pool)


bot = SabPotBot()
tree = bot.tree

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def has_staff_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_role_names = {r.name for r in member.roles}
    return bool(member_role_names & set(STAFF_ROLE_NAMES))

def has_tipping_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    tipping_roles = {"Owner", "Head of Operations", "Operations Manager", "Tipping"}
    return bool({r.name for r in member.roles} & tipping_roles)

def is_admin():
    async def predicate(interaction: discord.Interaction):
        return has_staff_role(interaction.user)
    return app_commands.check(predicate)

# ─── VIEWS ────────────────────────────────────────────────────────────────────

class BrainrotSelect(discord.ui.Select):
    def __init__(self, brainrots, placeholder="Select brainrot"):
        options = [
            discord.SelectOption(
                label=b['name'][:100],
                value=str(b['id']),
                emoji=b['emoji'],
                description=f"⬡{b['base_value']} | {b['tier'].upper()}"
            )
            for b in brainrots[:25]
        ]
        super().__init__(placeholder=placeholder, options=options)

class BrainrotSelectPage2(discord.ui.Select):
    def __init__(self, brainrots):
        options = [
            discord.SelectOption(
                label=b['name'][:100],
                value=str(b['id']),
                emoji=b['emoji'],
                description=f"⬡{b['base_value']} | {b['tier'].upper()}"
            )
            for b in brainrots[25:]
        ]
        super().__init__(placeholder="Select brainrot (page 2)", options=options)

class MutationSelect(discord.ui.Select):
    def __init__(self, mutations):
        options = [
            discord.SelectOption(
                label=m['name'],
                value=str(m['id']),
                description=f"{m['multiplier']}x multiplier"
            )
            for m in mutations
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

        self.br_select = BrainrotSelect(brainrots)
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

        self.br_select.callback = br_cb
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

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=3)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_brainrot or not self.selected_mutation:
            await interaction.response.send_message("Select a brainrot and mutation first!", ephemeral=True)
            return
        await self.callback_fn(interaction, self.target_user_id, self.selected_brainrot, self.selected_mutation, self.traits)
        self.stop()

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # FIX: get pool from bot instance, not from get_pool()
        pool = interaction.client.pool
        await db.close_ticket(pool, interaction.channel.id)
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

# ─── EVENTS ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            await auto_setup(guild)
        except Exception as e:
            print(f"⚠️ Auto-setup error: {e}")
    print(f"✅ SabPot bot ready as {bot.user}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Surface the real error in logs
    raise error

async def auto_setup(guild: discord.Guild):
    """Automatically create roles and log channels if they don't exist."""
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
                await guild.create_role(
                    name=role_name, color=color,
                    hoist=True, mentionable=True,
                    reason="SabPot auto-setup"
                )
                print(f"  ✅ Created role: {role_name}")
            except Exception as e:
                print(f"  ❌ Failed to create role {role_name}: {e}")

    existing_channels = {c.name for c in guild.text_channels}
    log_channels = [
        ("🪙coinflip",  "Coinflip game logs"),
        ("💥upgrader",  "Upgrader game logs"),
        ("🎁tipping",   "Tipping logs — restricted to staff"),
        ("🔐login",     "Site login logs"),
    ]

    category = discord.utils.get(guild.categories, name="SabPot Logs")
    if not category:
        try:
            category = await guild.create_category(
                "SabPot Logs",
                overwrites={guild.default_role: discord.PermissionOverwrite(read_messages=False)},
                reason="SabPot auto-setup"
            )
            print("  ✅ Created category: SabPot Logs")
        except Exception as e:
            print(f"  ❌ Failed to create category: {e}")
            category = None

    for ch_name, topic in log_channels:
        if ch_name not in existing_channels:
            try:
                ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
                for rn in STAFF_ROLE_NAMES:
                    r = discord.utils.get(guild.roles, name=rn)
                    if r:
                        ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

                if "tipping" in ch_name or "login" in ch_name:
                    ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
                    for rn in ["Owner", "Head of Operations", "Operations Manager", "Tipping"]:
                        r = discord.utils.get(guild.roles, name=rn)
                        if r:
                            ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

                await guild.create_text_channel(
                    ch_name, category=category,
                    topic=topic, overwrites=ow,
                    reason="SabPot auto-setup"
                )
                print(f"  ✅ Created channel: {ch_name}")
            except Exception as e:
                print(f"  ❌ Failed to create channel {ch_name}: {e}")

# ─── LOG HELPERS ─────────────────────────────────────────────────────────────

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

# ─── ADMIN: ADDITEM ───────────────────────────────────────────────────────────

@tree.command(name="additem", description="[ADMIN] Add a brainrot item to a user's inventory", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def additem(interaction: discord.Interaction, user: discord.Member):
    pool = interaction.client.pool  # FIX: pool from bot instance
    brainrots = await db.get_all_brainrots(pool)
    mutations = await db.get_all_mutations(pool)
    await db.ensure_user(pool, user.id, str(user), str(user.display_avatar))

    embed = discord.Embed(title=f"➕ Add Item to {user.display_name}", color=GREEN,
                          description="Select the brainrot, mutation, and traits below.")

    async def do_add(inter, target_uid, brainrot_id, mutation_id, traits):
        inv_id = await db.add_item_to_inventory(pool, target_uid, brainrot_id, mutation_id, traits)
        b = next(x for x in brainrots if x['id'] == brainrot_id)
        m = next(x for x in mutations if x['id'] == mutation_id)
        val = db.calc_value(float(b['base_value']), float(m['multiplier']), traits)
        result = discord.Embed(title="✅ Item Added", color=GREEN)
        result.add_field(name="Item",     value=f"{b['emoji']} **{b['name']}**", inline=True)
        result.add_field(name="Mutation", value=f"{m['name']} ({m['multiplier']}x)", inline=True)
        result.add_field(name="Traits",   value=str(traits), inline=True)
        result.add_field(name="Value",    value=f"⬡{db.format_value(val)}", inline=True)
        result.add_field(name="User",     value=f"<@{target_uid}>", inline=True)
        result.add_field(name="Inv ID",   value=str(inv_id), inline=True)
        await inter.response.edit_message(embed=result, view=None)
        log_ch = discord.utils.get(interaction.guild.text_channels, name="🪙coinflip") \
               or discord.utils.get(interaction.guild.text_channels, name="💥upgrader")
        if log_ch:
            await log_ch.send(embed=result)

    view = AddItemView(brainrots, mutations, user.id, do_add)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ─── ADMIN: REMOVEITEM ────────────────────────────────────────────────────────

class RemoveItemSelect(discord.ui.Select):
    def __init__(self, items, user, pool):
        self.pool = pool
        self.user = user
        options = [
            discord.SelectOption(
                label=f"{r['emoji']} {r['name']} [{r['mutation']}]{' +'+str(r['traits'])+'T' if r['traits'] else ''}",
                value=str(r['id']),
                description=f"⬡{db.format_value(float(r['value']))} — ID: {r['id']}"
            )
            for r in items[:25]
        ]
        super().__init__(placeholder="Select item to remove", options=options)

    async def callback(self, interaction: discord.Interaction):
        inv_id = int(self.values[0])
        item = next((r for r in self.view.items if r['id'] == inv_id), None)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        await db.remove_item_from_inventory(self.pool, inv_id)
        embed = discord.Embed(title="🗑️ Item Removed", color=RED)
        embed.add_field(name="Item",     value=f"{item['emoji']} {item['name']}", inline=True)
        embed.add_field(name="Mutation", value=item['mutation'], inline=True)
        embed.add_field(name="Value",    value=f"⬡{db.format_value(float(item['value']))}", inline=True)
        embed.add_field(name="From",     value=self.user.mention, inline=True)
        await interaction.response.edit_message(embed=embed, view=None)
        log_ch = discord.utils.get(interaction.guild.text_channels, name="🪙coinflip")
        if log_ch:
            await log_ch.send(embed=embed)

class RemoveItemView(discord.ui.View):
    def __init__(self, items, user, pool):
        super().__init__(timeout=120)
        self.items = items
        self.add_item(RemoveItemSelect(items, user, pool))

@tree.command(name="removeitem", description="[ADMIN] Remove an item from a user's inventory", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def removeitem(interaction: discord.Interaction, user: discord.Member):
    pool = interaction.client.pool  # FIX
    items = await pool.fetch("""
        SELECT i.id, b.name, b.emoji, m.name as mutation, i.traits,
               ROUND(b.base_value * m.multiplier * (1 + i.traits * 0.07), 2) as value
        FROM inventory i
        JOIN brainrots b ON i.brainrot_id = b.id
        JOIN mutations m ON i.mutation_id = m.id
        WHERE i.user_id = $1
        ORDER BY value DESC
    """, user.id)

    if not items:
        await interaction.response.send_message(f"{user.display_name} has no items.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🗑️ Remove Item from {user.display_name}",
        description=f"**{len(items)} item{'s' if len(items)!=1 else ''}** — select one to remove.",
        color=RED
    )
    await interaction.response.send_message(embed=embed, view=RemoveItemView(items, user, pool), ephemeral=True)

# ─── ADMIN: ADDBOTSTOCK ───────────────────────────────────────────────────────

@tree.command(name="addbotstock", description="[ADMIN] Add an item to the bot's upgrade stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def addbotstock(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX: this was the crashing line — now uses correct pool
    brainrots = await db.get_all_brainrots(pool)
    mutations = await db.get_all_mutations(pool)

    embed = discord.Embed(title="➕ Add to Bot Stock", color=GOLD,
                          description="Select brainrot, mutation, and traits for the bot stock item.")

    async def do_add_stock(inter, _, brainrot_id, mutation_id, traits):
        stock_id = await db.add_to_bot_stock(pool, brainrot_id, mutation_id, traits)
        b = next(x for x in brainrots if x['id'] == brainrot_id)
        m = next(x for x in mutations if x['id'] == mutation_id)
        val = db.calc_value(float(b['base_value']), float(m['multiplier']), traits)
        result = discord.Embed(title="✅ Added to Bot Stock", color=GOLD)
        result.add_field(name="Item",     value=f"{b['emoji']} **{b['name']}**", inline=True)
        result.add_field(name="Mutation", value=f"{m['name']} ({m['multiplier']}x)", inline=True)
        result.add_field(name="Traits",   value=str(traits), inline=True)
        result.add_field(name="Value",    value=f"⬡{db.format_value(val)}", inline=True)
        result.add_field(name="Stock ID", value=str(stock_id), inline=True)
        await inter.response.edit_message(embed=result, view=None)
        log_ch = discord.utils.get(interaction.guild.text_channels, name="🪙coinflip") \
               or discord.utils.get(interaction.guild.text_channels, name="💥upgrader")
        if log_ch:
            await log_ch.send(embed=result)

    view = AddItemView(brainrots, mutations, 0, do_add_stock)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ─── ADMIN: REMOVESTOCK ──────────────────────────────────────────────────────

class RemoveStockSelect(discord.ui.Select):
    def __init__(self, items, pool, guild_ref):
        self.pool = pool
        self.guild_ref = guild_ref
        options = [
            discord.SelectOption(
                label=f"{r['emoji']} {r['name']} [{r['mutation']}]{' +'+str(r['traits'])+'T' if r['traits'] else ''}",
                value=str(r['id']),
                description=f"⬡{db.format_value(float(r['value']))} — Stock ID: {r['id']}"
            )
            for r in items[:25]
        ]
        super().__init__(placeholder="Select stock item to remove", options=options)

    async def callback(self, interaction: discord.Interaction):
        stock_id = int(self.values[0])
        item = next((r for r in self.view.items if r['id'] == stock_id), None)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        await db.remove_from_bot_stock(self.pool, stock_id)
        embed = discord.Embed(title="🗑️ Removed from Bot Stock", color=RED)
        embed.add_field(name="Item",     value=f"{item['emoji']} {item['name']}", inline=True)
        embed.add_field(name="Mutation", value=item['mutation'], inline=True)
        embed.add_field(name="Value",    value=f"⬡{db.format_value(float(item['value']))}", inline=True)
        await interaction.response.edit_message(embed=embed, view=None)
        log_ch = discord.utils.get(self.guild_ref.text_channels, name="💥upgrader")
        if log_ch:
            await log_ch.send(embed=embed)

class RemoveStockView(discord.ui.View):
    def __init__(self, items, pool, guild_ref):
        super().__init__(timeout=120)
        self.items = items
        self.add_item(RemoveStockSelect(items, pool, guild_ref))

@tree.command(name="removestock", description="[ADMIN] Remove an item from bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def removestock(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX
    items = await pool.fetch("""
        SELECT s.id, b.name, b.emoji, m.name as mutation, s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) as value
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        ORDER BY value DESC
    """)

    if not items:
        await interaction.response.send_message("Bot stock is empty.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🗑️ Remove from Bot Stock",
        description=f"**{len(items)} item{'s' if len(items)!=1 else ''}** in stock — select one to remove.",
        color=RED
    )
    await interaction.response.send_message(embed=embed, view=RemoveStockView(items, pool, interaction.guild), ephemeral=True)

# ─── DEPOSIT ──────────────────────────────────────────────────────────────────

@tree.command(name="deposit", description="Open a deposit ticket", guild=discord.Object(id=GUILD_ID))
async def deposit(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX
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

    channel = await guild.create_text_channel(
        name=f"deposit-{interaction.user.name}",
        category=category,
        overwrites=overwrites
    )
    await db.create_ticket(pool, interaction.user.id, 'deposit', channel.id)

    embed = discord.Embed(title="📥 Deposit Ticket", color=GREEN)
    embed.description = (
        f"Welcome {interaction.user.mention}!\n\n"
        f"To deposit, please send:\n"
        f"**• Your Roblox username**\n"
        f"**• The items you want to deposit**\n\n"
        f"An admin will assist you shortly. 🙏"
    )
    embed.set_footer(text="Click the button below to close this ticket when done.")
    await channel.send(embed=embed, view=CloseTicketView())
    await interaction.response.send_message(f"✅ Deposit ticket created: {channel.mention}", ephemeral=True)

# ─── WITHDRAW ─────────────────────────────────────────────────────────────────

@tree.command(name="withdraw", description="Open a withdraw ticket", guild=discord.Object(id=GUILD_ID))
async def withdraw(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX
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

    channel = await guild.create_text_channel(
        name=f"withdraw-{interaction.user.name}",
        category=category,
        overwrites=overwrites
    )
    await db.create_ticket(pool, interaction.user.id, 'withdraw', channel.id)

    embed = discord.Embed(title="📤 Withdraw Ticket", color=GOLD)
    embed.description = (
        f"Welcome {interaction.user.mention}!\n\n"
        f"To withdraw, please send:\n"
        f"**• Your Roblox username**\n"
        f"**• Which items you want to withdraw**\n\n"
        f"An admin will assist you shortly. 🙏"
    )
    embed.set_footer(text="Click the button below to close this ticket when done.")
    await channel.send(embed=embed, view=CloseTicketView())
    await interaction.response.send_message(f"✅ Withdraw ticket created: {channel.mention}", ephemeral=True)

# ─── ADMIN: BOTSTOCK ──────────────────────────────────────────────────────────

@tree.command(name="botstock", description="[ADMIN] View all items currently in bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def botstock(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX
    rows = await pool.fetch("""
        SELECT s.id, b.name, b.emoji, b.tier, m.name AS mutation, m.multiplier,
               s.traits,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        ORDER BY value DESC
    """)

    if not rows:
        await interaction.response.send_message("Bot stock is currently empty.", ephemeral=True)
        return

    embed = discord.Embed(title="🤖 Bot Stock", color=BLUE)
    embed.description = f"**{len(rows)} item{'s' if len(rows) != 1 else ''}** in stock\n\u200b"

    chunk = 15
    for i in range(0, min(len(rows), 45), chunk):
        batch = rows[i:i+chunk]
        lines = []
        for r in batch:
            traits_str = f" · {r['traits']}T" if r['traits'] > 0 else ""
            mut_str    = f" [{r['mutation']}]" if r['mutation'] != 'Base' else ""
            lines.append(
                f"`#{r['id']}` {r['emoji']} **{r['name']}**{mut_str}{traits_str} — ⬡{db.format_value(float(r['value']))}"
            )
        embed.add_field(
            name=f"Items {i+1}–{min(i+chunk, len(rows))}",
            value="\n".join(lines),
            inline=False
        )

    if len(rows) > 45:
        embed.set_footer(text=f"Showing first 45 of {len(rows)} items")
    else:
        embed.set_footer(text=f"Total: {len(rows)} items")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── ADMIN: CREATEPROMO ───────────────────────────────────────────────────────

class PromoStockSelect(discord.ui.Select):
    def __init__(self, stock_items):
        options = [
            discord.SelectOption(
                label=f"{s['name']} [{s['mutation']}]{' +'+str(s['traits'])+'T' if s['traits'] > 0 else ''} ×{s['qty']}"[:100],
                value=f"{s['stock_id']}|{s['brainrot_id']}|{s['mutation_id']}|{s['traits']}",
                description=f"⬡{db.format_value(float(s['value']))} — qty: {s['qty']}{(' traits: '+str(s['traits'])) if s['traits'] > 0 else ''}"[:100]
            )
            for s in stock_items[:25]
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
        self.view.item_label = label
        await interaction.response.defer()

class RedeemModal(discord.ui.Modal, title="Set Promo Code Details"):
    max_redeems = discord.ui.TextInput(
        label="Max Redeems",
        placeholder="e.g. 10",
        min_length=1, max_length=4
    )
    custom_code = discord.ui.TextInput(
        label="Custom Code (leave blank for auto)",
        placeholder="e.g. SABPOT2025",
        required=False, max_length=20
    )

    def __init__(self, stock_id, brainrot_id, mutation_id, traits, item_label):
        super().__init__()
        self.stock_id    = stock_id
        self.brainrot_id = brainrot_id
        self.mutation_id = mutation_id
        self.traits      = traits
        self.item_label  = item_label

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_r = int(self.max_redeems.value)
            if max_r < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Invalid number of redeems.", ephemeral=True)
            return

        import random, string
        code = self.custom_code.value.strip().upper() if self.custom_code.value.strip() \
            else ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

        pool = interaction.client.pool  # FIX
        try:
            await db.create_promo(pool, code, self.stock_id, self.brainrot_id,
                                   self.mutation_id, self.traits, max_r)
        except Exception:
            await interaction.response.send_message("❌ Code already exists. Try a different one.", ephemeral=True)
            return

        embed = discord.Embed(title="✅ Promo Code Created", color=GREEN)
        embed.add_field(name="Code",     value=f"`{code}`", inline=True)
        embed.add_field(name="Item",     value=self.item_label, inline=True)
        embed.add_field(name="Max Uses", value=str(max_r), inline=True)
        embed.set_footer(text="Share this code with users to redeem on the site.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        log_ch = discord.utils.get(interaction.guild.text_channels, name="🔐login")
        if log_ch:
            await log_ch.send(embed=embed)

class PromoView(discord.ui.View):
    def __init__(self, stock_items):
        super().__init__(timeout=120)
        self.selected    = None
        self.stock_id    = None
        self.brainrot_id = None
        self.mutation_id = None
        self.traits      = None
        self.item_label  = None
        self.add_item(PromoStockSelect(stock_items))

    @discord.ui.button(label="Confirm & Set Redeems", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected:
            await interaction.response.send_message("❌ Please select an item first.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RedeemModal(self.stock_id, self.brainrot_id, self.mutation_id,
                        self.traits, self.item_label)
        )

@tree.command(name="createpromo", description="[ADMIN] Create a promo code from bot stock", guild=discord.Object(id=GUILD_ID))
@is_admin()
async def createpromo(interaction: discord.Interaction):
    pool = interaction.client.pool  # FIX
    rows = await pool.fetch("""
        SELECT MIN(s.id) AS stock_id, s.brainrot_id, s.mutation_id, s.traits,
               b.name, b.emoji, m.name AS mutation,
               ROUND(b.base_value * m.multiplier * (1 + s.traits * 0.07), 2) AS value,
               COUNT(*) AS qty
        FROM bot_stock s
        JOIN brainrots b ON s.brainrot_id = b.id
        JOIN mutations m ON s.mutation_id = m.id
        GROUP BY s.brainrot_id, s.mutation_id, s.traits, b.name, b.emoji, m.name,
                 b.base_value, m.multiplier
        ORDER BY value DESC
    """)

    if not rows:
        await interaction.response.send_message("❌ Bot stock is empty.", ephemeral=True)
        return

    stock_items = [dict(r) for r in rows]
    embed = discord.Embed(title="🎟️ Create Promo Code", color=GOLD,
                          description="Select an item from bot stock, then click **Confirm & Set Redeems**.")
    embed.add_field(
        name="Current Stock",
        value="\n".join(
            f"{s['emoji']} **{s['name']}** [{s['mutation']}]{' +'+str(s['traits'])+'T' if s['traits'] > 0 else ''} — ⬡{db.format_value(float(s['value']))} × {s['qty']}"
            for s in stock_items[:15]
        ) or "Empty",
        inline=False
    )
    await interaction.response.send_message(embed=embed, view=PromoView(stock_items), ephemeral=True)

# ─── RUN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
