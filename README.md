# 🌙 Luna.AI

A Discord bot that sets up your server for you. Describe the community you want in plain English, review the plan, and Luna builds the roles and channels.

Built with Python + discord.py, and Claude for the planning.

## ✨ Features

| Command | What it does |
|---|---|
| `/customize prompt:<what you want>` | AI designs roles, categories and channels from your description, shows a preview, and builds it when you press **Build it** |
| `/setup-server` | Creates a starter set of channels and categories (no AI needed) |
| `/setup-roles` | Creates a starter set of coloured roles (no AI needed) |
| `/reaction-role #channel roles:🎮=Gamer, 🎵=Music` | Posts a message where members react to give themselves a role |

All commands are administrator-only and server-only.

### How `/customize` works

```
/customize prompt: "A study community for NEET aspirants. Mentors can post in
announcements. Add a private staff area and voice rooms for group study."
```

1. Luna reads your current roles and channels, so the plan fits what is already there.
2. Claude drafts roles, categories, channels, topics and permissions.
3. You get a **preview** with Build / Cancel buttons. Nothing has changed yet.
4. On **Build it**, Luna creates everything and reports what it did.

Safety rules, enforced in code rather than just asked of the model:

- **Create-only.** Existing roles, categories and channels are matched by name and left untouched. Nothing is renamed, moved or deleted, so it is safe to run again.
- **No dangerous permissions.** Generated roles can never get Administrator, Manage Server/Roles/Channels/Webhooks, Kick, Ban or Mention Everyone. You can grant those by hand afterwards.
- **Size limits.** At most 25 roles, 15 categories and 80 channels per run.
- **Admin only.** Only the administrator who ran the command can press the buttons, and the preview expires after 5 minutes.
- **Audit log.** Everything Luna creates is tagged with who asked for it.

## 🚀 Quick Start

### 1. Create your bot
1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** tab.
2. Click **Reset Token** and copy it.
3. No privileged intents are needed. Leave them all off.

### 2. Invite it
Replace `YOUR_CLIENT_ID`:
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=268520528&scope=bot%20applications.commands
```
That grants only what Luna needs: View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, Manage Channels and Manage Roles.

For `/customize` on your own server, giving Luna's role **Administrator** is the simplest option. Luna can only set permissions on a channel that she holds herself, so private channels work most reliably that way. Also keep Luna's role **above** any role you want her to hand out with `/reaction-role`.

### 3. Configure
Copy `.env.example` to `.env` and fill it in:
```env
BOT_TOKEN=your_discord_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key   # needed for /customize
# LUNA_MODEL=claude-sonnet-5-5             # optional, defaults to claude-opus-5-5
```
Get an API key at [console.anthropic.com](https://console.anthropic.com). Without one, everything except `/customize` still works.

### 4. Install & run
```bash
pip install -r requirements.txt
python bot.py
```

### 5. Test (optional)
```bash
python -m unittest discover -s tests -t .
```
The tests use fakes, so they need no Discord or Anthropic credentials.

## 📁 Project Structure
```
Discord Automation/
├── bot.py                   # Entry point: loads cogs, syncs slash commands
├── .env.example             # Template for your secrets (copy to .env)
├── requirements.txt
├── cogs/
│   ├── ai_customizer.py     # /customize and its Build / Cancel buttons
│   ├── server_setup.py      # /setup-server, /setup-roles (starter template)
│   └── reaction_roles.py    # /reaction-role and the reaction listeners
├── luna/
│   ├── plan.py              # Plan schema + validation of the model's output
│   ├── planner.py           # Calls Claude to turn a prompt into a plan
│   ├── applier.py           # Builds a plan in a server (create-only)
│   ├── embeds.py            # Preview and result embeds
│   └── errors.py            # Friendly slash-command error replies
├── tests/
├── docs/                    # GitHub Pages site (home, terms, privacy)
└── reaction_roles.json      # Auto-generated, git-ignored
```

## 🔧 Customising the starter template
Edit `STARTER_TEMPLATE` in [cogs/server_setup.py](cogs/server_setup.py) to change what `/setup-server` and `/setup-roles` build.

## ⚠️ Important
- **Never share or commit `.env`.** It holds your bot token and API key.
- `/customize` sends your description and your server's role, category and channel **names** to Anthropic's API. See the [privacy policy](docs/privacy.html).
- Discord only allows Announcement and Forum channels on **Community** servers. On other servers Luna makes them plain text channels and tells you.
