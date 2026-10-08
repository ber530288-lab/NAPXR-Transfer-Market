import asyncio
import os
import re
import sqlite3
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "VCL X NAPXR.GG | PitchX")
LEAGUE_SHORT = os.getenv("LEAGUE_SHORT", "PitchX")
LEAGUE_LOGO = os.getenv("LEAGUE_LOGO_URL") or None
PROFILE_URL = os.getenv("PROFILE_URL", "https://discord.com/users/{user_id}")
DASHBOARD_URL = os.getenv("DASHBOARD_URL") or None
REMINDER_DAYS = float(os.getenv("REMINDER_DAYS", "14"))
OFFER_HOURS = 48

PINK, ORANGE, GREEN, YELLOW, RED = 0xE91E63, 0xF57C00, 0x2ECC71, 0xF1C40F, 0xE74C3C
FORM = {"W": "🟢", "D": "🟡", "L": "🔴"}

# ---------------------------------------------------------------- database
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pitchx.db"))
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, logo TEXT, tier TEXT);
CREATE TABLE IF NOT EXISTS players(
  user_id INTEGER PRIMARY KEY, team_id INTEGER, signed_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, user_id INTEGER, team_id INTEGER);
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

def footer_text(team):
    return f"{team['tier']} | {team['name']} • {LEAGUE_SHORT}" if team else LEAGUE_SHORT

def tx_embed(title, desc, quote, team, color=PINK):
    e = discord.Embed(title=title, description=f"{desc}\n\n{quote}", color=color,
                      timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Transactions", icon_url=LEAGUE_LOGO)
    if team and team["logo"]:
        e.set_thumbnail(url=team["logo"])
    return e

async def post_tx(guild, embed, user_id, content=None):
    ch_id = get_setting("tx_channel")
    ch = guild.get_channel(int(ch_id)) if guild and ch_id else None
    if ch:
        await ch.send(content=content, embed=embed, view=profile_view(user_id))
    return ch

staff = app_commands.default_permissions(manage_guild=True)

# ---------------------------------------------------------------- setup / teams
@bot.tree.command(description="Set the channel where transactions are posted")
@staff
async def setup(i: discord.Interaction, channel: discord.TextChannel):
    set_setting("tx_channel", str(channel.id))
    await i.response.send_message(f"✅ Transactions will post in {channel.mention}", ephemeral=True)

@bot.tree.command(description="Add a team to the league")
@staff
async def team_add(i: discord.Interaction, name: str, tier: str = "D-Tier", logo_url: str = None):
    try:
        db.execute("INSERT INTO teams(name,logo,tier) VALUES(?,?,?)", (name, logo_url, tier)); db.commit()
    except sqlite3.IntegrityError:
        return await i.response.send_message("That team already exists.", ephemeral=True)
    await i.response.send_message(f"✅ Added **{name}** ({tier})", ephemeral=True)

@bot.tree.command(description="Set or change a team's logo (image URL)")
@staff
@app_commands.autocomplete(team=team_ac)
async def team_logo(i: discord.Interaction, team: str, logo_url: str):
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
    if t["logo"]: e.set_thumbnail(url=t["logo"])
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
    if logo_team["logo"]:
        e.set_thumbnail(url=logo_team["logo"])
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
    what = {"sign": f"sign with **{team['name'] if team else ''}**",
            "transfer": f"transfer to **{team['name'] if team else ''}**",
            "loan": f"go on loan to **{team['name'] if team else ''}**",
            "release": f"be released from **{from_team['name'] if from_team else ''}**"}[kind]
    try:
        await player.send(embed=embed, view=view)
        await i.followup.send(f"📨 Offer sent to {player.mention} to {what}. Waiting for their answer.")
    except (discord.Forbidden, discord.HTTPException):
        ch_id = get_setting("tx_channel")
        ch = i.guild.get_channel(int(ch_id)) if ch_id else None
        if ch:
            await ch.send(content=player.mention, embed=embed, view=view)
            await i.followup.send(f"📨 Offer for {player.mention} to {what} (couldn't DM them, posted in {ch.mention})")
        else:
            db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (oid,)); db.commit()
            await i.followup.send(f"❌ Couldn't DM {player.mention} and no transactions channel is set (/setup).")

