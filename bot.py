import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "VCL X NAPXR.GG | PitchX")
LEAGUE_LOGO = os.getenv("LEAGUE_LOGO_URL") or None
PROFILE_URL = os.getenv("PROFILE_URL", "https://discord.com/users/{user_id}")

PINK, ORANGE, GREEN = 0xE91E63, 0xF57C00, 0x2ECC71
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
""")

def get_team(name):
    return db.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()

def team_by_id(tid):
    return db.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone() if tid else None

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
    v.add_item(discord.ui.Button(label="View Profile", emoji="↗️",
                                 url=PROFILE_URL.format(user_id=user_id)))
    return v

def base_embed(title, desc, color, section):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{section}", icon_url=LEAGUE_LOGO)
    return e

def tx_embed(title, desc, quote, team, color=PINK):
    e = base_embed(title, f"{desc}\n\n{quote}", color, f"{LEAGUE_NAME} Transactions")
    if team and team["logo"]:
        e.set_thumbnail(url=team["logo"])
    return e

async def announce(guild, embed, user_id, content=None):
    """Post to the transactions channel and DM the player. Returns DM success."""
    ch_id = get_setting("tx_channel")
    if ch_id and (ch := guild.get_channel(int(ch_id))):
        await ch.send(content=content, embed=embed, view=profile_view(user_id))
    try:
        user = guild.get_member(user_id) or await bot.fetch_user(user_id)
        await user.send(embed=embed, view=profile_view(user_id))
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

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
    r = db.execute("SELECT * FROM players WHERE user_id=?", (player.id,)).fetchone()
    t = team_by_id(r["team_id"]) if r else None
    e = discord.Embed(title=player.display_name, color=PINK)
    e.add_field(name="Club", value=t["name"] if t else "Free Agent")
    e.add_field(name="Tier", value=t["tier"] if t else "—")
    e.set_thumbnail(url=player.display_avatar.url)
    await i.response.send_message(embed=e)

# ---------------------------------------------------------------- transfers
@bot.tree.command(description="Sign a player to a team")
@staff
@app_commands.autocomplete(team=team_ac)
async def sign(i: discord.Interaction, player: discord.Member, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found. Use /team_add first.", ephemeral=True)
    db.execute("INSERT OR REPLACE INTO players VALUES(?,?,datetime('now'))", (player.id, t["id"])); db.commit()
    e = tx_embed("You've Been Signed!", f"You have signed a contract with **{t['name']}**!",
                 f"> 🏟️ **New Club:** {t['name']}\n\nWelcome to the team! ⚽", t)
    await i.response.defer(ephemeral=True)
    dm = await announce(i.guild, e, player.id, content=player.mention)
    await i.followup.send(f"✅ Signed {player.mention} to **{t['name']}**" + ("" if dm else " (couldn't DM them)"))

@bot.tree.command(description="Release a player (becomes a free agent)")
@staff
async def release(i: discord.Interaction, player: discord.Member):
    r = db.execute("SELECT * FROM players WHERE user_id=?", (player.id,)).fetchone()
    t = team_by_id(r["team_id"]) if r else None
    if not t:
        return await i.response.send_message("That player isn't on a team.", ephemeral=True)
    db.execute("UPDATE players SET team_id=NULL WHERE user_id=?", (player.id,)); db.commit()
    e = tx_embed("You've Been Released", f"You have been released from **{t['name']}**.",
                 f"> 🏟️ **Former Club:** {t['name']}\n> 📋 **Status:** Free Agent\n\n"
                 "You are now free to sign with any team.", t)
    await i.response.defer(ephemeral=True)
    dm = await announce(i.guild, e, player.id, content=player.mention)
    await i.followup.send(f"✅ Released {player.mention}" + ("" if dm else " (couldn't DM them)"))

@bot.tree.command(description="Transfer a player from one team to another")
@staff
@app_commands.autocomplete(to_team=team_ac)
async def transfer(i: discord.Interaction, player: discord.Member, to_team: str):
    new = get_team(to_team)
    if not new:
        return await i.response.send_message("Team not found.", ephemeral=True)
    r = db.execute("SELECT * FROM players WHERE user_id=?", (player.id,)).fetchone()
    old = team_by_id(r["team_id"]) if r else None
    db.execute("INSERT OR REPLACE INTO players VALUES(?,?,datetime('now'))", (player.id, new["id"])); db.commit()
    e = tx_embed("Transfer Complete", f"You have been transferred to **{new['name']}**!",
                 f"> 🔁 **From:** {old['name'] if old else 'Free Agent'}\n> 🏟️ **To:** {new['name']}", new, GREEN)
    await i.response.defer(ephemeral=True)
    dm = await announce(i.guild, e, player.id, content=player.mention)
    await i.followup.send(f"✅ Transferred {player.mention} to **{new['name']}**" + ("" if dm else " (couldn't DM them)"))

# ---------------------------------------------------------------- matchday
def matchday_embed(gw, me, opp, is_home, competition, tier, me_pos, me_pts, opp_pos, opp_pts,
                   me_form, opp_form, h2h, brief):
    f = lambda s: " ".join(FORM.get(c, "") for c in s.upper())
    title = f"{me['name']} vs {opp['name']} ({'H' if is_home else 'A'})"
    e = discord.Embed(title=title, color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{LEAGUE_NAME} Matchday — GW {gw}", icon_url=LEAGUE_LOGO)
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
        for p in db.execute("SELECT user_id FROM players WHERE team_id=?", (me["id"],)):
            try:
                u = i.guild.get_member(p["user_id"]) or await bot.fetch_user(p["user_id"])
                await u.send(embed=e, view=profile_view(p["user_id"])); sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
    await i.followup.send(f"📨 Matchday GW{gameweek}: {sent} DMs sent, {failed} failed (DMs closed).")

@bot.event
async def setup_hook():
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
