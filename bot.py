import discord
from discord.ext import commands
import os
import sys
from dotenv import load_dotenv

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Load environment variables ──────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Load Cogs ────────────────────────────────────────────────────────────────
async def load_cogs():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py"):
            await bot.load_extension(f"cogs.{filename[:-3]}")
            print(f"  [OK] Loaded cog: {filename}")

# ── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"\n{'='*45}")
    print(f"  [BOT] Online : {bot.user.name}")
    print(f"  [ID]        : {bot.user.id}")
    print(f"  [NET] Servers: {len(bot.guilds)}")
    print(f"{'='*45}\n")

    try:
        synced = await bot.tree.sync()
        print(f"  [CMD] Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"  [ERR] Sync error: {e}")

# ── Run ──────────────────────────────────────────────────────────────────────
async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