@bot.tree.command(description="Offer a free agent a contract (they must accept)")
@staff
@app_commands.autocomplete(team=team_ac)
async def sign(i: discord.Interaction, player: discord.Member, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found. Use /team_add first.", ephemeral=True)
    if player_team(player.id):
        return await i.response.send_message("That player already has a team. Use /transfer or /release.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await send_offer(i, player, "sign", t, None)

@bot.tree.command(description="Offer a player a transfer to another team (they must accept)")
@staff
@app_commands.autocomplete(to_team=team_ac)
async def transfer(i: discord.Interaction, player: discord.Member, to_team: str):
    new, cur = get_team(to_team), player_team(player.id)
    if not new:
        return await i.response.send_message("Team not found.", ephemeral=True)
    if not cur:
        return await i.response.send_message("That player is a free agent. Use /sign.", ephemeral=True)
    if cur["id"] == new["id"]:
        return await i.response.send_message("They're already on that team.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await send_offer(i, player, "transfer", new, cur)

@bot.tree.command(description="Offer a player a loan move (they must accept)")
@staff
@app_commands.autocomplete(to_team=team_ac)
async def loan(i: discord.Interaction, player: discord.Member, to_team: str):
    new, cur = get_team(to_team), player_team(player.id)
    if not new:
        return await i.response.send_message("Team not found.", ephemeral=True)
    if not cur or cur["id"] == new["id"]:
        return await i.response.send_message("Player needs a different current team to be loaned out.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await send_offer(i, player, "loan", new, cur)

@bot.tree.command(description="Ask a player to be released (they must accept)")
@staff
async def release(i: discord.Interaction, player: discord.Member):
    cur = player_team(player.id)
    if not cur:
        return await i.response.send_message("That player isn't on a team.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await send_offer(i, player, "release", None, cur)

# ---------------------------------------------------------------- matchday
def matchday_embed(gw, me, opp, is_home, competition, tier, me_pos, me_pts, opp_pos, opp_pts,
                   me_form, opp_form, h2h, brief):
    f = lambda s: " ".join(FORM.get(c, "") for c in s.upper())
    e = discord.Embed(title=f"{me['name']} vs {opp['name']} ({'H' if is_home else 'A'})",
                      color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_SHORT} Matchday — GW {gw}", icon_url=LEAGUE_LOGO)
    e.description = f"📍 **{competition}** • {tier}"
    e.add_field(name="📊 League position", inline=False, value=
        f"> You: **{me_pos}** ({me_pts} pts) {f(me_form)}\n> Them: **{opp_pos}** ({opp_pts} pts) {f(opp_form)}")
    e.add_field(name="⚔️ Head to head", inline=False, value=f"> {h2h}")
    e.add_field(name="💡 Your matchday brief", inline=False, value=f"> {brief}")
    e.set_footer(text=f"{me['name']} • {tier}", icon_url=me["logo"] or None)
    if me["logo"]: e.set_thumbnail(url=me["logo"])
    return e

@bot.tree.command(description="Send matchday briefs to both teams' players")
@staff
@app_commands.autocomplete(home=team_ac, away=team_ac)
async def matchday(i: discord.Interaction, gameweek: int, home: str, away: str,
                   competition: str = "League", tier: str = "D-Tier",
                   home_pos: str = "-", home_pts: int = 0, home_form: str = "",
                   away_pos: str = "-", away_pts: int = 0, away_form: str = "",
                   h2h: str = "First meeting this season",
                   brief: str = "Goals (2 pts), assists (2 pts)."):
    h, a = get_team(home), get_team(away)
    if not h or not a:
        return await i.response.send_message("One of those teams doesn't exist.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    sent = failed = 0
    for me, opp, is_home, pos, pts, frm, opos, opts, ofrm in (
        (h, a, True, home_pos, home_pts, home_form, away_pos, away_pts, away_form),
        (a, h, False, away_pos, away_pts, away_form, home_pos, home_pts, home_form)):
        e = matchday_embed(gameweek, me, opp, is_home, competition, tier, pos, pts, opos, opts, frm, ofrm, h2h, brief)
        for p in db.execute("SELECT user_id FROM players WHERE team_id=?", (me["id"],)).fetchall():
            try:
                u = i.guild.get_member(p["user_id"]) or await bot.fetch_user(p["user_id"])
                await u.send(embed=e, view=profile_view(p["user_id"])); sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
    await i.followup.send(f"📨 Matchday GW{gameweek}: {sent} DMs sent, {failed} failed (DMs closed).")

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

@bot.event
async def setup_hook():
    bot.add_dynamic_items(OfferButton)
    reminder_loop.start()
    if GUILD_ID:
        g = discord.Object(int(GUILD_ID))
        bot.tree.copy_global_to(guild=g)
        await bot.tree.sync(guild=g)
    else:
        await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in your host's environment/variables panel or in a .env file.")
bot.run(TOKEN)
