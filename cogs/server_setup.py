import discord
from discord.ext import commands
from discord import app_commands
import os

GUILD_ID = int(os.getenv("GUILD_ID"))

class ServerSetup(commands.Cog):
    """Cog for setting up channels, categories, and roles."""

    def __init__(self, bot):
        self.bot = bot

    # ── /setup-server ──────────────────────────────────────────────────────
    @app_commands.command(name="setup-server", description="Auto-setup channels and categories for this server")
    @app_commands.default_permissions(administrator=True)
    async def setup_server(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # ── Define your server layout here ──────────────────────────────
        layout = {
            "📢 Information": ["📣・announcements", "📜・rules", "🗺️・server-guide"],
            "💬 General": ["💬・general", "🤖・bot-commands", "📸・media", "😂・memes"],
            "🎮 Gaming": ["🎮・gaming-chat", "🔊・looking-for-group"],
            "🎵 Music": ["🎵・music-commands"],
            "🔧 Admin": ["📋・logs", "⚙️・admin-chat"],
        }

        created = []
        for category_name, channels in layout.items():
            # Create category if it doesn't exist
            category = discord.utils.get(guild.categories, name=category_name)
            if not category:
                category = await guild.create_category(category_name)

            for ch_name in channels:
                existing = discord.utils.get(guild.text_channels, name=ch_name.split("・")[1] if "・" in ch_name else ch_name)
                if not existing:
                    await guild.create_text_channel(ch_name.split("・")[1] if "・" in ch_name else ch_name, category=category)
                    created.append(ch_name)

        if created:
            await interaction.followup.send(f"✅ Created {len(created)} channel(s):\n" + "\n".join(f"  • {c}" for c in created), ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ All channels already exist!", ephemeral=True)

    # ── /setup-roles ───────────────────────────────────────────────────────
    @app_commands.command(name="setup-roles", description="Auto-create server roles with colors")
    @app_commands.default_permissions(administrator=True)
    async def setup_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # ── Define your roles here (name, color, hoist, mentionable) ────
        roles_to_create = [
            {"name": "👑 Owner",     "color": discord.Color.gold(),         "hoist": True},
            {"name": "⚡ Admin",      "color": discord.Color.red(),          "hoist": True},
            {"name": "🛡️ Moderator", "color": discord.Color.blue(),         "hoist": True},
            {"name": "🎖️ VIP",       "color": discord.Color.purple(),       "hoist": True},
            {"name": "✅ Member",     "color": discord.Color.green(),        "hoist": False},
            {"name": "🔇 Muted",      "color": discord.Color.dark_gray(),    "hoist": False},
        ]

        created = []
        for role_data in roles_to_create:
            existing = discord.utils.get(guild.roles, name=role_data["name"])
            if not existing:
                await guild.create_role(
                    name=role_data["name"],
                    color=role_data["color"],
                    hoist=role_data["hoist"],
                    mentionable=True
                )
                created.append(role_data["name"])

        if created:
            await interaction.followup.send(f"✅ Created {len(created)} role(s):\n" + "\n".join(f"  • {r}" for r in created), ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ All roles already exist!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerSetup(bot))
