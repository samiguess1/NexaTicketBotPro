NEXA.PM TICKET BOT PRO
=====================

FILES
-----
bot.py              Complete ticket bot
requirements.txt    Python packages
.env.example        Environment variable template
.gitignore          Keeps tokens and local data out of Git

SETUP (VS CODE)
---------------
1. Install Python 3.10 or newer and VS Code.
2. Open this folder in VS Code.
3. Open Terminal > New Terminal.
4. Create and activate a virtual environment:

   macOS/Linux:
   python3 -m venv .venv
   source .venv/bin/activate

   Windows PowerShell:
   py -m venv .venv
   .venv\Scripts\Activate.ps1

5. Install dependencies:

   python -m pip install -r requirements.txt

6. Open the included .env file and replace PASTE_YOUR_NEXA_BOT_TOKEN_HERE
   with your own Nexa.pm bot token.
7. Run:

   python bot.py

8. In your server, run /ticket-setup and choose the panel text channel, default
   ticket category, staff role, and optional transcript log channel from the
   command's selectors.

HOST ON BOT-HOSTING
-------------------
1. Create a Python deployment from the public GitHub repository:
   https://github.com/samiguess1/NexaTicketBotPro
2. Set the entry file to bot.py. The host installs packages from requirements.txt.
3. Add BOT_TOKEN in the deployment's Environment settings and mark it as secret.
   Do not upload .env or put the token in GitHub.
4. Start the deployment and check its console for "Nexa.pm Ticket Bot PRO is online".

ADD THE BOT TO A NEXA SERVER
----------------------------
1. Sign in to the Nexa instance where the server lives (for this bot, https://nexa.pm).
2. In Nexa, open Settings > Developer Portal and select this bot's app.
3. Open the app's Install tab and enable installation in servers. Configure the
   bot permissions and application commands the ticket bot needs, then save.
4. Copy the install link generated on that tab and open it while signed in to
   Nexa. Choose a server you manage and approve the installation.
5. Share that Nexa install link with other server admins. Do not use a
   discord.com/oauth2 link; it belongs to Discord, not Nexa.

If the link says the app is private, unavailable, or broken, verify server
installation is enabled and copy a fresh link from the app's Install tab. The
bot must be added separately to each server; running bot.py only connects it
and does not install it into a server.

10. The bot posts a ticket panel with four ticket types.

COMMANDS
--------
/say                Send a message as the bot in the current channel (administrators only)
/say-in             Private command for @ok to post in an accessible text channel
/ticket-setup       Configure panel, category, staff role, log channel
/ticket-config      View configuration
/ticket-panel       Post an additional panel
/ticket-category    Set a separate category for one ticket type
/claim              Claim the current ticket
/close              Close with a required reason; channel is deleted after 30 seconds
/add                Add a member to a ticket
/remove             Remove a member from a ticket
/transfer           Assign ticket to another staff member
/ticket-stats       View ticket statistics
/ticket-reopen      Reopen a closed ticket
/ticket-delete      Warn, wait 30 seconds, then delete a closed ticket channel

FEATURES
--------
- Private ticket channels
- Four ticket types
- Optional per-type categories
- Staff claiming and transfer
- Add/remove participants
- Plain-text transcripts
- Optional transcript log channel
- Inactivity auto-close (default 72 hours)
- Ticket ratings (1-5 stars)
- Rating prompt sent by DM when a ticket closes (when DMs are allowed)
- Close reason shown in the ticket closure notice and transcript event log
- Closed by /close or the Close button: channel deletion scheduled after 30 seconds
- SQLite persistence
- Persistent ticket panel and control views
- Per-user open-ticket limit (default 3)

IMPORTANT NEXA.PM COMPATIBILITY NOTE
------------------------------------
The code applies the Nexa.pm REST, Gateway, and CDN base overrides requested.
It uses discord.py 2.x features: application commands, select menus, buttons,
channel permission overwrites, and gateway interactions.

I could not verify public Nexa.pm developer documentation confirming that
every Discord API endpoint and interaction feature used here is supported.
A Discord-compatible URL alone does not guarantee full API compatibility.
If Nexa.pm has its own fork/version of discord.py or additional setup steps,
those must match its current developer documentation.

BOT PERMISSIONS
---------------
Grant only what the bot needs:
- View Channels
- Send Messages
- Read Message History
- Manage Channels
- Manage Roles (only if your setup requires it)
- Embed Links
- Attach Files
- Use Application Commands

Place the bot's highest role above the configured staff role if it needs to
manage that role. Do not grant Administrator unless truly necessary.

TOKEN SAFETY
------------
Never send your bot token to anyone, commit .env, or paste it into screenshots.
If the token is exposed, regenerate it in the Nexa.pm bot dashboard.

DATA
----
The bot creates tickets.sqlite3 and a transcripts/ folder automatically.
Keep backups of both. Ticket records are stored locally on the machine running
the bot. The bot must remain running for automatic closing to happen.
