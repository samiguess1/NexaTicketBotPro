import os
import re
import io
import json
import ssl
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import certifi
import aiohttp
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import discord
import yarl
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# Nexa.pm endpoint overrides requested by the project owner.
# Compatibility depends on Nexa.pm implementing these Discord-compatible endpoints.
BASE = "https://nexa.pm"
discord.http.Route.BASE = BASE + "/api/v10"
discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(
    "wss://nexa.pm/bot-gateway/"
)
discord.Asset.BASE = BASE + "/cdn"

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = PROJECT_DIR / "tickets.sqlite3"
TRANSCRIPT_DIR = PROJECT_DIR / "transcripts"
AUTHORIZED_SENDER_ID = 1568707704443109376
AUTO_CLOSE_HOURS = int(os.getenv("AUTO_CLOSE_HOURS", "72"))
MAX_TICKETS_PER_USER = int(os.getenv("MAX_TICKETS_PER_USER", "3"))

os.makedirs(TRANSCRIPT_DIR, exist_ok=True)

if not TOKEN or TOKEN == "PASTE_YOUR_NEXA_BOT_TOKEN_HERE":
    raise RuntimeError("Add your Nexa.pm bot token to the BOT_TOKEN value in .env")

# ---------------- Database ----------------

db = sqlite3.connect(DB_PATH, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")
db.executescript("""
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    panel_channel_id INTEGER,
    category_id INTEGER,
    staff_role_id INTEGER,
    log_channel_id INTEGER,
    panel_message_id INTEGER,
    panel_title TEXT DEFAULT 'Support Center',
    panel_description TEXT DEFAULT 'Choose a ticket type below to contact our team.'
);
CREATE TABLE IF NOT EXISTS ticket_types (
    guild_id INTEGER NOT NULL,
    type_key TEXT NOT NULL,
    label TEXT NOT NULL,
    category_id INTEGER,
    PRIMARY KEY (guild_id, type_key)
);
CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    ticket_number INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    ticket_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by INTEGER,
    created_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    closed_at TEXT,
    rating INTEGER,
    feedback TEXT
);
CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anti_react_settings (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);
""")
ticket_columns = {row["name"] for row in db.execute("PRAGMA table_info(tickets)")}
if "delete_at" not in ticket_columns:
    db.execute("ALTER TABLE tickets ADD COLUMN delete_at TEXT")
db.commit()
ticket_creation_locks = {}
reaction_purge_tasks = {}
reaction_purge_started_guilds = set()
reaction_cleanup_tasks = {}
reaction_cleanup_again = set()

def utcnow():
    return datetime.now(timezone.utc)

def stamp():
    return utcnow().isoformat()

def config(guild_id):
    return db.execute(
        "SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)
    ).fetchone()

def ticket(channel_id):
    row = db.execute(
        "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
    ).fetchone()
    if row:
        return row

    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return None
    marker = re.fullmatch(
        r"Nexa ticket #(\d+) \| owner_id=(\d+) \| type=([a-z0-9_-]+)",
        channel.topic or "",
        flags=re.IGNORECASE
    )
    if marker is None:
        return None

    ticket_number, owner_id, type_key = marker.groups()
    created_at = channel.created_at.isoformat()
    is_closed = channel.name.startswith("closed-")
    db.execute(
        """INSERT OR IGNORE INTO tickets(
            channel_id,guild_id,ticket_number,owner_id,ticket_type,status,
            created_at,last_activity,closed_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            channel.id, channel.guild.id, int(ticket_number), int(owner_id), type_key,
            "closed" if is_closed else "open", created_at, created_at,
            stamp() if is_closed else None
        )
    )
    db.commit()
    return db.execute(
        "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
    ).fetchone()

def log_event(channel_id, actor_id, action):
    db.execute(
        "INSERT INTO ticket_events(channel_id,actor_id,action,created_at) VALUES(?,?,?,?)",
        (channel_id, actor_id, action, stamp())
    )
    db.commit()

def staff_check(member, cfg):
    if member.guild_permissions.administrator:
        return True
    role_id = cfg["staff_role_id"] if cfg else None
    return bool(role_id and any(r.id == role_id for r in member.roles))

async def resolve_member(guild, member_id):
    member = guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(member_id)
    except discord.NotFound:
        return None

def resolve_channel_input(guild, value, expected_type):
    raw_value = value.strip()
    if raw_value.startswith("#"):
        raw_value = raw_value[1:].strip()
    mention = re.fullmatch(r"<#(\d+)>", raw_value)
    channel_id = int(mention.group(1)) if mention else int(raw_value) if raw_value.isdigit() else None
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, expected_type) else None

    matches = [
        channel for channel in guild.channels
        if isinstance(channel, expected_type) and channel.name.casefold() == raw_value.casefold()
    ]
    return matches[0] if len(matches) == 1 else None

async def resolve_member_input(guild, value):
    raw_value = value.strip()
    mention = re.fullmatch(r"<@!?(\d+)>", raw_value)
    member_id = int(mention.group(1)) if mention else int(raw_value) if raw_value.isdigit() else None
    if member_id is not None:
        try:
            return await resolve_member(guild, member_id)
        except discord.HTTPException:
            return None

    matches = [
        member for member in guild.members
        if raw_value.casefold() in {
            member.name.casefold(), member.display_name.casefold(),
            (member.nick or "").casefold(),
            (getattr(member, "global_name", None) or "").casefold()
        }
    ]
    return matches[0] if len(matches) == 1 else None

def get_or_create_config(guild_id):
    cfg = config(guild_id)
    if not cfg:
        db.execute("INSERT INTO guild_config(guild_id) VALUES(?)", (guild_id,))
        db.commit()
        cfg = config(guild_id)
    return cfg

def safe_slug(value):
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug[:18] or "member")

# ---------------- Ticket UI ----------------

TICKET_TYPES = [
    ("general", "General Support", "💬"),
    ("bug", "Bug Report", "🐛"),
    ("appeal", "Appeal", "⚖️"),
    ("purchase", "Purchase Help", "🛒"),
]

class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji,
                                 description=f"Open a {label.lower()} ticket")
            for key, label, emoji in TICKET_TYPES
        ]
        super().__init__(
            placeholder="Choose what you need help with…",
            min_values=1, max_values=1, options=options,
            custom_id="nexa_pro:ticket_type"
        )

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())

class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary,
                       custom_id="nexa_pro:claim")
    async def claim_button(self, interaction, button):
        await claim_ticket(interaction)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="nexa_pro:close")
    async def close_button(self, interaction, button):
        await interaction.response.send_modal(CloseReasonModal())

class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Why are you closing this ticket?",
        placeholder="Enter a short reason",
        min_length=1,
        max_length=500,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await close_ticket(interaction, reason=str(self.reason.value).strip())

class RatingView(discord.ui.View):
    def __init__(self, channel_id, owner_id):
        super().__init__(timeout=86400)
        self.channel_id = channel_id
        self.owner_id = owner_id
        for score in range(1, 6):
            self.add_item(RatingButton(score, channel_id, owner_id))

class RatingButton(discord.ui.Button):
    def __init__(self, score, channel_id, owner_id):
        super().__init__(label=f"{score} ⭐", style=discord.ButtonStyle.secondary,
                         custom_id=f"nexa_pro:rate:{channel_id}:{score}")
        self.score = score
        self.channel_id = channel_id
        self.owner_id = owner_id

    async def callback(self, interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "Only the ticket owner can rate this support experience.", ephemeral=True
            )
        db.execute("UPDATE tickets SET rating=? WHERE channel_id=?",
                   (self.score, self.channel_id))
        db.commit()
        await interaction.response.edit_message(
            content=f"Thank you! You rated this ticket {self.score}/5 ⭐.",
            view=None
        )

# ---------------- Ticket creation ----------------

async def create_ticket(interaction, type_key):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "Tickets can only be opened inside a server.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    lock_key = interaction.guild_id
    lock = ticket_creation_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        await _create_ticket_locked(interaction, type_key)

async def send_ticket_followup(interaction, content):
    try:
        await retry_rate_limited(
            lambda: interaction.edit_original_response(content=content)
        )
        return
    except (discord.HTTPException, discord.RateLimited) as exc:
        print(f"Ticket interaction response edit failed: {exc}")
    try:
        await retry_rate_limited(
            lambda: interaction.followup.send(content, ephemeral=True)
        )
    except (discord.HTTPException, discord.RateLimited) as fallback_exc:
        print(f"Ticket interaction follow-up failed: {fallback_exc}")

async def _create_ticket_locked(interaction, type_key):
    guild = interaction.guild
    if guild is None:
        return await send_ticket_followup(interaction, "Tickets can only be opened inside a server.")

    cfg = config(guild.id)
    if not cfg or not cfg["staff_role_id"] or not cfg["category_id"]:
        return await send_ticket_followup(
            interaction,
            "The ticket system is not fully configured. Ask an administrator to run /ticket-setup."
        )

    recent_cutoff = (utcnow() - timedelta(seconds=30)).isoformat()
    recent_ticket = db.execute(
        "SELECT channel_id FROM tickets WHERE guild_id=? AND owner_id=? AND status='open' "
        "AND created_at>=? ORDER BY created_at DESC LIMIT 1",
        (guild.id, interaction.user.id, recent_cutoff)
    ).fetchone()
    if recent_ticket:
        existing_channel = guild.get_channel(recent_ticket["channel_id"])
        location = existing_channel.mention if existing_channel else "your newly created ticket"
        return await send_ticket_followup(
            interaction, f"Your ticket was just created: {location}. I reused it to avoid creating a duplicate."
        )

    existing_count = db.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE guild_id=? AND owner_id=? AND status='open'",
        (guild.id, interaction.user.id)
    ).fetchone()["n"]
    if existing_count >= MAX_TICKETS_PER_USER:
        return await send_ticket_followup(
            interaction,
            f"You already have {existing_count} open tickets (limit: {MAX_TICKETS_PER_USER})."
        )

    type_row = db.execute(
        "SELECT * FROM ticket_types WHERE guild_id=? AND type_key=?",
        (guild.id, type_key)
    ).fetchone()
    if not type_row:
        return await send_ticket_followup(interaction, "That ticket type is not configured.")

    category_id = type_row["category_id"] or cfg["category_id"]
    category = guild.get_channel(category_id)
    staff_role = guild.get_role(cfg["staff_role_id"])
    bot_member = guild.me

    if not isinstance(category, discord.CategoryChannel) or staff_role is None or bot_member is None:
        return await send_ticket_followup(
            interaction,
            "A configured category, staff role, or bot member is missing. Please check /ticket-config.",
        )

    number = db.execute(
        "SELECT COALESCE(MAX(ticket_number),0)+1 AS n FROM tickets WHERE guild_id=?",
        (guild.id,)
    ).fetchone()["n"]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True
        ),
        staff_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, attach_files=True
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True
        )
    }

    channel_name = f"ticket-{number:04d}-{safe_slug(interaction.user.name)}"
    try:
        channel = await retry_rate_limited(
            lambda: guild.create_text_channel(
                name=channel_name, category=category, overwrites=overwrites,
                topic=f"Nexa ticket #{number} | owner_id={interaction.user.id} | type={type_key}"
            )
        )
    except discord.Forbidden:
        return await send_ticket_followup(
            interaction,
            "I don't have permission to create private channels. Check Manage Channels and role hierarchy.",
        )
    except (discord.HTTPException, discord.RateLimited) as exc:
        print(f"Ticket channel creation failed in server {guild.id} ({type(exc).__name__}): {exc}")
        return await send_ticket_followup(
            interaction, "Nexa couldn't create the ticket channel after retrying a temporary API error."
        )

    db.execute("""
        INSERT INTO tickets(channel_id,guild_id,ticket_number,owner_id,ticket_type,status,created_at,last_activity)
        VALUES(?,?,?,?,?,'open',?,?)
    """, (channel.id, guild.id, number, interaction.user.id, type_key, stamp(), stamp()))
    db.commit()
    try:
        log_event(channel.id, interaction.user.id, "created")
    except sqlite3.Error as exc:
        print(f"Could not record creation event for ticket channel {channel.id}: {exc}")

    label = dict((k, v) for k, v, _ in TICKET_TYPES).get(type_key, type_key.title())
    embed = discord.Embed(
        title=f"🎫 {label} — Ticket #{number:04d}",
        description=(
            f"Hello {interaction.user.mention}! Your private ticket is ready.\n\n"
            "Please describe your request and include any useful details or screenshots.\n"
            "A staff member will be with you soon."
        ),
        color=discord.Color.blurple(),
        timestamp=utcnow()
    )
    embed.add_field(name="Ticket owner", value=interaction.user.mention, inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)
    embed.add_field(name="Assigned staff", value="Unclaimed", inline=True)
    try:
        await retry_rate_limited(
            lambda: channel.send(
                content=f"{interaction.user.mention} {staff_role.mention}",
                embed=embed, view=TicketControls(), allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=False
                )
            )
        )
    except (discord.HTTPException, discord.RateLimited) as exc:
        try:
            log_event(channel.id, interaction.user.id, f"created; opening post failed: {type(exc).__name__}")
        except sqlite3.Error:
            pass
        return await send_ticket_followup(
            interaction,
            f"Your ticket channel was created: {channel.mention}, but I couldn't post its welcome message. "
            "You can still use the channel; staff can close it with /close."
        )
    await send_ticket_followup(interaction, f"Your ticket is open: {channel.mention}")

async def claim_ticket(interaction):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id) if interaction.guild else None
    if not row or row["status"] != "open":
        return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
    if not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Only support staff can claim tickets.", ephemeral=True)
    if row["claimed_by"]:
        return await interaction.response.send_message(
            f"This ticket is already claimed by <@{row['claimed_by']}>.", ephemeral=True
        )
    db.execute("UPDATE tickets SET claimed_by=?,last_activity=? WHERE channel_id=?",
               (interaction.user.id, stamp(), interaction.channel_id))
    db.commit()
    log_event(interaction.channel_id, interaction.user.id, "claimed")
    await interaction.response.send_message(f"🙋 {interaction.user.mention} claimed this ticket.")

async def make_transcript(channel):
    lines = [
        f"Transcript for #{channel.name}",
        f"Server: {channel.guild.name} ({channel.guild.id})",
        f"Channel ID: {channel.id}",
        f"Generated: {utcnow().isoformat()}",
        "=" * 72,
        ""
    ]
    async for msg in channel.history(limit=None, oldest_first=True):
        created = msg.created_at.replace(tzinfo=timezone.utc).isoformat()
        content = msg.clean_content or "[no text content]"
        lines.append(f"[{created}] {msg.author} ({msg.author.id}): {content}")
        for attachment in msg.attachments:
            lines.append(f"  Attachment: {attachment.filename} — {attachment.url}")
        for embed in msg.embeds:
            if embed.title or embed.description:
                lines.append(f"  Embed: {embed.title or ''} {embed.description or ''}")
    return "\n".join(lines).encode("utf-8")

async def dm_ticket_rating(guild, owner_id, channel_id, ticket_number):
    try:
        owner = await resolve_member(guild, owner_id)
    except discord.HTTPException as exc:
        print(f"Could not resolve ticket owner {owner_id} for rating DM: {exc}")
        return False
    if owner is None:
        return False

    embed = discord.Embed(
        title=f"How was your support? Ticket #{ticket_number:04d}",
        description="Please rate your ticket experience from 1 to 5 stars.",
        color=discord.Color.blurple()
    )
    try:
        await owner.send(embed=embed, view=RatingView(channel_id, owner_id))
    except discord.HTTPException as exc:
        print(f"Could not DM ticket rating to owner {owner_id}: {exc}")
        return False
    return True

async def close_ticket(interaction, reason):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id) if interaction.guild else None
    if not row or row["status"] != "open":
        return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
    is_owner = interaction.user.id == row["owner_id"]
    if not is_owner and not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Only the ticket owner or support staff can close it.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    try:
        transcript_bytes = await make_transcript(channel)
    except discord.Forbidden as exc:
        print(f"Transcript history denied for channel {channel.id}: {exc}")
        return await interaction.followup.send(
            "Nexa denied access to this ticket's message history, so I couldn't create the transcript. "
            "Check this channel's Read Message History permission for NexTickets.",
            ephemeral=True
        )
    filename = f"ticket-{row['ticket_number']:04d}-{channel.id}.txt"
    path = os.path.join(TRANSCRIPT_DIR, filename)
    with open(path, "wb") as f:
        f.write(transcript_bytes)

    delete_at = utcnow() + timedelta(seconds=30)
    db.execute(
        "UPDATE tickets SET status='closed',closed_at=?,last_activity=?,delete_at=? WHERE channel_id=?",
        (stamp(), stamp(), delete_at.isoformat(), channel.id)
    )
    db.commit()
    log_event(channel.id, interaction.user.id, "closed: " + reason[:150])

    try:
        await channel.edit(name=("closed-" + channel.name)[:100])
    except discord.HTTPException as exc:
        print(f"Could not rename closed ticket channel {channel.id}: {exc}")

    log_channel = channel.guild.get_channel(cfg["log_channel_id"]) if cfg and cfg["log_channel_id"] else None
    if isinstance(log_channel, discord.TextChannel):
        try:
            await log_channel.send(
                content=f"🔒 Ticket #{row['ticket_number']:04d} closed by {interaction.user.mention}.",
                file=discord.File(io.BytesIO(transcript_bytes), filename=filename)
            )
        except discord.HTTPException as exc:
            print(f"Could not send transcript log for ticket channel {channel.id}: {exc}")

    try:
        await channel.send(
            f"Ticket has been closed, channel will be deleted in 30 seconds. :lock:\n"
            f"Reason: {reason[:500]}\n\nRate your support experience:",
            view=RatingView(channel.id, row["owner_id"])
        )
    except discord.HTTPException as exc:
        print(f"Could not post close notice for ticket channel {channel.id}: {exc}")
    await dm_ticket_rating(channel.guild, row["owner_id"], channel.id, row["ticket_number"])
    await interaction.followup.send(
        "Ticket closed and transcript saved. The channel is scheduled for deletion in 30 seconds.",
        ephemeral=True
    )

# ---------------- Bot setup ----------------

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.reactions = True

class NexaTicketBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControls())
        await self.tree.sync()
        if not auto_close_tickets.is_running():
            auto_close_tickets.start()
        if not delete_closed_tickets.is_running():
            delete_closed_tickets.start()

bot = NexaTicketBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print("=" * 48)
    print(f"Connected as {bot.user}")
    print("Nexa.pm Ticket Bot PRO is online")
    print(f"Auto-close after: {AUTO_CLOSE_HOURS} hours")
    print("=" * 48)
    for row in db.execute(
        "SELECT guild_id,panel_channel_id,panel_message_id FROM guild_config "
        "WHERE panel_channel_id IS NOT NULL AND panel_message_id IS NOT NULL"
    ):
        channel = bot.get_channel(row["panel_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            message = await retry_rate_limited(
                lambda: channel.fetch_message(row["panel_message_id"])
            )
            is_ticket_panel = any(
                embed.title == "🎫 Support Center" for embed in message.embeds
            )
            if bot.user and message.author.id == bot.user.id and is_ticket_panel and message.content:
                await retry_rate_limited(
                    lambda: message.edit(content=None, view=TicketPanelView())
                )
                print(f"Removed stale text from ticket panel message {message.id}.")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, discord.RateLimited) as exc:
            print(f"Could not repair ticket panel message {row['panel_message_id']}: {exc}")

    for guild in bot.guilds:
        setting = db.execute(
            "SELECT enabled FROM anti_react_settings WHERE guild_id=?",
            (guild.id,)
        ).fetchone()
        if setting and setting["enabled"] and guild.id not in reaction_purge_started_guilds:
            reaction_purge_started_guilds.add(guild.id)
            reaction_purge_tasks[guild.id] = asyncio.create_task(
                purge_existing_reactions(guild)
            )

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    row = ticket(message.channel.id)
    if row and row["status"] == "open":
        db.execute("UPDATE tickets SET last_activity=? WHERE channel_id=?",
                   (stamp(), message.channel.id))
        db.commit()
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id is None or (bot.user and payload.user_id == bot.user.id):
        return
    setting = db.execute(
        "SELECT enabled FROM anti_react_settings WHERE guild_id=?",
        (payload.guild_id,)
    ).fetchone()
    if not setting or not setting["enabled"]:
        return
    key = (payload.guild_id, payload.channel_id, payload.message_id)
    task = reaction_cleanup_tasks.get(key)
    if task is not None and not task.done():
        reaction_cleanup_again.add(key)
        return
    reaction_cleanup_tasks[key] = asyncio.create_task(
        clear_reaction_burst(*key)
    )

async def retry_rate_limited(operation):
    for attempt in range(6):
        try:
            return await operation()
        except discord.RateLimited as exc:
            if attempt == 5:
                raise
            delay = exc.retry_after
        except discord.HTTPException as exc:
            if exc.status != 429 or attempt == 5:
                raise
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", {})
            retry_after = headers.get("Retry-After") or headers.get("X-RateLimit-Reset-After")
            if not retry_after:
                try:
                    retry_after = json.loads(exc.text).get("retry_after")
                except (TypeError, ValueError, AttributeError):
                    retry_after = None
            delay = float(retry_after) if retry_after is not None else min(30, 2 ** (attempt + 1))
        await asyncio.sleep(max(1, delay))

async def clear_reaction_burst(guild_id, channel_id, message_id):
    key = (guild_id, channel_id, message_id)
    try:
        await asyncio.sleep(0.25)
        while True:
            reaction_cleanup_again.discard(key)
            setting = db.execute(
                "SELECT enabled FROM anti_react_settings WHERE guild_id=?",
                (guild_id,)
            ).fetchone()
            if not setting or not setting["enabled"]:
                return
            channel = bot.get_channel(channel_id) or await retry_rate_limited(
                lambda: bot.fetch_channel(channel_id)
            )
            message = await retry_rate_limited(lambda: channel.fetch_message(message_id))
            if message.reactions:
                await retry_rate_limited(message.clear_reactions)
            await asyncio.sleep(0.25)
            if key not in reaction_cleanup_again:
                return
    except discord.Forbidden:
        print(f"Anti-react-spam could not clear reactions in channel {channel_id}: missing permissions")
    except (discord.RateLimited, discord.HTTPException) as exc:
        print(f"Anti-react-spam failed for message {message_id}: {exc}")
    finally:
        if reaction_cleanup_tasks.get(key) is asyncio.current_task():
            reaction_cleanup_tasks.pop(key, None)
        reaction_cleanup_again.discard(key)

async def purge_existing_reactions(guild):
    bot_member = guild.me
    if bot_member is None:
        return

    channels = [*guild.text_channels, *guild.threads]
    semaphore = asyncio.Semaphore(2)

    async def purge_channel(channel):
        permissions = channel.permissions_for(bot_member)
        if not (permissions.view_channel and permissions.read_message_history and permissions.manage_messages):
            return 0, 1

        removed = 0
        failed = 0
        async with semaphore:
            try:
                async for message in channel.history(limit=None, oldest_first=False):
                    setting = db.execute(
                        "SELECT enabled FROM anti_react_settings WHERE guild_id=?",
                        (guild.id,)
                    ).fetchone()
                    if not setting or not setting["enabled"]:
                        return removed, failed
                    if not message.reactions:
                        continue
                    try:
                        reaction_count = sum(reaction.count for reaction in message.reactions)
                        await retry_rate_limited(message.clear_reactions)
                        removed += reaction_count
                    except discord.Forbidden:
                        return removed, failed + 1
                    except (discord.RateLimited, discord.HTTPException) as exc:
                        failed += 1
                        print(f"Anti-react-spam could not clear message {message.id}: {exc}")
            except (discord.RateLimited, discord.HTTPException) as exc:
                failed += 1
                print(f"Anti-react-spam could not scan channel {channel.id}: {exc}")
        return removed, failed

    results = await asyncio.gather(*(purge_channel(channel) for channel in channels))
    removed = sum(count for count, _ in results)
    skipped = sum(errors for _, errors in results)
    print(
        f"Anti-react-spam cleanup finished for server {guild.id}: "
        f"removed {removed} reactions; {skipped} channels or messages were skipped."
    )

@bot.tree.command(
    name="anti-react-spam",
    description="Toggle automatic removal of new reactions in this server."
)
@app_commands.check(
    lambda interaction: (
        interaction.user.id == AUTHORIZED_SENDER_ID
        or interaction.user.guild_permissions.administrator
    )
)
async def anti_react_spam(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )

    setting = db.execute(
        "SELECT enabled FROM anti_react_settings WHERE guild_id=?",
        (guild.id,)
    ).fetchone()
    enabled = not bool(setting["enabled"]) if setting else True
    db.execute(
        "INSERT INTO anti_react_settings(guild_id,enabled) VALUES(?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
        (guild.id, int(enabled))
    )
    db.commit()

    if enabled:
        task = reaction_purge_tasks.get(guild.id)
        if task is None or task.done():
            task = asyncio.create_task(purge_existing_reactions(guild))
            reaction_purge_tasks[guild.id] = task
            reaction_purge_started_guilds.add(guild.id)
        message = (
            "Anti-react-spam enabled. New reactions are removed immediately, and existing reactions "
            "are being cleared from accessible text channels and active threads."
        )
    else:
        task = reaction_purge_tasks.pop(guild.id, None)
        if task and not task.done():
            task.cancel()
        reaction_purge_started_guilds.discard(guild.id)
        message = "Anti-react-spam disabled. New reactions will no longer be removed."
    await interaction.response.send_message(message, ephemeral=True)

@bot.tree.command(name="say", description="Send a message as NexTickets in this channel.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message="Text to send in this channel")
async def say_as_bot(interaction: discord.Interaction, message: str):
    if interaction.guild is None or interaction.channel is None:
        return await interaction.response.send_message(
            "This command can only be used in a server channel.", ephemeral=True
        )
    if len(message) > 2000:
        return await interaction.response.send_message(
            "Messages must be 2000 characters or fewer.", ephemeral=True
        )
    await interaction.channel.send(
        message,
        allowed_mentions=discord.AllowedMentions.none()
    )
    await interaction.response.send_message("Message sent as NexTickets.", ephemeral=True)

@bot.tree.command(name="say-in", description="Send a private request for NexTickets to post in a channel.")
@app_commands.describe(channel="Channel name, mention, or ID", message="Message for NexTickets to post")
async def say_in_channel(interaction: discord.Interaction, channel: str, message: str):
    if interaction.user.id != AUTHORIZED_SENDER_ID:
        return await interaction.response.send_message(
            "You aren't allowed to use this command.", ephemeral=True
        )
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "Use this command inside the server where the message should be posted.",
            ephemeral=True
        )
    target = resolve_channel_input(guild, channel, discord.TextChannel)
    if target is None:
        return await interaction.response.send_message(
            "I couldn't resolve that to one text channel. Use its mention, ID, or exact unique name.",
            ephemeral=True
        )
    if len(message) > 2000:
        return await interaction.response.send_message(
            "Messages must be 2000 characters or fewer.", ephemeral=True
        )
    if not target.permissions_for(interaction.user).view_channel:
        return await interaction.response.send_message(
            "You need access to the target channel to send through NexTickets.", ephemeral=True
        )
    bot_member = guild.me
    if bot_member is None or not target.permissions_for(bot_member).send_messages:
        return await interaction.response.send_message(
            "NexTickets can't send messages in that channel.", ephemeral=True
        )
    try:
        await target.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        return await interaction.response.send_message(
            f"Nexa rejected the message (HTTP {exc.status}).", ephemeral=True
        )
    await interaction.response.send_message(f"Posted in {target.mention}.", ephemeral=True)

@bot.tree.command(name="ticket-setup", description="Configure the ticket system.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(panel_channel="Choose where the ticket panel will be posted",
                       category="Choose the category for new ticket channels",
                       staff_role="Choose the role that can manage tickets",
                       log_channel="Optional channel for transcripts")
async def ticket_setup(interaction: discord.Interaction,
                       panel_channel: discord.TextChannel,
                       category: discord.CategoryChannel,
                       staff_role: discord.Role,
                       log_channel: discord.TextChannel = None):
    guild_id = interaction.guild_id
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    bot_member = guild.me
    if bot_member is None:
        return await interaction.response.send_message(
            "I couldn't verify my membership in this server. Please try again.",
            ephemeral=True
        )
    await interaction.response.defer(ephemeral=True)
    panel_permissions = panel_channel.permissions_for(bot_member)
    missing_permissions = [
        name for name, granted in (
            ("View Channel", panel_permissions.view_channel),
            ("Send Messages", panel_permissions.send_messages),
            ("Embed Links", panel_permissions.embed_links),
        )
        if not granted
    ]
    if missing_permissions:
        missing = ", ".join(missing_permissions)
        return await send_ticket_followup(
            interaction,
            f"Nexa still denies {missing} in {panel_channel.mention}. "
            "Check the channel's permission overrides, then rerun /ticket-setup. "
            "No ticket settings were changed."
        )

    embed = discord.Embed(
        title="🎫 Support Center",
        description="Select a ticket type from the menu below. A private channel will be created for you.",
        color=discord.Color.blurple()
    )
    try:
        msg = await panel_channel.send(embed=embed, view=TicketPanelView())
    except discord.Forbidden:
        return await send_ticket_followup(
            interaction,
            f"Nexa denied the ticket panel post in {panel_channel.mention}. "
            "Check its View Channel, Send Messages, and Embed Links permissions."
        )
    except discord.HTTPException as exc:
        return await send_ticket_followup(
            interaction, f"Nexa couldn't post the ticket panel (HTTP {exc.status})."
        )
    db.execute("""
        INSERT INTO guild_config(guild_id,panel_channel_id,category_id,staff_role_id,log_channel_id)
        VALUES(?,?,?,?,?)
        ON CONFLICT(guild_id) DO UPDATE SET
        panel_channel_id=excluded.panel_channel_id,
        category_id=excluded.category_id,
        staff_role_id=excluded.staff_role_id,
        log_channel_id=excluded.log_channel_id
        """, (guild_id, panel_channel.id, category.id, staff_role.id,
            log_channel.id if log_channel else None))
    for key, label, _ in TICKET_TYPES:
        db.execute("""
            INSERT OR IGNORE INTO ticket_types(guild_id,type_key,label,category_id)
            VALUES(?,?,?,NULL)
        """, (guild_id, key, label))
    db.commit()
    db.execute("UPDATE guild_config SET panel_message_id=? WHERE guild_id=?",
               (msg.id, guild_id))
    db.commit()
    await send_ticket_followup(
        interaction,
        f"Setup complete!\nPanel: {panel_channel.mention}\nCategory: **{category.name}**\n"
        f"Staff role: {staff_role.mention}\nLogs: {log_channel.mention if log_channel else 'Disabled'}"
    )

@bot.tree.command(name="ticket-category", description="Set a category for one ticket type.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(ticket_type=[
    app_commands.Choice(name="General Support", value="general"),
    app_commands.Choice(name="Bug Report", value="bug"),
    app_commands.Choice(name="Appeal", value="appeal"),
    app_commands.Choice(name="Purchase Help", value="purchase")
])
@app_commands.describe(category="Choose the category for this ticket type")
async def ticket_category(interaction: discord.Interaction,
                          ticket_type: app_commands.Choice[str],
                          category: discord.CategoryChannel):
    cfg = config(interaction.guild_id)
    if not cfg:
        return await interaction.response.send_message("Run /ticket-setup first.", ephemeral=True)
    db.execute("""
        INSERT INTO ticket_types(guild_id,type_key,label,category_id) VALUES(?,?,?,?)
        ON CONFLICT(guild_id,type_key) DO UPDATE SET category_id=excluded.category_id
    """, (interaction.guild_id, ticket_type.value, ticket_type.name, category.id))
    db.commit()
    await interaction.response.send_message(
        f"{ticket_type.name} tickets will now be created in **{category.name}**.",
        ephemeral=True
    )

@bot.tree.command(name="ticket-config", description="Show ticket system configuration.")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_config(interaction: discord.Interaction):
    cfg = config(interaction.guild_id)
    if not cfg:
        return await interaction.response.send_message("Ticket system is not configured.", ephemeral=True)
    panel = interaction.guild.get_channel(cfg["panel_channel_id"]) if cfg["panel_channel_id"] else None
    cat = interaction.guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
    role = interaction.guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
    logs = interaction.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
    embed = discord.Embed(title="⚙️ Ticket Configuration", color=discord.Color.blurple())
    embed.add_field(name="Panel", value=panel.mention if panel else "Not set", inline=False)
    embed.add_field(name="Default category", value=cat.name if cat else "Not set", inline=True)
    embed.add_field(name="Staff role", value=role.mention if role else "Not set", inline=True)
    embed.add_field(name="Transcript logs", value=logs.mention if logs else "Disabled", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ticket-panel", description="Post another ticket panel.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Choose where to post the ticket panel")
async def ticket_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not config(interaction.guild_id):
        return await interaction.response.send_message("Run /ticket-setup first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="🎫 Support Center",
        description="Select a ticket type from the menu below to open a private ticket.",
        color=discord.Color.blurple()
    )
    try:
        await channel.send(embed=embed, view=TicketPanelView())
    except discord.Forbidden:
        return await send_ticket_followup(
            interaction, f"I can't post a panel in {channel.mention}."
        )
    except discord.HTTPException as exc:
        return await send_ticket_followup(
            interaction, f"Nexa couldn't post the ticket panel (HTTP {exc.status})."
        )
    await send_ticket_followup(interaction, f"Panel posted in {channel.mention}.")

# ---------------- Staff / ticket commands ----------------

@bot.tree.command(name="claim", description="Claim the current ticket.")
async def claim(interaction: discord.Interaction):
    await claim_ticket(interaction)

@bot.tree.command(name="close", description="Close the current ticket and save a transcript.")
@app_commands.describe(reason="Why this ticket is being closed")
async def close(interaction: discord.Interaction, reason: str):
    reason = reason.strip()
    if not reason:
        return await interaction.response.send_message("Please provide a close reason.", ephemeral=True)
    await close_ticket(interaction, reason=reason)

@bot.tree.command(name="add", description="Add a member to this ticket.")
@app_commands.describe(member="Member name, mention, or ID")
async def add_member(interaction: discord.Interaction, member: str):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id)
    if not row or row["status"] != "open":
        return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
    if not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    member_obj = await resolve_member_input(interaction.guild, member)
    if member_obj is None:
        return await interaction.response.send_message(
            "I couldn't resolve that member. Use their exact unique name, mention, or ID.", ephemeral=True
        )
    try:
        await interaction.channel.set_permissions(
            member_obj, view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True
        )
    except discord.Forbidden:
        return await interaction.response.send_message("Nexa denied the channel permission change.", ephemeral=True)
    log_event(interaction.channel_id, interaction.user.id, f"added member {member_obj.id}")
    await interaction.response.send_message(f"Added {member_obj.mention} to the ticket.")

@bot.tree.command(name="remove", description="Remove a member from this ticket.")
@app_commands.describe(member="Member name, mention, or ID")
async def remove_member(interaction: discord.Interaction, member: str):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id)
    if not row or row["status"] != "open":
        return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
    if not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    member_obj = await resolve_member_input(interaction.guild, member)
    if member_obj is None:
        return await interaction.response.send_message(
            "I couldn't resolve that member. Use their exact unique name, mention, or ID.", ephemeral=True
        )
    if member_obj.id == row["owner_id"]:
        return await interaction.response.send_message("You cannot remove the ticket owner.", ephemeral=True)
    try:
        await interaction.channel.set_permissions(member_obj, overwrite=None)
    except discord.Forbidden:
        return await interaction.response.send_message("Nexa denied the channel permission change.", ephemeral=True)
    log_event(interaction.channel_id, interaction.user.id, f"removed member {member_obj.id}")
    await interaction.response.send_message(f"Removed {member_obj.mention} from the ticket.")

@bot.tree.command(name="transfer", description="Assign this ticket to another staff member.")
@app_commands.describe(staff_member="Staff member name, mention, or ID")
async def transfer(interaction: discord.Interaction, staff_member: str):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id)
    if not row or row["status"] != "open":
        return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
    staff_member_obj = await resolve_member_input(interaction.guild, staff_member)
    if staff_member_obj is None:
        return await interaction.response.send_message(
            "I couldn't resolve that member. Use their exact unique name, mention, or ID.", ephemeral=True
        )
    if not staff_check(interaction.user, cfg) or not staff_check(staff_member_obj, cfg):
        return await interaction.response.send_message("Only support staff can transfer tickets to support staff.", ephemeral=True)
    db.execute("UPDATE tickets SET claimed_by=?,last_activity=? WHERE channel_id=?",
               (staff_member_obj.id, stamp(), interaction.channel_id))
    db.commit()
    log_event(interaction.channel_id, interaction.user.id, f"transferred to {staff_member_obj.id}")
    await interaction.response.send_message(f"Ticket assigned to {staff_member_obj.mention}.")

@bot.tree.command(name="ticket-stats", description="Show ticket statistics.")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_stats(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    total = db.execute("SELECT COUNT(*) n FROM tickets WHERE guild_id=?", (guild_id,)).fetchone()["n"]
    opened = db.execute("SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND status='open'", (guild_id,)).fetchone()["n"]
    closed = db.execute("SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND status='closed'", (guild_id,)).fetchone()["n"]
    avg = db.execute("SELECT AVG(rating) n FROM tickets WHERE guild_id=? AND rating IS NOT NULL", (guild_id,)).fetchone()["n"]
    embed = discord.Embed(title="📊 Ticket Statistics", color=discord.Color.blurple())
    embed.add_field(name="Total tickets", value=str(total), inline=True)
    embed.add_field(name="Open", value=str(opened), inline=True)
    embed.add_field(name="Closed", value=str(closed), inline=True)
    embed.add_field(name="Average rating", value=f"{avg:.2f}/5" if avg is not None else "No ratings yet", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ticket-reopen", description="Reopen a closed ticket.")
async def ticket_reopen(interaction: discord.Interaction):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id)
    if not row or row["status"] != "closed":
        return await interaction.response.send_message("This isn't a closed ticket.", ephemeral=True)
    if not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    try:
        owner = await resolve_member(interaction.guild, row["owner_id"])
    except discord.HTTPException as exc:
        return await interaction.response.send_message(
            f"I couldn't look up the ticket owner, so I left the ticket closed ({exc}).",
            ephemeral=True
        )
    if owner is None:
        return await interaction.response.send_message(
            "The ticket owner is no longer in this server, so I can't reopen the ticket for them.",
            ephemeral=True
        )
    try:
        await interaction.channel.set_permissions(
            owner, view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "Nexa wouldn't let me restore the ticket owner's channel access. The ticket remains closed.",
            ephemeral=True
        )
    db.execute("UPDATE tickets SET status='open',closed_at=NULL,last_activity=? WHERE channel_id=?",
               (stamp(), interaction.channel_id))
    db.commit()
    await interaction.channel.edit(name=interaction.channel.name.removeprefix("closed-")[:100])
    log_event(interaction.channel_id, interaction.user.id, "reopened")
    await interaction.response.send_message(f"🔓 Reopened by {interaction.user.mention}.")

@bot.tree.command(name="ticket-delete", description="Delete a closed ticket channel.")
async def ticket_delete(interaction: discord.Interaction):
    row = ticket(interaction.channel_id)
    cfg = config(interaction.guild_id)
    if not row or row["status"] != "closed":
        return await interaction.response.send_message("Only closed ticket channels can be deleted.", ephemeral=True)
    if not staff_check(interaction.user, cfg):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    channel = interaction.channel
    await interaction.response.send_message(
        "This closed ticket channel will be deleted in 30 seconds."
    )
    await asyncio.sleep(30)
    try:
        await channel.delete(reason=f"Ticket deleted by {interaction.user}")
    except discord.HTTPException as exc:
        print(f"Could not delete closed ticket channel {channel.id}: {exc}")
        await interaction.followup.send(
            "Nexa couldn't delete this ticket channel. Check the bot's Manage Channels permission.",
            ephemeral=True
        )

# ---------------- Automatic inactive-ticket close ----------------

@tasks.loop(minutes=15)
async def auto_close_tickets():
    if AUTO_CLOSE_HOURS <= 0:
        return
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if (channel.topic or "").startswith("Nexa ticket #"):
                ticket(channel.id)

    cutoff = utcnow() - timedelta(hours=AUTO_CLOSE_HOURS)
    rows = db.execute(
        "SELECT * FROM tickets WHERE status='open' AND last_activity < ?",
        (cutoff.isoformat(),)
    ).fetchall()
    for row in rows:
        guild = bot.get_guild(row["guild_id"])
        if not guild:
            continue
        channel = guild.get_channel(row["channel_id"])
        if not channel:
            continue
        try:
            transcript = await make_transcript(channel)
            filename = f"ticket-{row['ticket_number']:04d}-{channel.id}-auto-closed.txt"
            with open(os.path.join(TRANSCRIPT_DIR, filename), "wb") as f:
                f.write(transcript)
            db.execute(
                "UPDATE tickets SET status='closed',closed_at=?,last_activity=? WHERE channel_id=?",
                (stamp(), stamp(), channel.id)
            )
            db.commit()
            log_event(channel.id, bot.user.id if bot.user else 0, "auto-closed for inactivity")
            cfg = config(guild.id)
            log_channel = guild.get_channel(cfg["log_channel_id"]) if cfg and cfg["log_channel_id"] else None
            if isinstance(log_channel, discord.TextChannel):
                await log_channel.send(
                    f"⏰ Auto-closed inactive ticket #{row['ticket_number']:04d}.",
                    file=discord.File(io.BytesIO(transcript), filename=filename)
                )
            await channel.send("⏰ This ticket was automatically closed due to inactivity.")
            await channel.edit(name=("closed-" + channel.name)[:100])
            await dm_ticket_rating(guild, row["owner_id"], channel.id, row["ticket_number"])
        except Exception as exc:
            print(
                f"Auto-close failed for channel {row['channel_id']} "
                f"({type(exc).__name__}): {exc}"
            )

@auto_close_tickets.before_loop
async def before_auto_close():
    await bot.wait_until_ready()

@tasks.loop(seconds=5)
async def delete_closed_tickets():
    rows = db.execute(
        "SELECT channel_id,guild_id FROM tickets WHERE status='closed' AND delete_at IS NOT NULL AND delete_at<=?",
        (stamp(),)
    ).fetchall()
    for row in rows:
        guild = bot.get_guild(row["guild_id"])
        channel = guild.get_channel(row["channel_id"]) if guild else None
        if channel is None:
            db.execute("UPDATE tickets SET delete_at=NULL WHERE channel_id=?", (row["channel_id"],))
            db.commit()
            continue
        try:
            await channel.delete(reason="Closed ticket reached its 30-second deletion deadline")
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            print(f"Scheduled ticket deletion failed for channel {row['channel_id']}: {exc}")
            continue
        db.execute("UPDATE tickets SET delete_at=NULL WHERE channel_id=?", (row["channel_id"],))
        db.commit()

@delete_closed_tickets.before_loop
async def before_delete_closed_tickets():
    await bot.wait_until_ready()

# ---------------- Error handling ----------------

@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need Administrator permissions to use that command."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You don't have permission to use that command."
    else:
        original = getattr(error, "original", error)
        message = f"Command error: {original}"
        print(f"Slash command error: {original}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

async def run_bot():
    retry_delay = 5
    while True:
        try:
            bot.http.connector = aiohttp.TCPConnector(
                ssl=ssl.create_default_context(cafile=certifi.where())
            )
            async with bot:
                await bot.start(TOKEN)
        except discord.ConnectionClosed as exc:
            if exc.code != 4004:
                raise
            print("Gateway rejected the resumed session; starting a fresh session.")
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status not in (500, 502, 503, 504):
                raise
            print(f"Nexa gateway returned HTTP {exc.status}; retrying in {retry_delay} seconds.")
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as exc:
            print(f"Nexa gateway connection failed ({exc}); retrying in {retry_delay} seconds.")

        bot.clear()
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


asyncio.run(run_bot())
