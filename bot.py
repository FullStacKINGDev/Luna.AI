import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Fix Windows console encoding (emoji in log lines)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"

# ── Load environment variables ───────────────────────────────────────────────
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("BOT_TOKEN")

log = logging.getLogger("luna")


class LunaBot(commands.Bot):
    def __init__(self):
        # Only non-privileged intents: slash commands and reaction events need nothing more.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())

    async def setup_hook(self):
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            await self.load_extension(f"cogs.{path.stem}")
            log.info("Loaded cog: %s", path.name)

        synced = await self.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))

    async def on_ready(self):
        log.info("Online as %s (%s) in %d server(s)", self.user, self.user.id, len(self.guilds))


async def main():
    if not TOKEN:
        sys.exit("BOT_TOKEN is missing. Copy .env.example to .env and paste your bot token into it.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[WARN] ANTHROPIC_API_KEY is not set: /customize will not work until you add it to .env. "
              "Everything else runs normally.")

    discord.utils.setup_logging(level=logging.INFO)
    async with LunaBot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
