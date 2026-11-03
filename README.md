# 🌙 Luna.AI

A Discord bot that manages your server for you. Describe what you want in plain English, review the plan, and Luna creates, edits, reorders or deletes roles, categories and channels to match.

Built with Python + discord.py, and OpenAI for the planning.

## ✨ Features

| Command | What it does |
|---|---|
| `/customize prompt:<what you want>` | AI plans the change from your description, shows a preview, and carries it out when you approve |
| `/setup-server` | Creates a starter set of channels and categories (no AI needed) |
| `/setup-roles` | Creates a starter set of coloured roles (no AI needed) |
| `/reaction-role #channel roles:🎮=Gamer, 🎵=Music` | Posts a message where members react to give themselves a role |

All commands are administrator-only and server-only.

### What `/customize` can do

| | Examples |
|---|---|
| **Create** | "A study community for NEET aspirants with a private mentor area and voice rooms" |
| **Edit** | "Rename #chat to #general", "make the Mod role red", "move #memes into the Fun category", "make #staff private to Mods", "make #rules read-only" |
| **Reorder** | "Put the Admin role above Mod", "move the Staff category to the top" |
| **Delete** | "Delete the #old-announcements channel", "delete the Minecraft category and all channels in it" |

How it works:

1. Luna reads your current roles and channels, so the plan fits what is already there.
2. An OpenAI model drafts the plan.
3. You get a **preview** listing every create, edit and deletion. Nothing has changed yet.
4. You press the button. Plans that delete things, or hand out powerful permissions, make you **type CONFIRM** first.
5. Luna carries it out and reports exactly what happened.

### Safety

Enforced in code, not just asked of the model:

- **You approve every change.** Nothing happens until you press the button, only the admin who ran the command can, and the preview expires after 5 minutes.
- **Deletion needs a typed CONFIRM,** and the preview lists every item that will go. Deletion is permanent: Discord cannot restore a deleted channel or its messages.
- **Backup first.** Before editing or deleting anything, Luna saves the server's roles, channels and permissions to `backups/guild-<id>-<time>.json` (git-ignored) and attaches a copy to the result. It holds structure only, not messages, and Discord cannot restore from it automatically, but it is the record to rebuild from.
- **Locked items are untouchable.** Luna can't edit or delete managed (bot/integration) roles, roles at or above her own, `@everyone`, or the channels Discord's Community features depend on (rules, community updates, system messages, safety alerts, AFK). The model is never shown a handle to them.
- **Only what it was shown.** The model refers to existing items by opaque IDs from your server's snapshot. An invented ID is discarded.
- **Cautious by design.** Vague or sweeping requests ("tidy up", "delete everything") are declined with an explanation. Name what you want removed.
- **Powerful permissions are opt-in.** Administrator, Manage Server/Roles/Channels/Webhooks, Kick, Ban and Mention Everyone are only handed to a role if you explicitly ask, and that makes the plan require CONFIRM.
- **Permission edits are merged,** so changing one permission on a channel never wipes the others.
- **Limits.** At most 25 new roles, 15 new categories, 80 new channels, 100 edits and 60 deletions per run.
- **Audit log.** Every change is tagged with who asked for it.

What Luna won't do: assign roles to members, kick or ban, delete or edit messages, or change server settings.

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

For `/customize` on your own server, giving Luna's role **Administrator** is the simplest option. Luna can only set permissions on a channel that she holds herself, so private channels work most reliably that way. Luna can only edit or delete roles **below** her own role (Discord's rule), so drag her role above the roles you want her to manage under Server Settings → Roles. Roles above hers show up as *locked* and are left alone. The same goes for handing roles out with `/reaction-role`.

### 3. Configure
Copy `.env.example` to `.env` and fill it in:
```env
BOT_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key         # needed for /customize
# LUNA_MODEL=gpt-5-mini                    # optional, defaults to gpt-4.1
```
Get an API key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). The account needs billing credit, because each plan is one API call. Without a key, everything except `/customize` still works.

`LUNA_MODEL` can be any OpenAI chat model that supports structured outputs.

### 4. Install & run
```bash
pip install -r requirements.txt
python bot.py
```

### 5. Test (optional)
```bash
python -m unittest discover -s tests -t .
```
The unit tests use fakes, so they need no Discord or OpenAI credentials.

To check the create/edit/delete pipeline against real Discord, make a throwaway server whose name contains **Luna Test**, invite Luna with Administrator, then run:
```bash
python tools/live_e2e.py --guild="Luna Test"            # add --no-model to skip the OpenAI calls
```
It refuses to run on any server without "Luna Test" in the name, because it deletes things.

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
│   ├── planner.py           # Calls OpenAI to turn a prompt into a plan
│   ├── applier.py           # Applies a plan: snapshot, backup, create/edit/reorder/delete
│   ├── embeds.py            # Preview and result embeds
│   └── errors.py            # Friendly slash-command error replies
├── tests/                   # Unit tests (no credentials needed)
├── tools/live_e2e.py        # Live test against a scratch server
├── docs/                    # GitHub Pages site (home, terms, privacy)
├── backups/                 # Structure backups written before edits/deletions (git-ignored)
└── reaction_roles.json      # Auto-generated, git-ignored
```

## 🔧 Customising the starter template
Edit `STARTER_TEMPLATE` in [cogs/server_setup.py](cogs/server_setup.py) to change what `/setup-server` and `/setup-roles` build.

## ⚠️ Important
- **Never share or commit `.env`.** It holds your bot token and OpenAI API key.
- `/customize` sends your description and a snapshot of your server's structure to OpenAI's API: the names, types, colours and short topics (first 80 characters) of its roles, categories and channels, and whether each channel is private. It does not send messages, members or user IDs. See the [privacy policy](docs/privacy.html).
- Discord only allows Announcement and Forum channels on **Community** servers. On other servers Luna makes them plain text channels and tells you.
