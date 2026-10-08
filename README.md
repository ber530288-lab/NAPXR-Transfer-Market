# PitchX Transfer Market Bot

## Setup
1. https://discord.com/developers/applications → New Application → Bot → copy token.
   Enable **Server Members Intent**. Invite with scopes `bot` + `applications.commands`.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, fill in token (+ GUILD_ID, LEAGUE_LOGO_URL).
4. `python bot.py`

## First steps in your server
- `/setup #transactions`
- `/team_add name:Elite Manchester United tier:D-Tier`  (logo_url optional, add later with `/team_logo`)
- `/sign @player team` · `/release @player` · `/transfer @player team`
- `/matchday gameweek:3 home:... away:...` → DMs every rostered player a brief
- `/teams`, `/roster`, `/profile` for everyone

Staff commands require the **Manage Server** permission.

## Hosting on a panel host (upload-and-run style)
1. Upload everything in this folder (bot.py, main.py, requirements.txt) to your server's files.
2. Set the startup file to `bot.py` (or `main.py`). Python 3.10+ is needed.
3. Install requirements: most panels do this automatically from requirements.txt,
   otherwise run `pip install -r requirements.txt` in the console.
4. Add variables in the host's Startup/Environment tab (or upload a `.env` file):
   DISCORD_TOKEN, GUILD_ID, LEAGUE_LOGO_URL, PROFILE_URL
5. Start the bot. Keep `pitchx.db` in place - it holds your teams and rosters.
   Back it up before reinstalling or wiping the server.
