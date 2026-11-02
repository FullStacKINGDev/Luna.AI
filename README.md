# 🤖 Discord Bot

A feature-rich Discord bot for server customization — built with Python + discord.py.

## ✨ Features

| Feature | Command |
|---|---|
| Auto-create channels & categories | `/setup-server` |
| Auto-create roles with colors | `/setup-roles` |
| Reaction roles system | `/reaction-role #channel` |

## 🚀 Quick Start

### 1. Create Your Bot
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → Name it → Go to **Bot** tab
3. Click **Reset Token** → Copy your token
4. Enable these **Privileged Gateway Intents**:
   - ✅ Server Members Intent
   - ✅ Message Content Intent

### 2. Invite Your Bot
Use this URL (replace `YOUR_CLIENT_ID`):
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot%20applications.commands
```

### 3. Configure
Open `.env` and paste your bot token:
```env
BOT_TOKEN=paste_your_token_here
GUILD_ID=1250439303700549632
```

### 4. Install & Run
```bash
pip install -r requirements.txt
python bot.py
```

## 📁 Project Structure
```
Discord Automation/
├── bot.py                  # Main entry point
├── .env                    # Secret credentials (never share!)
├── requirements.txt        # Dependencies
├── reaction_roles.json     # Auto-generated reaction role data
└── cogs/
    ├── server_setup.py     # /setup-server & /setup-roles
    └── reaction_roles.py   # /reaction-role command + listeners
```

## ⚠️ Important
- **Never share your `.env` file or Bot Token with anyone**
- Make sure your bot has **Administrator** permission in your server
