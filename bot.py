import asyncio
import os
import re
import sqlite3
import datetime as dt
import time
import traceback
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "VCL X NAPXR.GG | PitchX")
LEAGUE_SHORT = os.getenv("LEAGUE_SHORT", "PitchX")
def _url(v):
    return v if v and v.startswith(("http://", "https://")) else None

LEAGUE_LOGO = _url(os.getenv("LEAGUE_LOGO_URL"))
PROFILE_URL = os.getenv("PROFILE_URL", "https://discord.com/users/{user_id}")
DASHBOARD_URL = _url(os.getenv("DASHBOARD_URL"))
REMINDER_DAYS = float(os.getenv("REMINDER_DAYS", "14"))
MATCHDAY_LEAD_HOURS = float(os.getenv("MATCHDAY_LEAD_HOURS", "24"))
OFFER_HOURS = 48

PINK, ORANGE, GREEN, YELLOW, RED = 0xE91E63, 0xF57C00, 0x2ECC71, 0xF1C40F, 0xE74C3C
FORM = {"W": "🟢", "D": "🟡", "L": "🔴"}

# ---------------------------------------------------------------- database
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pitchx.db"))
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
try:
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=NORMAL')
except sqlite3.Error:
    pass
db.executescript("""
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, logo TEXT, tier TEXT);
CREATE TABLE IF NOT EXISTS players(
  user_id INTEGER PRIMARY KEY, team_id INTEGER, signed_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, user_id INTEGER, team_id INTEGER);
CREATE TABLE IF NOT EXISTS fixtures(
  id INTEGER PRIMARY KEY, gw INTEGER, home_id INTEGER, away_id INTEGER, kickoff REAL, tier TEXT,
  competition TEXT, status TEXT DEFAULT 'scheduled', home_goals INTEGER, away_goals INTEGER,
  notes TEXT, brief_sent INTEGER DEFAULT 0, played_at REAL, forfeit INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS managers(
  team_id INTEGER, user_id INTEGER, PRIMARY KEY(team_id, user_id));
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, team_id INTEGER, from_team_id INTEGER,
  staff_id INTEGER, guild_id INTEGER, created_at REAL, status TEXT DEFAULT 'pending');
""")

def get_team(name):
    return db.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()

def team_by_id(tid):
    return db.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone() if tid else None

def player_team(user_id):
    r = db.execute("SELECT team_id FROM players WHERE user_id=?", (user_id,)).fetchone()
    return team_by_id(r["team_id"]) if r else None

def get_setting(k):
    r = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else None

def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v)); db.commit()

# ---------------------------------------------------------------- helpers
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def team_ac(_, current: str):
    rows = db.execute("SELECT name FROM teams WHERE name LIKE ? LIMIT 25", (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

def profile_view(user_id):
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="View Your Profile", emoji="↗️",
                                 url=PROFILE_URL.format(user_id=user_id)))
    return v

def dashboard_view():
    if not DASHBOARD_URL:
        return None
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Open Your Dashboard", emoji="🏟️", url=DASHBOARD_URL))
    return v

def logo(team):
    u = team["logo"] if team else None
    return u if u and u.startswith(("http://", "https://")) else None

def footer_text(team):
    return f"{team['tier']} | {team['name']} • {LEAGUE_SHORT}" if team else LEAGUE_SHORT

