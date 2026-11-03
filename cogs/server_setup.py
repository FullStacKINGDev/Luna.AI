"""Starter-template commands: no AI needed.

/setup-roles and /setup-server build a fixed, sensible layout through the same
engine /customize uses, so they are create-only and safe to re-run.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from luna.applier import apply_plan
from luna.embeds import build_result_embed
from luna.errors import respond_to_error
from luna.plan import ServerPlan, parse_plan

OWNER, ADMIN, MOD, VIP, MEMBER = "👑 Owner", "⚡ Admin", "🛡️ Moderator", "🎖️ VIP", "✅ Member"
STAFF = [OWNER, ADMIN, MOD]


def _channel(name, *, kind="text", topic="", read_only=False, writers=()):
    return {"name": name, "kind": kind, "topic": topic, "read_only": read_only,
            "writers": list(writers), "private_to": []}


# ── Edit this to change what /setup-roles and /setup-server build ────────────
STARTER_TEMPLATE = {
    "summary": "A general-purpose community with a staff-only area.",
    "roles": [
        {"name": OWNER,  "color": "#F1C40F", "hoist": True,  "mentionable": False, "permissions": []},
        {"name": ADMIN,  "color": "#E74C3C", "hoist": True,  "mentionable": False, "permissions": []},
        {"name": MOD,    "color": "#3498DB", "hoist": True,  "mentionable": True,
         "permissions": ["manage_messages", "manage_threads", "moderate_members", "mute_members",
                         "deafen_members", "move_members", "view_audit_log"]},
        {"name": VIP,    "color": "#9B59B6", "hoist": True,  "mentionable": True,  "permissions": []},
        {"name": MEMBER, "color": "#2ECC71", "hoist": False, "mentionable": True,  "permissions": []},
    ],
    "categories": [
        {"name": "📢 Information", "private_to": [], "channels": [
            _channel("announcements", kind="announcement", read_only=True, writers=STAFF),
            _channel("rules", read_only=True, writers=STAFF),
            _channel("server-guide", read_only=True, writers=STAFF),
        ]},
        {"name": "💬 General", "private_to": [], "channels": [
            _channel("general"), _channel("bot-commands"), _channel("media"), _channel("memes"),
        ]},
        {"name": "🎮 Gaming", "private_to": [], "channels": [
            _channel("gaming-chat"), _channel("looking-for-group"),
        ]},
        {"name": "🎵 Music", "private_to": [], "channels": [_channel("music-commands")]},
        {"name": "🔧 Admin", "private_to": STAFF, "channels": [
            _channel("logs"), _channel("admin-chat"),
        ]},
    ],
}


def starter_plan(*, roles: bool, channels: bool) -> ServerPlan:
    plan = parse_plan(STARTER_TEMPLATE)
    if not roles:
        plan.roles = []
    if not channels:
        plan.categories = []
    return plan


class ServerSetup(commands.Cog):
    """Cog for setting up channels, categories, and roles from a fixed template."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, interaction: discord.Interaction, plan: ServerPlan, command: str) -> None:
        await interaction.response.defer(ephemeral=True)
        report = await apply_plan(
            interaction.guild, plan,
            reason=f"Luna.AI /{command} by {interaction.user} ({interaction.user.id})",
        )
        embed = build_result_embed(report)
        if report.nothing_to_do and not report.errors and not report.aborted:
            embed.title = "ℹ️ Everything already exists"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="setup-server", description="Create the starter channels and categories")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_server(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, starter_plan(roles=False, channels=True), "setup-server")

    @app_commands.command(name="setup-roles", description="Create the starter roles with colors")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def setup_roles(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, starter_plan(roles=True, channels=False), "setup-roles")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await respond_to_error(interaction, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerSetup(bot))
