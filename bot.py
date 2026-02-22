import os
import sqlite3
import random
import time
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
import io

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD TOKEN MISSING")

DB_PATH = "bot.db"

STAFF_ROLE_ID     = 1474823002490405016
MEMBER_ROLE_ID    = 1474826229520793840
BOOSTER_ROLE_ID   = 1469733875709378674
BOOSTER_ROLE_2_ID = 1471590464279810210

COOLDOWN_SECONDS  = 800   # base cooldown for members
BOOSTER_COOLDOWN  = 400   # half cooldown for boosters
STAFF_COOLDOWN    = 0     # no cooldown for staff

SERVICES = [
    "steam", "xbox", "minecraft", "roblox",
    "crunchyroll", "nordvpn", "netflix", "disney",
    "hotmail", "capcut", "spotify"
]
# ==========================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DATABASE =================
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        cur = con.cursor()
        for svc in SERVICES:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {svc}_accounts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    password TEXT,
                    extra    TEXT DEFAULT ''
                )
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id  INTEGER,
                service  TEXT,
                last_gen INTEGER,
                PRIMARY KEY (user_id, service)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                account TEXT,
                service TEXT,
                reason  TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                owner_id INTEGER,
                code     TEXT UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_uses (
                user_id INTEGER UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vouches (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                message   TEXT,
                timestamp TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gens (
                user_id INTEGER,
                service TEXT,
                ts      INTEGER
            )
        """)

# ================= HELPERS =================
def has_role(member, role_id):
    return any(r.id == role_id for r in member.roles)

def is_staff(member):
    return has_role(member, STAFF_ROLE_ID)

def is_member(member):
    return has_role(member, MEMBER_ROLE_ID) or is_staff(member)

def has_referral(user_id):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM referral_uses WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def get_cooldown(member):
    """Return cooldown in seconds for this member."""
    if is_staff(member):
        return STAFF_COOLDOWN
    boosts = sum([has_role(member, BOOSTER_ROLE_ID), has_role(member, BOOSTER_ROLE_2_ID)])
    cd = COOLDOWN_SECONDS
    if boosts >= 1:
        cd = BOOSTER_COOLDOWN
    if has_referral(member.id):
        cd = max(0, cd - 60)  # referral bonus: -60 seconds
    return cd

def get_remaining_cooldown(user_id, service, cooldown_secs):
    """Returns seconds remaining on cooldown, or 0 if ready."""
    if cooldown_secs == 0:
        return 0
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT last_gen FROM cooldowns WHERE user_id=? AND service=?",
            (user_id, service)
        )
        row = cur.fetchone()
    if not row:
        return 0
    elapsed = int(time.time()) - row[0]
    remaining = cooldown_secs - elapsed
    return max(0, remaining)

def set_cooldown(user_id, service):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO cooldowns (user_id, service, last_gen) VALUES (?,?,?)",
            (user_id, service, int(time.time()))
        )

def format_time(seconds):
    """Format seconds into mm:ss or hh:mm:ss."""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def staff_check(interaction: discord.Interaction):
    return is_staff(interaction.user)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)

# ================= FILE PARSERS =================
def parse_steam_file(text: str):
    results = []
    lines = [l.rstrip() for l in text.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if "|" in line and ":" in line.split("|")[0]:
            creds, games = line.split("|", 1)
            user, pwd = creds.split(":", 1)
            if user.strip() and pwd.strip():
                results.append((user.strip(), pwd.strip(), games.strip()))
            i += 1
            continue
        norm = line.replace(" - ", "|")
        if "|" in norm and ":" in norm.split("|")[0]:
            creds, games = norm.split("|", 1)
            user, pwd = creds.split(":", 1)
            if user.strip() and pwd.strip() and games.strip():
                results.append((user.strip(), pwd.strip(), games.strip()))
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip():
            block.append(lines[i].strip())
            i += 1
        cred_idx = None
        for j, bl in enumerate(block):
            if ":" in bl:
                user_part = bl.split(":", 1)[0]
                if user_part and " " not in user_part:
                    cred_idx = j
                    break
        if cred_idx is None:
            continue
        game_lines = [bl for bl in block[:cred_idx] if bl]
        cred_line = block[cred_idx]
        user, pwd = cred_line.split(":", 1)
        user, pwd = user.strip(), pwd.strip()
        if not user or not pwd:
            continue
        games = ", ".join(game_lines) if game_lines else ""
        results.append((user, pwd, games))
    return results


def parse_simple_file(text: str):
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        user, pwd = line.split(":", 1)
        if user.strip() and pwd.strip():
            results.append((user.strip(), pwd.strip(), ""))
    return results

# ================= EMBEDS =================
SERVICE_COLORS = {
    "steam":       discord.Color.blue(),
    "xbox":        discord.Color.green(),
    "minecraft":   discord.Color.from_rgb(98, 56, 27),
    "roblox":      discord.Color.from_rgb(226, 35, 26),
    "crunchyroll": discord.Color.from_rgb(255, 90, 0),
    "nordvpn":     discord.Color.from_rgb(0, 99, 220),
    "netflix":     discord.Color.from_rgb(229, 9, 20),
    "disney":      discord.Color.from_rgb(17, 60, 165),
    "hotmail":     discord.Color.from_rgb(0, 120, 212),
    "capcut":      discord.Color.from_rgb(50, 50, 50),
    "spotify":     discord.Color.from_rgb(30, 215, 96),
}

SERVICE_EMOJI = {
    "steam":       "🎮",
    "xbox":        "🟢",
    "minecraft":   "⛏️",
    "roblox":      "🟥",
    "crunchyroll": "🍥",
    "nordvpn":     "🔒",
    "netflix":     "🎬",
    "disney":      "🏰",
    "hotmail":     "📧",
    "capcut":      "🎵",
    "spotify":     "🎧",
}

SERVICE_DISPLAY = {
    "steam":       "Steam",
    "xbox":        "Xbox",
    "minecraft":   "Minecraft",
    "roblox":      "Roblox",
    "crunchyroll": "Crunchyroll",
    "nordvpn":     "NordVPN",
    "netflix":     "Netflix",
    "disney":      "Disney+",
    "hotmail":     "Hotmail",
    "capcut":      "CapCut",
    "spotify":     "Spotify",
}

def service_embed(service: str, user: str, pwd: str, extra: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{SERVICE_EMOJI[service]} {SERVICE_DISPLAY[service]} Account",
        description="💎 **Diamond Account Gen**\nWe only distribute accounts we own.\nWe take no responsibility for what you do with these accounts.",
        color=SERVICE_COLORS[service],
    )
    embed.set_author(name="💎 Diamond Account Gen")
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1470798856085307423/1471984801266532362/IMG_7053.gif")
    embed.add_field(name="🔐 Account Details", value=f"`{user}:{pwd}`", inline=False)
    if extra:
        label = "🎮 Games" if service == "steam" else "📋 Info"
        embed.add_field(
            name=label,
            value=extra[:1024] if len(extra) <= 1024 else extra[:1021] + "...",
            inline=False,
        )
    embed.set_footer(text="💎 Diamond Account Gen • Enjoy! ❤️")
    return embed

# ================= PAGINATION =================
class GameView(discord.ui.View):
    def __init__(self, user_id, pages):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.pages   = pages
        self.index   = 0
        self._sync()

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ These buttons are not for you.", ephemeral=True)
            return False
        return True

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index == len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(content=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync()
        await interaction.response.edit_message(content=self.pages[self.index], view=self)

# ================= EVENTS =================
@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    await bot.change_presence(
        activity=discord.Game(name="💎 Diamond Account Gen"),
        status=discord.Status.online,
    )
    print(f"✅ Logged in as {bot.user}")

# ================= /generate =================
@bot.tree.command(name="generate", description="Generate a free account")
@app_commands.describe(
    service="Which service to generate",
    game="Steam only — filter by game name (optional)"
)
@app_commands.choices(service=[
    app_commands.Choice(name="🎮 Steam",       value="steam"),
    app_commands.Choice(name="🟢 Xbox",        value="xbox"),
    app_commands.Choice(name="⛏️ Minecraft",   value="minecraft"),
    app_commands.Choice(name="🟥 Roblox",      value="roblox"),
    app_commands.Choice(name="🍥 Crunchyroll", value="crunchyroll"),
    app_commands.Choice(name="🔒 NordVPN",     value="nordvpn"),
    app_commands.Choice(name="🎬 Netflix",     value="netflix"),
    app_commands.Choice(name="🏰 Disney+",     value="disney"),
    app_commands.Choice(name="📧 Hotmail",     value="hotmail"),
    app_commands.Choice(name="🎵 CapCut",      value="capcut"),
    app_commands.Choice(name="🎧 Spotify",     value="spotify"),
])
async def generate(interaction: discord.Interaction, service: str, game: str = None):
    await interaction.response.defer(ephemeral=True)

    # Check member role
    if not is_member(interaction.user):
        await interaction.followup.send(
            "❌ You need the **Member** role to generate accounts.", ephemeral=True
        )
        return

    # Check cooldown
    cd = get_cooldown(interaction.user)
    remaining = get_remaining_cooldown(interaction.user.id, service, cd)
    if remaining > 0:
        await interaction.followup.send(
            f"⏳ You're on cooldown for **{SERVICE_DISPLAY[service]}**.\n"
            f"Try again in **{format_time(remaining)}**.",
            ephemeral=True
        )
        return

    table = f"{service}_accounts"

    with db() as con:
        cur = con.cursor()
        if game and service == "steam":
            cur.execute(
                f"SELECT id, username, password, extra FROM {table} "
                f"WHERE extra LIKE ? ORDER BY RANDOM() LIMIT 1",
                (f"%{game}%",),
            )
        else:
            cur.execute(
                f"SELECT id, username, password, extra FROM {table} ORDER BY RANDOM() LIMIT 1"
            )
        row = cur.fetchone()

        if not row:
            label = f"{SERVICE_DISPLAY[service]}{f' ({game})' if game else ''}"
            await interaction.followup.send(f"❌ No **{label}** accounts in stock.", ephemeral=True)
            return

        acc_id, user, pwd, extra = row
        cur.execute(
            "INSERT INTO gens (user_id, service, ts) VALUES (?,?,?)",
            (interaction.user.id, service, int(time.time()))
        )

    # Set cooldown AFTER successful gen
    set_cooldown(interaction.user.id, service)

    embed = service_embed(service, user, pwd, extra)

    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            f"✅ Account sent to your DMs!\n⏳ Next **{SERVICE_DISPLAY[service]}** gen available in **{format_time(cd)}**.",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ Couldn't DM you. Enable DMs from server members.\n\n**Account:** `{user}:{pwd}`\n"
            f"⏳ Next gen available in **{format_time(cd)}**.",
            ephemeral=True,
        )

# ================= /cooldown =================
@bot.tree.command(name="cooldown", description="Check your current cooldowns")
async def cooldown_cmd(interaction: discord.Interaction):
    if not is_member(interaction.user):
        await interaction.response.send_message("❌ You need the **Member** role.", ephemeral=True)
        return

    cd = get_cooldown(interaction.user)
    lines = []
    for svc in SERVICES:
        remaining = get_remaining_cooldown(interaction.user.id, svc, cd)
        if remaining > 0:
            lines.append(f"{SERVICE_EMOJI[svc]} **{SERVICE_DISPLAY[svc]}:** ⏳ {format_time(remaining)}")
        else:
            lines.append(f"{SERVICE_EMOJI[svc]} **{SERVICE_DISPLAY[svc]}:** ✅ Ready")

    embed = discord.Embed(title="⏱️ Your Cooldowns", color=discord.Color.blurple())
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Base cooldown: {format_time(COOLDOWN_SECONDS)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= /stock =================
@bot.tree.command(name="stock", description="View available account stock")
async def stock_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(title="📦 Stock Overview", color=discord.Color.blurple())
    with db() as con:
        cur = con.cursor()
        for svc in SERVICES:
            cur.execute(f"SELECT COUNT(*) FROM {svc}_accounts")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM reports WHERE service=?", (svc,))
            reported = cur.fetchone()[0]
            embed.add_field(
                name=f"{SERVICE_EMOJI[svc]} {SERVICE_DISPLAY[svc]}",
                value=f"✅ **{max(total - reported, 0)}** available\n🚨 **{reported}** reported",
                inline=True,
            )
    await interaction.followup.send(embed=embed)

# ================= /search & /listgames =================
@bot.tree.command(name="search", description="Search Steam stock for a game")
@app_commands.describe(game="Game to search for")
async def search(interaction: discord.Interaction, game: str):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM steam_accounts WHERE extra LIKE ?", (f"%{game}%",))
        count = cur.fetchone()[0]
    await interaction.response.send_message(f"🔍 **{game}** — **{count}** account(s) in stock.")


@bot.tree.command(name="listgames", description="Browse available Steam games")
async def listgames(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT DISTINCT extra FROM steam_accounts WHERE extra != ''")
        rows = cur.fetchall()

    games = sorted({g.strip() for (row,) in rows for g in row.replace("/", ",").split(",") if g.strip()})

    if not games:
        await interaction.response.send_message("❌ No Steam games in stock.")
        return

    pages = ["🎮 **Available Steam Games**\n" + "\n".join(games[i:i+15])
             for i in range(0, len(games), 15)]
    view = GameView(interaction.user.id, pages)
    await interaction.response.send_message(pages[0], view=view)

# ================= USER COMMANDS =================
@bot.tree.command(name="mystats", description="View your gen stats")
async def mystats(interaction: discord.Interaction):
    cd = get_cooldown(interaction.user)
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM gens WHERE user_id=?", (interaction.user.id,))
        total_gens = cur.fetchone()[0]

    ref = has_referral(interaction.user.id)
    embed = discord.Embed(title="📊 Your Stats", color=discord.Color.blurple())
    embed.add_field(name="🎯 Total Gens",     value=str(total_gens), inline=True)
    embed.add_field(name="⏱️ Your Cooldown",  value=format_time(cd), inline=True)
    embed.add_field(name="🎁 Referral Bonus", value="Yes" if ref else "No", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="topusers", description="Top generators of all time")
async def topusers(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT user_id, COUNT(*) FROM gens "
            "GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 10"
        )
        rows = cur.fetchall()

    if not rows:
        await interaction.response.send_message("❌ No gens yet.")
        return

    msg = "🏆 **Top Users**\n" + "\n".join(
        f"{i}. <@{uid}> — {count}" for i, (uid, count) in enumerate(rows, 1)
    )
    await interaction.response.send_message(msg)


@bot.tree.command(name="boostinfo", description="View boost perks")
async def boostinfo(interaction: discord.Interaction):
    await interaction.response.send_message(
        "💎 **Boost Perks**\n"
        f"No boost: **{format_time(COOLDOWN_SECONDS)}** cooldown\n"
        f"1+ boost: **{format_time(BOOSTER_COOLDOWN)}** cooldown\n"
        "Referral code: **-60s** off cooldown",
        ephemeral=True,
    )

# ================= REFERRALS =================
@bot.tree.command(name="referral_create", description="Create your referral code")
async def referral_create(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT code FROM referrals WHERE owner_id=?", (interaction.user.id,))
        existing = cur.fetchone()
        if existing:
            await interaction.response.send_message(f"🎁 **Your Referral Code:** `{existing[0]}`", ephemeral=True)
            return
        code = "".join(str(random.randint(0, 9)) for _ in range(8))
        cur.execute("INSERT OR IGNORE INTO referrals VALUES (?,?)", (interaction.user.id, code))
    await interaction.response.send_message(f"🎁 **Your Referral Code:** `{code}`", ephemeral=True)


@bot.tree.command(name="refer", description="Redeem a referral code for -60s cooldown")
@app_commands.describe(code="The 8-digit referral code")
async def refer(interaction: discord.Interaction, code: str):
    if not code.isdigit() or len(code) != 8:
        await interaction.response.send_message("❌ Invalid code.", ephemeral=True)
        return
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT owner_id FROM referrals WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message("❌ Code not found.", ephemeral=True)
            return
        if row[0] == interaction.user.id:
            await interaction.response.send_message("❌ You can't use your own code.", ephemeral=True)
            return
        if has_referral(interaction.user.id):
            await interaction.response.send_message("❌ Already redeemed a referral.", ephemeral=True)
            return
        cur.execute("INSERT OR IGNORE INTO referral_uses VALUES (?)", (interaction.user.id,))
    await interaction.response.send_message("✅ Referral redeemed! -60s off your cooldown.", ephemeral=True)

# ================= REPORT / VOUCH =================
@bot.tree.command(name="report", description="Report a bad account")
@app_commands.describe(service="Which service", account="Account in user:pass format", reason="Reason")
@app_commands.choices(service=[
    app_commands.Choice(name=f"{SERVICE_EMOJI[s]} {SERVICE_DISPLAY[s]}", value=s) for s in SERVICES
])
async def report(interaction: discord.Interaction, service: str, account: str, reason: str = "Invalid"):
    with db() as con:
        con.execute("INSERT INTO reports VALUES (?,?,?)", (account, service, reason))
    await interaction.response.send_message("🚨 Report submitted.", ephemeral=True)


@bot.tree.command(name="vouch", description="Leave a vouch for the service")
@app_commands.describe(message="Your vouch message")
async def vouch(interaction: discord.Interaction, message: str):
    with db() as con:
        con.execute(
            "INSERT INTO vouches (user_id, message, timestamp) VALUES (?,?,?)",
            (interaction.user.id, message, datetime.utcnow().isoformat()),
        )
    embed = discord.Embed(title="⭐ New Vouch", description=message, color=discord.Color.gold())
    embed.set_footer(text=f"Vouched by {interaction.user.display_name}")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed)

# ================= STAFF COMMANDS =================
@bot.tree.command(name="restock", description="[Staff] Upload a .txt file to restock accounts")
@app_commands.describe(
    service="Which service to restock",
    file="Steam: user:pass|Games per line. Others: user:pass per line."
)
@app_commands.choices(service=[
    app_commands.Choice(name=f"{SERVICE_EMOJI[s]} {SERVICE_DISPLAY[s]}", value=s) for s in SERVICES
])
@app_commands.check(staff_check)
async def restock(interaction: discord.Interaction, service: str, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    if not file.filename.endswith(".txt"):
        await interaction.followup.send("❌ Please upload a `.txt` file.", ephemeral=True)
        return

    try:
        text = (await file.read()).decode("utf-8", errors="ignore")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read file: {e}", ephemeral=True)
        return

    parsed = parse_steam_file(text) if service == "steam" else parse_simple_file(text)

    if not parsed:
        await interaction.followup.send("❌ No valid accounts found in file.", ephemeral=True)
        return

    table = f"{service}_accounts"
    added = 0
    with db() as con:
        cur = con.cursor()
        for user, pwd, extra in parsed:
            cur.execute(
                f"INSERT INTO {table} (username, password, extra) VALUES (?,?,?)",
                (user, pwd, extra)
            )
            added += 1
        con.commit()

    embed = discord.Embed(title="🔄 Restock Complete", color=discord.Color.green())
    embed.add_field(name="Service", value=f"{SERVICE_EMOJI[service]} {SERVICE_DISPLAY[service]}", inline=True)
    embed.add_field(name="Added",   value=f"**{added}** account(s)", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="downloadstock", description="[Staff] Download all stock for a service as a .txt file")
@app_commands.describe(service="Which service to download stock for")
@app_commands.choices(service=[
    app_commands.Choice(name=f"{SERVICE_EMOJI[s]} {SERVICE_DISPLAY[s]}", value=s) for s in SERVICES
])
@app_commands.check(staff_check)
async def downloadstock(interaction: discord.Interaction, service: str):
    await interaction.response.defer(ephemeral=True)

    with db() as con:
        cur = con.cursor()
        cur.execute(f"SELECT username, password, extra FROM {service}_accounts ORDER BY id")
        rows = cur.fetchall()

    if not rows:
        await interaction.followup.send(f"❌ No stock for **{SERVICE_DISPLAY[service]}**.", ephemeral=True)
        return

    lines = []
    for user, pwd, extra in rows:
        if extra:
            lines.append(f"{user}:{pwd}|{extra}")
        else:
            lines.append(f"{user}:{pwd}")

    content = "\n".join(lines)
    file_bytes = io.BytesIO(content.encode("utf-8"))
    file = discord.File(file_bytes, filename=f"{service}_stock.txt")

    await interaction.followup.send(
        f"📥 **{SERVICE_DISPLAY[service]}** stock — **{len(rows)}** account(s)",
        file=file,
        ephemeral=True
    )


@bot.tree.command(name="removeaccount", description="[Staff] Remove an account from stock")
@app_commands.describe(service="Which service", account="user:pass")
@app_commands.choices(service=[
    app_commands.Choice(name=f"{SERVICE_EMOJI[s]} {SERVICE_DISPLAY[s]}", value=s) for s in SERVICES
])
@app_commands.check(staff_check)
async def removeaccount(interaction: discord.Interaction, service: str, account: str):
    with db() as con:
        cur = con.cursor()
        cur.execute(f"DELETE FROM {service}_accounts WHERE username||':'||password=?", (account,))
        removed = cur.rowcount
    await interaction.response.send_message(f"🗑️ Removed **{removed}** account(s).", ephemeral=True)


@bot.tree.command(name="resetcooldown", description="[Staff] Reset a user's cooldown for a service")
@app_commands.describe(user="The user to reset", service="Which service")
@app_commands.choices(service=[
    app_commands.Choice(name=f"{SERVICE_EMOJI[s]} {SERVICE_DISPLAY[s]}", value=s) for s in SERVICES
])
@app_commands.check(staff_check)
async def resetcooldown(interaction: discord.Interaction, user: discord.Member, service: str):
    with db() as con:
        con.execute(
            "DELETE FROM cooldowns WHERE user_id=? AND service=?",
            (user.id, service)
        )
    await interaction.response.send_message(
        f"✅ Reset **{SERVICE_DISPLAY[service]}** cooldown for {user.mention}.", ephemeral=True
    )


@bot.tree.command(name="reportedaccounts", description="[Staff] View reported accounts")
@app_commands.check(staff_check)
async def reportedaccounts(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT account, service, reason FROM reports")
        rows = cur.fetchall()

    if not rows:
        await interaction.response.send_message("✅ No reports.", ephemeral=True)
        return

    msg = "🚨 **Reported Accounts**\n" + "\n".join(
        f"`{acc}` [{SERVICE_DISPLAY.get(svc, svc)}] — {reason}" for acc, svc, reason in rows
    )
    await interaction.response.send_message(msg[:2000], ephemeral=True)


@bot.tree.command(name="resetreport", description="[Staff] Clear a specific report")
@app_commands.describe(account="user:pass to clear")
@app_commands.check(staff_check)
async def resetreport(interaction: discord.Interaction, account: str):
    with db() as con:
        con.execute("DELETE FROM reports WHERE account=?", (account,))
    await interaction.response.send_message("✅ Report cleared.", ephemeral=True)


@bot.tree.command(name="resetallreports", description="[Staff] Clear all reports")
@app_commands.check(staff_check)
async def resetallreports(interaction: discord.Interaction):
    with db() as con:
        con.execute("DELETE FROM reports")
    await interaction.response.send_message("✅ All reports cleared.", ephemeral=True)


@bot.tree.command(name="globalstats", description="[Staff] View global bot stats")
@app_commands.check(staff_check)
async def globalstats(interaction: discord.Interaction):
    embed = discord.Embed(title="🌍 Global Stats", color=discord.Color.blurple())
    with db() as con:
        cur = con.cursor()
        for svc in SERVICES:
            cur.execute(f"SELECT COUNT(*) FROM {svc}_accounts")
            total = cur.fetchone()[0]
            embed.add_field(
                name=f"{SERVICE_EMOJI[svc]} {SERVICE_DISPLAY[svc]}",
                value=f"**{total}** accounts",
                inline=True
            )
        cur.execute("SELECT COUNT(*) FROM gens")
        all_gens = cur.fetchone()[0]
    embed.add_field(name="🎯 Total Gens", value=str(all_gens), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= RUN =================
bot.run(TOKEN)