def tx_embed(title, desc, quote, team, color=PINK):
    e = discord.Embed(title=title, description=f"{desc}\n\n{quote}", color=color,
                      timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Transactions", icon_url=LEAGUE_LOGO)
    if logo(team):
        e.set_thumbnail(url=logo(team))
    return e

async def post_tx(guild, embed, user_id, content=None):
    ch_id = get_setting("tx_channel")
    ch = guild.get_channel(int(ch_id)) if guild and ch_id else None
    if ch:
        await ch.send(content=content, embed=embed, view=profile_view(user_id))
    return ch

def is_staff_member(m):
    p = m.guild_permissions
    if p.administrator or p.manage_guild or m.id == m.guild.owner_id:
        return True
    rid = get_setting("staff_role")
    return bool(rid) and any(r.id == int(rid) for r in m.roles)

async def _staff_pred(i: discord.Interaction):
    if i.guild and isinstance(i.user, discord.Member) and is_staff_member(i.user):
        return True
    raise app_commands.CheckFailure("not staff")

# Runtime check (not hidden by Discord), so admins and the staff role can always use these.
staff = app_commands.check(_staff_pred)

# ---------------------------------------------------------------- setup / teams
@bot.tree.command(description="Set the channel where transactions are posted")
@staff
async def setup(i: discord.Interaction, channel: discord.TextChannel):
    set_setting("tx_channel", str(channel.id)); set_setting("guild_id", str(i.guild_id))
    await i.response.send_message(f"✅ Transactions will post in {channel.mention}", ephemeral=True)

@bot.tree.command(description="Add a team to the league")
@staff
async def team_add(i: discord.Interaction, name: str, tier: str = "D-Tier", logo_url: str = None):
    if logo_url and not logo_url.startswith(("http://", "https://")):
        return await i.response.send_message("Logo must be a direct image link starting with http:// or https://", ephemeral=True)
    try:
        db.execute("INSERT INTO teams(name,logo,tier) VALUES(?,?,?)", (name, logo_url, tier)); db.commit()
    except sqlite3.IntegrityError:
        return await i.response.send_message("That team already exists.", ephemeral=True)
    await i.response.send_message(f"✅ Added **{name}** ({tier})", ephemeral=True)

@bot.tree.command(description="Set or change a team's logo (image URL)")
@staff
@app_commands.autocomplete(team=team_ac)
async def team_logo(i: discord.Interaction, team: str, logo_url: str):
    if not logo_url.startswith(("http://", "https://")):
        return await i.response.send_message("Logo must be a direct image link starting with http:// or https://", ephemeral=True)
    if not get_team(team):
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("UPDATE teams SET logo=? WHERE name=?", (logo_url, team)); db.commit()
    await i.response.send_message(f"✅ Logo updated for **{team}**", ephemeral=True)

@bot.tree.command(description="Remove a team")
@staff
@app_commands.autocomplete(team=team_ac)
async def team_remove(i: discord.Interaction, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("UPDATE players SET team_id=NULL WHERE team_id=?", (t["id"],))
    db.execute("DELETE FROM fixtures WHERE home_id=? OR away_id=?", (t["id"], t["id"]))
    db.execute("DELETE FROM managers WHERE team_id=?", (t["id"],))
    db.execute("DELETE FROM teams WHERE id=?", (t["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Removed **{team}**", ephemeral=True)

@bot.tree.command(description="List all teams")
async def teams(i: discord.Interaction):
    rows = db.execute("""SELECT t.name, t.tier, COUNT(p.user_id) n FROM teams t
                         LEFT JOIN players p ON p.team_id=t.id GROUP BY t.id ORDER BY t.name""").fetchall()
    if not rows:
        return await i.response.send_message("No teams yet.", ephemeral=True)
    e = discord.Embed(title=f"{LEAGUE_NAME} Teams", color=PINK,
                      description="\n".join(f"**{r['name']}** • {r['tier']} • {r['n']} players" for r in rows))
    await i.response.send_message(embed=e)

@bot.tree.command(description="Show a team's roster")
@app_commands.autocomplete(team=team_ac)
async def roster(i: discord.Interaction, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    ps = db.execute("SELECT user_id FROM players WHERE team_id=?", (t["id"],)).fetchall()
    e = discord.Embed(title=f"{t['name']} Roster", color=PINK,
                      description="\n".join(f"<@{p['user_id']}>" for p in ps) or "No players signed.")
    if logo(t): e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

@bot.tree.command(description="View a player's status")
async def profile(i: discord.Interaction, player: discord.Member = None):
    player = player or i.user
    t = player_team(player.id)
    e = discord.Embed(title=player.display_name, color=PINK)
    e.add_field(name="Club", value=t["name"] if t else "Free Agent")
    e.add_field(name="Tier", value=t["tier"] if t else "—")
    e.set_thumbnail(url=player.display_avatar.url)
    await i.response.send_message(embed=e)

# ---------------------------------------------------------------- offers (player must accept)
def offer_embed(kind, team, from_team):
    tail = f"\n\n⏳ This offer expires in {OFFER_HOURS} hours."
    if kind == "sign":
        title, desc = "📝 You've Got a Contract Offer!", f"**{team['name']}** wants to sign you."
        quote = f"> 🏟️ **Club:** {team['name']}\n> 📋 **Status:** Awaiting your answer"
        ask, logo_team, foot = "Do you want to sign?", team, team
    elif kind == "transfer":
        title, desc = "🔁 You've Got a Transfer Offer!", f"**{team['name']}** wants to bring you in."
        quote = f"> 🔁 **From:** {from_team['name']}\n> 🏟️ **To:** {team['name']}"
        ask, logo_team, foot = "Do you want to transfer?", team, team
    elif kind == "loan":
        title, desc = "🔄 You've Got a Loan Offer!", f"**{team['name']}** wants to take you on loan."
        quote = f"> 🔁 **From:** {from_team['name']}\n> 🏟️ **Loan Club:** {team['name']}"
        ask, logo_team, foot = "Do you want to go on loan?", team, team
    else:  # release
        title, desc = "📤 Release Request", f"**{from_team['name']}** wants to release you."
        quote = f"> 🏟️ **Club:** {from_team['name']}\n> 📋 **Status after:** Free Agent"
        ask, logo_team, foot = "Do you accept being released?", from_team, from_team
    e = discord.Embed(title=title, description=f"{desc}\n\n{quote}\n\n**{ask}**{tail}",
                      color=PINK, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Transactions", icon_url=LEAGUE_LOGO)
    e.set_footer(text=footer_text(foot))
    if logo(logo_team):
        e.set_thumbnail(url=logo(logo_team))
    return e

class OfferButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"pitchx:offer:(?P<action>accept|decline):(?P<id>[0-9]+)"):
    def __init__(self, action: str, offer_id: int):
        ok = action == "accept"
        super().__init__(discord.ui.Button(
            label="Accept" if ok else "Decline", emoji="✅" if ok else "❌",
            style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
            custom_id=f"pitchx:offer:{action}:{offer_id}"))
        self.action, self.offer_id = action, int(offer_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await handle_offer(interaction, self.offer_id, self.action == "accept")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = f"⚠️ Something went wrong: {type(error).__name__}: {str(error)[:200]}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

def offer_view(offer_id):
    v = discord.ui.View(timeout=None)
    v.add_item(OfferButton("accept", offer_id))
    v.add_item(OfferButton("decline", offer_id))
    return v

def apply_offer(o):
    """Carry out an accepted offer and return the result embed."""
    new, old = team_by_id(o["team_id"]), team_by_id(o["from_team_id"])
    uid, kind = o["user_id"], o["kind"]
    if kind == "release":
        db.execute("UPDATE players SET team_id=NULL WHERE user_id=?", (uid,))
        embed = tx_embed("You've Been Released", f"You have been released from **{old['name']}**.",
                         f"> 🏟️ **Former Club:** {old['name']}\n> 📋 **Status:** Free Agent\n\n"
                         "You are now free to sign with any team.", old)
        team_id = old["id"]
    else:
        db.execute("INSERT OR REPLACE INTO players VALUES(?,?,datetime('now'))", (uid, new["id"]))
        team_id = new["id"]
        if kind == "sign":
            embed = tx_embed("You've Been Signed!", f"You have signed a contract with **{new['name']}**!",
                             f"> 🏟️ **New Club:** {new['name']}\n\nWelcome to the team! ⚽", new)
        elif kind == "transfer":
            embed = tx_embed("Transfer Complete", f"You have been transferred to **{new['name']}**!",
                             f"> 🔁 **From:** {old['name']}\n> 🏟️ **To:** {new['name']}", new, GREEN)
        else:
            embed = tx_embed("Loan Complete", f"You are now on loan at **{new['name']}**!",
                             f"> 🔁 **From:** {old['name']}\n> 🏟️ **Loan Club:** {new['name']}", new, GREEN)
    db.execute("INSERT INTO events(kind,user_id,team_id) VALUES(?,?,?)", (kind, uid, team_id))
    db.execute("UPDATE offers SET status='accepted' WHERE id=?", (o["id"],))
    db.commit()
    return embed

async def handle_offer(i: discord.Interaction, offer_id: int, accept: bool):
    o = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not o:
        return await i.response.send_message("This offer no longer exists.", ephemeral=True)
    if i.user.id != o["user_id"]:
        return await i.response.send_message("This offer isn't for you.", ephemeral=True)
    if o["status"] != "pending":
        return await i.response.send_message("This offer was already answered or cancelled.", ephemeral=True)

    emb = i.message.embeds[0].copy() if i.message.embeds else discord.Embed()
    if time.time() - o["created_at"] > OFFER_HOURS * 3600:
        db.execute("UPDATE offers SET status='expired' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text="⌛ This offer expired")
        return await i.response.edit_message(embed=emb, view=None)

    # make sure nothing changed since the offer was sent
    cur = player_team(o["user_id"])
    ok = True
    if o["kind"] == "sign":
        ok = cur is None and team_by_id(o["team_id"])
    elif o["kind"] in ("transfer", "loan"):
        ok = cur and cur["id"] == o["from_team_id"] and team_by_id(o["team_id"])
    else:
        ok = cur and cur["id"] == o["from_team_id"]
    if accept and not ok:
        db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text="⚠️ This offer is no longer valid")
        return await i.response.edit_message(embed=emb, view=None)

    guild = bot.get_guild(o["guild_id"])
    staff_user = None
    try:
        staff_user = await bot.fetch_user(o["staff_id"])
    except discord.HTTPException:
        pass

    if not accept:
        db.execute("UPDATE offers SET status='declined' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text="❌ You declined this offer")
        await i.response.edit_message(embed=emb, view=None)
        if staff_user:
            try: await staff_user.send(f"❌ <@{o['user_id']}> **declined** the {o['kind']} offer.")
            except discord.HTTPException: pass
        return

    result = apply_offer(o)
    emb.color = discord.Color(GREEN); emb.set_footer(text="✅ You accepted this offer")
    await i.response.edit_message(embed=emb, view=None)
    ch = await post_tx(guild, result, o["user_id"], content=f"<@{o['user_id']}>")
    if not ch or i.channel_id != ch.id:
        await i.followup.send(embed=result, view=profile_view(o["user_id"]))
    if staff_user:
        try: await staff_user.send(f"✅ <@{o['user_id']}> **accepted** the {o['kind']} offer.")
        except discord.HTTPException: pass

async def send_offer(i: discord.Interaction, player: discord.Member, kind: str, team, from_team):
    db.execute("UPDATE offers SET status='cancelled' WHERE user_id=? AND status='pending'", (player.id,))
    cur = db.execute("""INSERT INTO offers(user_id,kind,team_id,from_team_id,staff_id,guild_id,created_at)
                        VALUES(?,?,?,?,?,?,?)""",
                     (player.id, kind, team["id"] if team else None,
                      from_team["id"] if from_team else None, i.user.id, i.guild_id, time.time()))
    db.commit()
    oid = cur.lastrowid
    embed, view = offer_embed(kind, team, from_team), offer_view(oid)
    tname = team["name"] if team else ""
    fname = from_team["name"] if from_team else ""
    what = {"sign": f"sign with **{tname}**", "transfer": f"transfer to **{tname}**",
            "loan": f"go on loan to **{tname}**", "release": f"be released from **{fname}**"}[kind]

    try:
        await asyncio.wait_for(player.send(embed=embed, view=view), timeout=15)
        return await i.followup.send(f"📨 Offer sent to {player.mention} to {what}. Waiting for their answer.")
    except Exception as ex:
        print(f"[offer] DM to {player.id} failed: {ex!r}")

    reason = "no transactions channel is set (use /setup)"
    ch_id = get_setting("tx_channel")
    ch = i.guild.get_channel(int(ch_id)) if ch_id else None
    if ch:
        try:
            await asyncio.wait_for(ch.send(content=player.mention, embed=embed, view=view), timeout=15)
            return await i.followup.send(
                f"📨 Offer for {player.mention} to {what} (couldn't DM them, posted in {ch.mention})")
        except Exception as ex:
            print(f"[offer] channel post failed: {ex!r}")
            reason = f"I can't post in {ch.mention} ({type(ex).__name__}). Check my channel permissions"
    db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (oid,)); db.commit()
    await i.followup.send(f"❌ Couldn't DM {player.mention} and {reason}.")

def managed_team_ids(user_id):
    return [r["team_id"] for r in db.execute("SELECT team_id FROM managers WHERE user_id=?", (user_id,)).fetchall()]

async def _staff_or_manager_pred(i: discord.Interaction):
    if i.guild and isinstance(i.user, discord.Member) and (is_staff_member(i.user) or managed_team_ids(i.user.id)):
        return True
    raise app_commands.CheckFailure("not staff or manager")

# Staff can act for any team; team managers can act for the team(s) they manage.
staff_or_manager = app_commands.check(_staff_or_manager_pred)

def resolve_team(i, name):
    """Returns (team, error). Staff may pick any team; managers only teams they manage."""
    staff_ok = is_staff_member(i.user)
    mine = managed_team_ids(i.user.id)
    if name:
        t = get_team(name)
        if not t:
            return None, "Team not found."
        if not staff_ok and t["id"] not in mine:
            return None, "You can only do this for a team you manage."
        return t, None
    if len(mine) == 1:
        return team_by_id(mine[0]), None
    if mine:
        return None, "You manage more than one team. Pick which one."
    return None, "Pick a team."

async def my_team_ac(i: discord.Interaction, current: str):
    like = f"%{current}%"
    if isinstance(i.user, discord.Member) and is_staff_member(i.user):
        rows = db.execute("SELECT name FROM teams WHERE name LIKE ? LIMIT 25", (like,)).fetchall()
    else:
        rows = db.execute("""SELECT t.name FROM teams t JOIN managers m ON m.team_id=t.id
                             WHERE m.user_id=? AND t.name LIKE ? LIMIT 25""", (i.user.id, like)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

@bot.tree.command(description="Make someone a team's manager (they can sign/transfer/loan for it)")
@staff
@app_commands.autocomplete(team=team_ac)
async def manager_add(i: discord.Interaction, team: str, user: discord.Member):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("INSERT OR IGNORE INTO managers VALUES(?,?)", (t["id"], user.id)); db.commit()
    await i.response.send_message(f"✅ {user.mention} can now sign, transfer and loan players for **{t['name']}**.", ephemeral=True)

@bot.tree.command(description="Remove a team manager")
@staff
@app_commands.autocomplete(team=team_ac)
async def manager_remove(i: discord.Interaction, team: str, user: discord.Member):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("DELETE FROM managers WHERE team_id=? AND user_id=?", (t["id"], user.id)); db.commit()
    await i.response.send_message(f"✅ {user.mention} is no longer a manager of **{t['name']}**.", ephemeral=True)

@bot.tree.command(description="List team managers")
@app_commands.autocomplete(team=team_ac)
async def managers(i: discord.Interaction, team: str = None):
    t = get_team(team) if team else None
    if team and not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    q = "SELECT team_id, user_id FROM managers" + (" WHERE team_id=?" if t else "") + " ORDER BY team_id"
    rows = db.execute(q, (t["id"],) if t else ()).fetchall()
    desc = "\n".join(f"**{name_of(r['team_id'])}**: <@{r['user_id']}>" for r in rows) or "No managers set. Use /manager_add."
    await i.response.send_message(embed=discord.Embed(title="Team Managers", color=PINK, description=desc))

@bot.tree.command(description="Offer a free agent a contract for your team (they must accept)")
@staff_or_manager
@app_commands.autocomplete(team=my_team_ac)
async def sign(i: discord.Interaction, player: discord.Member, team: str = None):
    await i.response.defer(ephemeral=True)
    t, err = resolve_team(i, team)
    if err:
        return await i.followup.send(err)
    if player.bot:
        return await i.followup.send("You can't sign a bot.")
    if player_team(player.id):
        return await i.followup.send("That player already has a team. Use /transfer to bring them in.")
    await send_offer(i, player, "sign", t, None)

@bot.tree.command(description="Offer a player a transfer to your team (they must accept)")
@staff_or_manager
@app_commands.autocomplete(to_team=my_team_ac)
async def transfer(i: discord.Interaction, player: discord.Member, to_team: str = None):
    await i.response.defer(ephemeral=True)
    new, err = resolve_team(i, to_team)
    if err:
        return await i.followup.send(err)
    cur = player_team(player.id)
    if player.bot:
        return await i.followup.send("You can't transfer a bot.")
    if not cur:
        return await i.followup.send("That player is a free agent. Use /sign.")
    if cur["id"] == new["id"]:
        return await i.followup.send("They're already on that team.")
    await send_offer(i, player, "transfer", new, cur)

@bot.tree.command(description="Offer a player a loan move to your team (they must accept)")
@staff_or_manager
@app_commands.autocomplete(to_team=my_team_ac)
async def loan(i: discord.Interaction, player: discord.Member, to_team: str = None):
    await i.response.defer(ephemeral=True)
    new, err = resolve_team(i, to_team)
    if err:
        return await i.followup.send(err)
    cur = player_team(player.id)
    if player.bot:
        return await i.followup.send("You can't loan a bot.")
    if not cur or cur["id"] == new["id"]:
        return await i.followup.send("Player needs a different current team to be loaned out.")
    await send_offer(i, player, "loan", new, cur)

@bot.tree.command(description="Ask a player to be released (they must accept)")
@staff
async def release(i: discord.Interaction, player: discord.Member):
    await i.response.defer(ephemeral=True)
    cur = player_team(player.id)
    if not cur:
        return await i.followup.send("That player isn't on a team.")
    await send_offer(i, player, "release", None, cur)

# ---------------------------------------------------------------- time / lookup helpers
def league_tz():
    name = get_setting("timezone") or os.getenv("LEAGUE_TZ", "UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc

def parse_dt(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=league_tz())
        except ValueError:
            continue
    return None

TIME_HELP = "Use `YYYY-MM-DD HH:MM` (24h) or `YYYY-MM-DD 8:00 PM`, in the league timezone (set with /timezone)."

def tsf(x, style="F"):
    return f"<t:{int(x)}:{style}>"

def ordinal(n):
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def gw_label(gw):
    return f" — GW {gw}" if gw else ""

def get_guild():
    gid = GUILD_ID or get_setting("guild_id")
    if gid and bot.get_guild(int(gid)):
        return bot.get_guild(int(gid))
    return bot.guilds[0] if len(bot.guilds) == 1 else None

def chan(guild, key):
    cid = get_setting(key)
    return guild.get_channel(int(cid)) if guild and cid else None

def name_of(tid):
    t = team_by_id(tid)
    return t["name"] if t else "Deleted team"

def get_fx(val):
    try:
        n = int(str(val).strip().lstrip("#").split()[0])
    except (ValueError, IndexError):
        return None
    return db.execute("SELECT * FROM fixtures WHERE id=?", (n,)).fetchone()

def fixture_ac(status):
    async def ac(_, current: str):
        like = f"%{current}%"
        order = "ASC" if status == "scheduled" else "DESC"
        rows = db.execute(f"""SELECT f.id, f.gw, h.name hn, a.name an FROM fixtures f
            JOIN teams h ON h.id=f.home_id JOIN teams a ON a.id=f.away_id
            WHERE f.status=? AND (h.name LIKE ? OR a.name LIKE ? OR CAST(f.id AS TEXT)=?)
            ORDER BY f.kickoff {order} LIMIT 25""",
            (status, like, like, current.strip().lstrip("#"))).fetchall()
        return [app_commands.Choice(name=f"#{r['id']} GW{r['gw']} {r['hn']} vs {r['an']}"[:100],
                                    value=str(r["id"])) for r in rows]
    return ac

sched_ac = fixture_ac("scheduled")
played_ac = fixture_ac("played")

async def tier_ac(_, current: str):
    rows = db.execute("SELECT DISTINCT tier FROM teams WHERE tier LIKE ? LIMIT 25", (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["tier"], value=r["tier"]) for r in rows if r["tier"]]

# ---------------------------------------------------------------- records (standings / form / h2h)
def standings(tier=None):
    if tier:
        ts_ = db.execute("SELECT * FROM teams WHERE tier=?", (tier,)).fetchall()
    else:
        ts_ = db.execute("SELECT * FROM teams").fetchall()
    tbl = {t["id"]: dict(team=t, p=0, w=0, d=0, l=0, gf=0, ga=0, pts=0) for t in ts_}
    for f in db.execute("SELECT * FROM fixtures WHERE status='played'").fetchall():
        hg, ag = f["home_goals"], f["away_goals"]
        for tid, gf, ga in ((f["home_id"], hg, ag), (f["away_id"], ag, hg)):
            r = tbl.get(tid)
            if not r:
                continue
            r["p"] += 1; r["gf"] += gf; r["ga"] += ga
            if gf > ga:
                r["w"] += 1; r["pts"] += 3
            elif gf == ga:
                r["d"] += 1; r["pts"] += 1
            else:
                r["l"] += 1
    rows = sorted(tbl.values(), key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r["team"]["name"].lower()))
    for n, r in enumerate(rows, 1):
        r["pos"] = n
    return rows

def pos_pts(team, tier):
    for r in standings(tier):
        if r["team"]["id"] == team["id"]:
            return ordinal(r["pos"]), r["pts"]
    return "-", 0

def form_str(team_id, n=5):
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND (home_id=? OR away_id=?)
                         ORDER BY played_at DESC, id DESC LIMIT ?""", (team_id, team_id, n)).fetchall()
    out = []
    for f in reversed(rows):
        gf, ga = (f["home_goals"], f["away_goals"]) if f["home_id"] == team_id else (f["away_goals"], f["home_goals"])
        out.append("W" if gf > ga else "D" if gf == ga else "L")
    return "".join(out)

def h2h_text(me_id, opp_id):
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND
                         ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
                         ORDER BY played_at DESC, id DESC""", (me_id, opp_id, opp_id, me_id)).fetchall()
    if not rows:
        return "First meeting this season"
    w = d = l = 0
    for f in rows:
        gf, ga = (f["home_goals"], f["away_goals"]) if f["home_id"] == me_id else (f["away_goals"], f["home_goals"])
        if gf > ga: w += 1
        elif gf == ga: d += 1
        else: l += 1
    last = rows[0]
    return (f"Played **{len(rows)}** • You: {w}W {d}D {l}L\n"
            f"Last: {name_of(last['home_id'])} {last['home_goals']}–{last['away_goals']} {name_of(last['away_id'])}")

def next_fixture(team_id):
    return db.execute("""SELECT * FROM fixtures WHERE status='scheduled' AND (home_id=? OR away_id=?)
                         AND kickoff > ? ORDER BY kickoff LIMIT 1""", (team_id, team_id, time.time() - 7200)).fetchone()

def next_match_line(team, f):
    home = f["home_id"] == team["id"]
    opp = name_of(f["away_id"] if home else f["home_id"])
    return f"**{opp}** ({'H' if home else 'A'}) • {tsf(f['kickoff'])} ({tsf(f['kickoff'], 'R')})"

def table_block(rows):
    lines = [f"{'#':>2}  {'Team':<14} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GD':>3} {'Pts':>3}"]
    for r in rows[:18]:
        gd = r["gf"] - r["ga"]
        lines.append(f"{r['pos']:>2}  {r['team']['name'][:14]:<14} {r['p']:>2} {r['w']:>2} {r['d']:>2} {r['l']:>2} {gd:>+3} {r['pts']:>3}")
    return "```\n" + "\n".join(lines) + "\n```"

# ---------------------------------------------------------------- matchday briefs (auto + manual)
def brief_text():
    return get_setting("brief_text") or "Goals (2 pts), assists (2 pts)."

def matchday_embed(fx, me, opp, is_home):
    tier = fx["tier"] or me["tier"]
    mp, mpts = pos_pts(me, tier)
    op, opts = pos_pts(opp, tier)
    fm = lambda s: " ".join(FORM.get(c, "") for c in s)
    e = discord.Embed(title=f"{me['name']} vs {opp['name']} ({'H' if is_home else 'A'})",
                      color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Matchday{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    e.description = (f"📍 **{fx['competition'] or 'League'}** • {tier}\n"
                     f"🕒 {tsf(fx['kickoff'])} ({tsf(fx['kickoff'], 'R')})")
    e.add_field(name="📊 League position", inline=False, value=
        f"> You: **{mp}** ({mpts} pts) {fm(form_str(me['id']))}\n> Them: **{op}** ({opts} pts) {fm(form_str(opp['id']))}")
    h2h = "\n".join("> " + x for x in h2h_text(me["id"], opp["id"]).split("\n"))
    e.add_field(name="⚔️ Head to head", inline=False, value=h2h)
    e.add_field(name="💡 Your matchday brief", inline=False, value=f"> {brief_text()}")
    e.set_footer(text=f"{me['name']} • {tier}", icon_url=logo(me))
    if logo(me):
        e.set_thumbnail(url=logo(me))
    return e

def announce_embed(fx, h, a):
    e = discord.Embed(title=f"{h['name']} vs {a['name']}", color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Matchday{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    e.description = (f"📍 **{fx['competition'] or 'League'}** • {fx['tier'] or h['tier']}\n"
                     f"🕒 {tsf(fx['kickoff'])} ({tsf(fx['kickoff'], 'R')})")
    return e

async def send_matchday(fx):
    """DM every rostered player; post a public card and ping anyone whose DMs are closed."""
    guild = get_guild()
    h, a = team_by_id(fx["home_id"]), team_by_id(fx["away_id"])
    sent, fallback, empty = 0, [], []
    for me, opp, is_home in ((h, a, True), (a, h, False)):
        e = matchday_embed(fx, me, opp, is_home)
        players = db.execute("SELECT user_id FROM players WHERE team_id=?", (me["id"],)).fetchall()
        if not players:
            empty.append(me["name"])
        failed = []
        for p in players:
            try:
                u = (guild.get_member(p["user_id"]) if guild else None) or await bot.fetch_user(p["user_id"])
                await asyncio.wait_for(u.send(embed=e, view=profile_view(p["user_id"])), timeout=15)
                sent += 1
            except Exception as ex:
                print(f"[matchday] DM to {p['user_id']} failed: {ex!r}")
                failed.append(p["user_id"])
            await asyncio.sleep(0.3)
        if failed:
            fallback.append((e, failed))
    ch = chan(guild, "matchday_channel")
    if ch:
        try:
            await ch.send(embed=announce_embed(fx, h, a))
            for e, ids in fallback:
                await ch.send(content=" ".join(f"<@{u}>" for u in ids) + " (couldn't DM you, here's your brief)", embed=e)
        except Exception as ex:
            print(f"[matchday] channel post failed: {ex!r}")
            ch = None
    return dict(sent=sent, failed=sum(len(ids) for _, ids in fallback), posted=ch, empty=empty)

@tasks.loop(minutes=1)
async def matchday_loop():
    now = time.time()
    due = db.execute("""SELECT * FROM fixtures WHERE status='scheduled' AND brief_sent=0
                        AND kickoff - ? <= ? AND kickoff > ?""",
                     (now, MATCHDAY_LEAD_HOURS * 3600, now - 3 * 3600)).fetchall()
    for fx in due:
        db.execute("UPDATE fixtures SET brief_sent=1 WHERE id=?", (fx["id"],)); db.commit()
        try:
            await send_matchday(fx)
        except Exception:
            traceback.print_exc()

@matchday_loop.before_loop
async def _wait_md():
    await bot.wait_until_ready()

@bot.tree.command(description="Send the matchday brief for a fixture right now")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def matchday(i: discord.Interaction, fixture: str):
    await i.response.defer(ephemeral=True)
    fx = get_fx(fixture)
    if not fx:
        return await i.followup.send("Fixture not found. Pick one from the list (add one with /schedule_add).")
    db.execute("UPDATE fixtures SET brief_sent=1 WHERE id=?", (fx["id"],)); db.commit()
    r = await send_matchday(fx)
    msg = f"📨 Matchday{gw_label(fx['gw'])}: **{r['sent']}** DMs sent, **{r['failed']}** couldn't be DMed."
    if r["posted"]:
        msg += f" Public post and pings in {r['posted'].mention}."
    elif r["failed"]:
        msg += " Set a channel with /setchannel so closed-DM players get pinged there."
    for n in r["empty"]:
        msg += f"\n⚠️ **{n}** has no players on its roster."
    await i.followup.send(msg)

# ---------------------------------------------------------------- schedule
def round_robin(ids):
    ids = list(ids)
    if len(ids) % 2:
        ids.append(None)
    n, rounds = len(ids), []
    for r in range(n - 1):
        pairs = []
        for k in range(n // 2):
            a, b = ids[k], ids[n - 1 - k]
            if a is not None and b is not None:
                pairs.append((a, b) if (r + k) % 2 == 0 else (b, a))
        rounds.append(pairs)
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds

@bot.tree.command(description="Add one fixture to the schedule")
@staff
@app_commands.autocomplete(home=team_ac, away=team_ac)
async def schedule_add(i: discord.Interaction, home: str, away: str, when: str,
                       gameweek: int = 1, competition: str = "League"):
    h, a, d = get_team(home), get_team(away), parse_dt(when)
    if not h or not a:
        return await i.response.send_message("Team not found.", ephemeral=True)
    if h["id"] == a["id"]:
        return await i.response.send_message("A team can't play itself.", ephemeral=True)
    if not d:
        return await i.response.send_message(f"Couldn't read that time. {TIME_HELP}", ephemeral=True)
    cur = db.execute("INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition) VALUES(?,?,?,?,?,?)",
                     (gameweek, h["id"], a["id"], d.timestamp(), h["tier"], competition)); db.commit()
    await i.response.send_message(
        f"📅 Fixture **#{cur.lastrowid}**: **{h['name']}** vs **{a['name']}** • GW{gameweek} • {tsf(d.timestamp())}\n"
        f"Brief goes out automatically {int(MATCHDAY_LEAD_HOURS)}h before kickoff.", ephemeral=True)

@bot.tree.command(description="Auto-generate a full round-robin schedule for a tier")
@staff
@app_commands.autocomplete(tier=tier_ac)
async def schedule_generate(i: discord.Interaction, tier: str, start: str,
                            days_between: app_commands.Range[int, 1, 60] = 7,
                            legs: app_commands.Range[int, 1, 2] = 1,
                            competition: str = "League", replace: bool = False):
    await i.response.defer(ephemeral=True)
    base = parse_dt(start)
    if not base:
        return await i.followup.send(f"Couldn't read the start time. {TIME_HELP}")
    ids = [r["id"] for r in db.execute("SELECT id FROM teams WHERE tier=? ORDER BY id", (tier,)).fetchall()]
    if len(ids) < 2:
        return await i.followup.send(f"Tier **{tier}** needs at least 2 teams.")
    existing = db.execute("SELECT COUNT(*) FROM fixtures WHERE tier=? AND status='scheduled'", (tier,)).fetchone()[0]
    if existing and not replace:
        return await i.followup.send(f"**{tier}** already has {existing} scheduled fixtures. Re-run with `replace: True` to overwrite them.")
    if replace:
        db.execute("DELETE FROM fixtures WHERE tier=? AND status='scheduled'", (tier,))
    rounds = round_robin(ids)
    if legs == 2:
        rounds += [[(b, a) for a, b in r] for r in rounds]
    n = 0
    for gw, pairs in enumerate(rounds, 1):
        when = base + dt.timedelta(days=(gw - 1) * days_between)
        for h_id, a_id in pairs:
            db.execute("INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition) VALUES(?,?,?,?,?,?)",
                       (gw, h_id, a_id, when.timestamp(), tier, competition)); n += 1
    db.commit()
    await i.followup.send(f"📅 Created **{n}** fixtures over **{len(rounds)}** gameweeks for **{tier}** "
                          f"({len(ids)} teams), starting {tsf(base.timestamp())}. See them with /fixtures.")

@bot.tree.command(description="Remove a scheduled fixture")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def schedule_remove(i: discord.Interaction, fixture: str):
    fx = get_fx(fixture)
    if not fx or fx["status"] != "scheduled":
        return await i.response.send_message("Scheduled fixture not found.", ephemeral=True)
    db.execute("DELETE FROM fixtures WHERE id=?", (fx["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Removed fixture #{fx['id']}.", ephemeral=True)

@bot.tree.command(description="Move a fixture to a new date/time (brief is re-sent automatically)")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def schedule_move(i: discord.Interaction, fixture: str, when: str):
    fx, d = get_fx(fixture), parse_dt(when)
    if not fx or fx["status"] != "scheduled":
        return await i.response.send_message("Scheduled fixture not found.", ephemeral=True)
    if not d:
        return await i.response.send_message(f"Couldn't read that time. {TIME_HELP}", ephemeral=True)
    db.execute("UPDATE fixtures SET kickoff=?, brief_sent=0 WHERE id=?", (d.timestamp(), fx["id"])); db.commit()
    await i.response.send_message(f"📅 Fixture #{fx['id']} moved to {tsf(d.timestamp())}.", ephemeral=True)

@bot.tree.command(description="Delete all scheduled (unplayed) fixtures for a tier")
@staff
@app_commands.autocomplete(tier=tier_ac)
async def schedule_clear(i: discord.Interaction, tier: str):
    cur = db.execute("DELETE FROM fixtures WHERE tier=? AND status='scheduled'", (tier,)); db.commit()
    await i.response.send_message(f"🗑️ Deleted {cur.rowcount} scheduled fixtures from **{tier}**. Played results were kept.", ephemeral=True)

@bot.tree.command(name="fixtures", description="Upcoming fixtures")
@app_commands.autocomplete(team=team_ac, tier=tier_ac)
async def fixtures_cmd(i: discord.Interaction, team: str = None, tier: str = None,
                       limit: app_commands.Range[int, 1, 25] = 10):
    q, args = "SELECT * FROM fixtures WHERE status='scheduled'", []
    if team:
        t = get_team(team)
        if not t:
            return await i.response.send_message("Team not found.", ephemeral=True)
        q += " AND (home_id=? OR away_id=?)"; args += [t["id"], t["id"]]
    if tier:
        q += " AND tier=?"; args.append(tier)
    rows = db.execute(q + " ORDER BY kickoff LIMIT ?", args + [limit]).fetchall()
    if not rows:
        return await i.response.send_message("No upcoming fixtures.", ephemeral=True)
    lines = [f"`#{f['id']}` GW{f['gw']} • **{name_of(f['home_id'])}** vs **{name_of(f['away_id'])}** • {tsf(f['kickoff'])}" for f in rows]
    await i.response.send_message(embed=discord.Embed(title=f"{LEAGUE_SHORT} Fixtures", color=ORANGE, description="\n".join(lines)))

@bot.tree.command(description="Show a team's next match (defaults to your team)")
@app_commands.autocomplete(team=team_ac)
async def nextmatch(i: discord.Interaction, team: str = None):
    t = get_team(team) if team else player_team(i.user.id)
    if not t:
        return await i.response.send_message("Pick a team (you aren't on one).", ephemeral=True)
    f = next_fixture(t["id"])
    if not f:
        return await i.response.send_message(f"No upcoming match scheduled for **{t['name']}**.", ephemeral=True)
    e = discord.Embed(title=f"{t['name']}: next match", color=ORANGE,
                      description=f"{next_match_line(t, f)}\n📍 {f['competition'] or 'League'} • {f['tier'] or t['tier']}{gw_label(f['gw'])}")
    if logo(t):
        e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

# ---------------------------------------------------------------- match results
def save_result(fid, hg, ag, notes=None, forfeit=0):
    db.execute("""UPDATE fixtures SET status='played', home_goals=?, away_goals=?, notes=?, played_at=?, forfeit=?
                  WHERE id=?""", (hg, ag, notes, time.time(), forfeit, fid)); db.commit()

def result_embed(fx):
    h, a = team_by_id(fx["home_id"]), team_by_id(fx["away_id"])
    hg, ag = fx["home_goals"], fx["away_goals"]
    win = h if hg > ag else a if ag > hg else None
    line = f"🏆 **Winner:** {win['name']}" if win else "🤝 **Draw**"
    if fx["forfeit"]:
        line += " (forfeit)"
    e = discord.Embed(title=f"{h['name']} {hg} – {ag} {a['name']}", description=line,
                      color=GREEN if win else YELLOW, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Match Results{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    tier = fx["tier"] or h["tier"]
    pos = {r["team"]["id"]: r for r in standings(tier)}
    lines = [f"> {t['name']}: **{ordinal(pos[t['id']]['pos'])}** ({pos[t['id']]['pts']} pts)" for t in (h, a) if t["id"] in pos]
    if lines:
        e.add_field(name="📊 League position", value="\n".join(lines), inline=False)
    if fx["notes"] and not fx["forfeit"]:
        e.add_field(name="📝 Notes", value=f"> {fx['notes']}", inline=False)
    if win and logo(win):
        e.set_thumbnail(url=logo(win))
    e.set_footer(text=f"{fx['competition'] or 'League'} • {tier}")
    return e

async def announce_result(fx):
    guild = get_guild()
    ch = chan(guild, "results_channel") or chan(guild, "matchday_channel")
    if not ch:
        return "(No results channel set. Use /setchannel.)"
    try:
        await asyncio.wait_for(ch.send(embed=result_embed(fx)), timeout=15)
        return f"Posted in {ch.mention}."
    except Exception as ex:
        print(f"[result] post failed: {ex!r}")
        return f"(Couldn't post in {ch.mention}: {type(ex).__name__}.)"

@bot.tree.command(name="result", description="Enter the result of a scheduled match")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def result_cmd(i: discord.Interaction, fixture: str,
                     home_score: app_commands.Range[int, 0, 99], away_score: app_commands.Range[int, 0, 99],
                     notes: str = None):
    await i.response.defer(ephemeral=True)
    fx = get_fx(fixture)
    if not fx:
        return await i.followup.send("Fixture not found. Pick one from the list.")
    if fx["status"] == "played":
        return await i.followup.send("That fixture already has a result. Use /result_remove first.")
    save_result(fx["id"], home_score, away_score, notes)
    fx = get_fx(fx["id"])
    where = await announce_result(fx)
    await i.followup.send(f"✅ Saved: **{name_of(fx['home_id'])} {home_score}–{away_score} {name_of(fx['away_id'])}**. {where}")

@bot.tree.command(description="Record a result for a match that wasn't scheduled (friendly, make-up game)")
@staff
@app_commands.autocomplete(home=team_ac, away=team_ac)
async def result_add(i: discord.Interaction, home: str, away: str,
                     home_score: app_commands.Range[int, 0, 99], away_score: app_commands.Range[int, 0, 99],
                     gameweek: int = 0, notes: str = None):
    await i.response.defer(ephemeral=True)
    h, a = get_team(home), get_team(away)
    if not h or not a or h["id"] == a["id"]:
        return await i.followup.send("Pick two different, existing teams.")
    now = time.time()
    cur = db.execute("""INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition,brief_sent)
                        VALUES(?,?,?,?,?,?,1)""", (gameweek, h["id"], a["id"], now, h["tier"], "League"))
    save_result(cur.lastrowid, home_score, away_score, notes)
    where = await announce_result(get_fx(cur.lastrowid))
    await i.followup.send(f"✅ Saved: **{h['name']} {home_score}–{away_score} {a['name']}**. {where}")

@bot.tree.command(description="Award a forfeit win (3-0) for a scheduled match")
@staff
@app_commands.autocomplete(fixture=sched_ac, winner=team_ac)
async def forfeit(i: discord.Interaction, fixture: str, winner: str):
    await i.response.defer(ephemeral=True)
    fx, w = get_fx(fixture), get_team(winner)
    if not fx or fx["status"] != "scheduled":
        return await i.followup.send("Scheduled fixture not found.")
    if not w or w["id"] not in (fx["home_id"], fx["away_id"]):
        return await i.followup.send("The winner must be one of the two teams in that fixture.")
    hg, ag = (3, 0) if w["id"] == fx["home_id"] else (0, 3)
    save_result(fx["id"], hg, ag, "Forfeit win", 1)
    where = await announce_result(get_fx(fx["id"]))
    await i.followup.send(f"✅ Forfeit win awarded to **{w['name']}** (3–0). {where}")

@bot.tree.command(description="Undo a result (puts the fixture back on the schedule)")
@staff
@app_commands.autocomplete(fixture=played_ac)
async def result_remove(i: discord.Interaction, fixture: str):
    fx = get_fx(fixture)
    if not fx or fx["status"] != "played":
        return await i.response.send_message("Played fixture not found.", ephemeral=True)
    db.execute("""UPDATE fixtures SET status='scheduled', home_goals=NULL, away_goals=NULL, notes=NULL,
                  played_at=NULL, forfeit=0 WHERE id=?""", (fx["id"],)); db.commit()
    await i.response.send_message(f"↩️ Result removed. Fixture #{fx['id']} is back on the schedule.", ephemeral=True)

@bot.tree.command(description="Recent match results")
@app_commands.autocomplete(team=team_ac)
async def results(i: discord.Interaction, team: str = None, limit: app_commands.Range[int, 1, 20] = 10):
    t = get_team(team) if team else None
    if team and not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    q, args = "SELECT * FROM fixtures WHERE status='played'", []
    if t:
        q += " AND (home_id=? OR away_id=?)"; args += [t["id"], t["id"]]
    rows = db.execute(q + " ORDER BY played_at DESC, id DESC LIMIT ?", args + [limit]).fetchall()
    if not rows:
        return await i.response.send_message("No results yet.", ephemeral=True)
    lines = [f"`#{f['id']}` **{name_of(f['home_id'])}** {f['home_goals']}–{f['away_goals']} **{name_of(f['away_id'])}**"
             + (f" • GW{f['gw']}" if f["gw"] else "") for f in rows]
    title = f"{t['name']} Results" if t else f"{LEAGUE_SHORT} Results"
    await i.response.send_message(embed=discord.Embed(title=title, color=GREEN, description="\n".join(lines)))

@bot.tree.command(name="standings", description="League table (3 pts win, 1 draw)")
@app_commands.autocomplete(tier=tier_ac)
async def standings_cmd(i: discord.Interaction, tier: str = None):
    tiers = [tier] if tier else [r["tier"] for r in db.execute(
        "SELECT DISTINCT tier FROM teams WHERE tier IS NOT NULL ORDER BY tier").fetchall()]
    if not tiers:
        return await i.response.send_message("No teams yet.", ephemeral=True)
    e = discord.Embed(title=f"{LEAGUE_SHORT} Standings", color=PINK, timestamp=discord.utils.utcnow())
    for tr in tiers[:25]:
        rows = standings(tr)
        if rows:
            e.add_field(name=tr, value=table_block(rows), inline=False)
    if not e.fields:
        return await i.response.send_message("No teams in that tier.", ephemeral=True)
    await i.response.send_message(embed=e)

@bot.tree.command(description="A team's record, position and form (defaults to your team)")
@app_commands.autocomplete(team=team_ac)
async def record(i: discord.Interaction, team: str = None):
    t = get_team(team) if team else player_team(i.user.id)
    if not t:
        return await i.response.send_message("Pick a team (you aren't on one).", ephemeral=True)
    r = next((x for x in standings(t["tier"]) if x["team"]["id"] == t["id"]), None)
    fm = " ".join(FORM.get(c, "") for c in form_str(t["id"])) or "—"
    e = discord.Embed(title=f"{t['name']} Record", color=PINK)
    e.add_field(name="Position", value=f"{ordinal(r['pos'])} in {t['tier']}")
    e.add_field(name="Points", value=str(r["pts"]))
    e.add_field(name="W / D / L", value=f"{r['w']} / {r['d']} / {r['l']}")
    e.add_field(name="Goals", value=f"{r['gf']} for • {r['ga']} against ({r['gf'] - r['ga']:+d})")
    e.add_field(name="Form", value=fm)
    nf = next_fixture(t["id"])
    if nf:
        e.add_field(name="Next match", value=next_match_line(t, nf), inline=False)
    if logo(t):
        e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

@bot.tree.command(description="Head-to-head record between two teams")
@app_commands.autocomplete(team_a=team_ac, team_b=team_ac)
async def h2h(i: discord.Interaction, team_a: str, team_b: str):
    a, b = get_team(team_a), get_team(team_b)
    if not a or not b or a["id"] == b["id"]:
        return await i.response.send_message("Pick two different, existing teams.", ephemeral=True)
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND
                         ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
                         ORDER BY played_at DESC, id DESC LIMIT 5""", (a["id"], b["id"], b["id"], a["id"])).fetchall()
    e = discord.Embed(title=f"{a['name']} vs {b['name']}", color=ORANGE,
                      description=h2h_text(a["id"], b["id"]).replace("You:", f"{a['name']}:"))
    if rows:
        e.add_field(name="Recent meetings", inline=False, value="\n".join(
            f"{name_of(f['home_id'])} {f['home_goals']}–{f['away_goals']} {name_of(f['away_id'])}" for f in rows))
    await i.response.send_message(embed=e)

@bot.tree.command(description="List players without a team")
async def freeagents(i: discord.Interaction):
    rows = db.execute("SELECT user_id FROM players WHERE team_id IS NULL").fetchall()
    desc = "\n".join(f"<@{r['user_id']}>" for r in rows[:60]) or "No free agents right now."
    await i.response.send_message(embed=discord.Embed(title="Free Agents", color=PINK, description=desc))

# ---------------------------------------------------------------- settings
@bot.tree.command(description="Choose where the bot posts: transactions, matchdays or results")
@staff
@app_commands.choices(kind=[app_commands.Choice(name="Transactions", value="tx_channel"),
                            app_commands.Choice(name="Matchdays", value="matchday_channel"),
                            app_commands.Choice(name="Results", value="results_channel")])
async def setchannel(i: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel):
    set_setting(kind.value, str(channel.id)); set_setting("guild_id", str(i.guild_id))
    await i.response.send_message(f"✅ **{kind.name}** will post in {channel.mention}", ephemeral=True)

@bot.tree.command(description="Set the league timezone used when entering fixture times (e.g. America/New_York)")
@staff
async def timezone(i: discord.Interaction, name: str):
    try:
        ZoneInfo(name)
    except Exception:
        return await i.response.send_message("Unknown timezone. Use a name like `America/New_York`, `Europe/London` or `UTC`.", ephemeral=True)
    set_setting("timezone", name)
    await i.response.send_message(f"✅ Fixture times are now read as **{name}**. Everyone sees them in their own local time.", ephemeral=True)

@bot.tree.command(description="Set the text shown as the 'matchday brief' in matchday messages")
@staff
async def brief(i: discord.Interaction, text: str):
    set_setting("brief_text", text[:500])
    await i.response.send_message("✅ Matchday brief updated.", ephemeral=True)

# ---------------------------------------------------------------- league reminders (every 2 weeks)
REMINDER_KINDS = ["next", "rivals", "pulse"]

def week_stats():
    rows = db.execute("SELECT kind, COUNT(*) n FROM events WHERE ts >= datetime('now','-7 days') GROUP BY kind").fetchall()
    c = {r["kind"]: r["n"] for r in rows}
    active = db.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= datetime('now','-7 days')").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    return dict(signings=c.get("sign", 0), transfers=c.get("transfer", 0), loans=c.get("loan", 0),
                moves=c.get("sign", 0) + c.get("transfer", 0) + c.get("loan", 0), active=active, total=total)

def reminder_embed(kind, team):
    st = week_stats()
    if kind == "next":
        e = discord.Embed(title="🧐 Do You Know Who You're Playing Next?", color=YELLOW, description=(
            "The best managers don't just pick a team — they **study the opponent first**.\n\n"
            "> 📊 **Opponent Analysis** — Break down any team's roster and strengths\n"
            "> 🧠 See their best players, weak positions, and recent form\n"
            "> 🎯 Build your game plan before kick-off\n\n"
            "⚠️ This feature is **only available on your dashboard**.\n\n"
            "Managers who prepare win more. It's that simple."))
    elif kind == "rivals":
        e = discord.Embed(title="🔥 Your Rivals Are Making Moves", color=YELLOW, description=(
            f"**{st['moves']}** transfers and signings happened in **{LEAGUE_SHORT}** this week.\n\n"
            f"> {st['active']} managers were active — were you one of them?\n\n"
            "Other teams are strengthening. Don't get left behind."))
    else:
        e = discord.Embed(title=f"📈 {LEAGUE_SHORT} Weekly Pulse", color=GREEN, description=(
            f"This week in **{LEAGUE_SHORT}**:\n\n"
            f"> 🏟️ **{st['active']}** of **{st['total']}** managers were active\n"
            f"> ✍️ **{st['signings']}** new signings\n"
            f"> 💰 **{st['transfers']}** transfers\n"
            f"> 🔄 **{st['loans']}** loans\n\n"
            "The active managers are pulling ahead. Join them."))
    if kind == "next" and team:
        nf = next_fixture(team["id"])
        if nf:
            e.add_field(name="🗓️ Your next match", value=next_match_line(team, nf), inline=False)
    e.set_author(name=f"{LEAGUE_SHORT} • Manager HQ", icon_url=LEAGUE_LOGO)
    e.set_footer(text=footer_text(team))
    e.timestamp = discord.utils.utcnow()
    return e

async def send_reminders(kind):
    sent = failed = 0
    for p in db.execute("SELECT user_id, team_id FROM players WHERE team_id IS NOT NULL").fetchall():
        try:
            u = await bot.fetch_user(p["user_id"])
            await u.send(embed=reminder_embed(kind, team_by_id(p["team_id"])), view=dashboard_view())
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(0.6)
    return sent, failed

@tasks.loop(hours=1)
async def reminder_loop():
    now, last = time.time(), get_setting("last_reminder")
    if last is None:                     # first run: start the 2-week clock
        return set_setting("last_reminder", str(now))
    if now - float(last) >= REMINDER_DAYS * 86400:
        idx = int(get_setting("reminder_idx") or 0)
        set_setting("last_reminder", str(now)); set_setting("reminder_idx", str(idx + 1))
        await send_reminders(REMINDER_KINDS[idx % len(REMINDER_KINDS)])

@reminder_loop.before_loop
async def _wait():
    await bot.wait_until_ready()

@bot.tree.command(description="Send a league reminder to all rostered players now (test)")
@staff
@app_commands.choices(kind=[app_commands.Choice(name="Who you're playing next", value="next"),
                            app_commands.Choice(name="Rivals making moves", value="rivals"),
                            app_commands.Choice(name="Weekly pulse", value="pulse")])
async def reminder_now(i: discord.Interaction, kind: app_commands.Choice[str]):
    await i.response.defer(ephemeral=True)
    sent, failed = await send_reminders(kind.value)
    await i.followup.send(f"📨 Reminder sent: {sent} DMs, {failed} failed.")

@bot.tree.command(description="Set the role that can use staff commands (admins always can)")
@staff
async def staffrole(i: discord.Interaction, role: discord.Role):
    if not i.user.guild_permissions.manage_guild:
        return await i.response.send_message("Only admins can change the staff role.", ephemeral=True)
    set_setting("staff_role", str(role.id))
    await i.response.send_message(f"✅ Members with {role.mention} can now use staff commands.", ephemeral=True)

@bot.tree.error
async def on_app_error(i: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if str(error) == "not staff or manager":
            msg = "🚫 Only staff and team managers can use this. Ask staff to add you with /manager_add."
        else:
            msg = "🚫 Staff only. You need Administrator, Manage Server, or the staff role (/staffrole)."
    else:
        orig = getattr(error, "original", error)
        traceback.print_exception(type(orig), orig, orig.__traceback__)
        msg = f"⚠️ Something went wrong: {type(orig).__name__}: {str(orig)[:200]}"
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.event
async def setup_hook():
    bot.add_dynamic_items(OfferButton)
    reminder_loop.start()
    matchday_loop.start()
    if GUILD_ID:
        # server-only commands (appear instantly); wipe the global copies that cause duplicates
        g = discord.Object(int(GUILD_ID))
        bot.tree.copy_global_to(guild=g)
        await bot.tree.sync(guild=g)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    else:
        await bot.tree.sync()

_cleaned = False

@bot.event
async def on_ready():
    global _cleaned
    print(f"Logged in as {bot.user}")
    if not GUILD_ID and not _cleaned:   # remove old per-server copies that cause duplicates
        _cleaned = True
        for g in bot.guilds:
            bot.tree.clear_commands(guild=g)
            try:
                await bot.tree.sync(guild=g)
            except discord.HTTPException:
                pass

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in your host's environment/variables panel or in a .env file.")
bot.run(TOKEN)
