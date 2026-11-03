"""/customize - describe the server you want, review Luna's plan, then build it."""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from luna.applier import apply_plan, snapshot_guild
from luna.embeds import REVIEW_TIMEOUT, build_preview_embed, build_result_embed
from luna.errors import respond_to_error
from luna.plan import ServerPlan
from luna.planner import PlannerError, generate_plan

log = logging.getLogger("luna.customize")

PROGRESS_INTERVAL = 2.0    # seconds between progress edits (keeps clear of rate limits)


class ConfirmView(discord.ui.View):
    """Build / Cancel buttons, usable only by the admin who ran the command."""

    def __init__(self, cog: "Customizer", interaction: discord.Interaction, plan: ServerPlan):
        super().__init__(timeout=REVIEW_TIMEOUT)
        self.cog = cog
        self.origin = interaction
        self.owner_id = interaction.user.id
        self.plan = plan

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not interaction.permissions.administrator:
            await interaction.response.send_message("Only the admin who ran `/customize` can use these buttons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.origin.edit_original_response(
                content="⌛ That plan expired and nothing was changed. Run `/customize` again to start over.",
                embed=None, view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.success, emoji="🛠️")
    async def build(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild.id in self.cog.busy:
            await interaction.response.send_message("I'm already building something in this server. Wait for it to finish.", ephemeral=True)
            return
        self.cog.busy.add(guild.id)
        self.stop()
        await interaction.response.edit_message(content="🛠️ Building your server… this can take a minute.", embed=None, view=None)

        last_edit = 0.0

        async def progress(text: str) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < PROGRESS_INTERVAL:
                return
            last_edit = now
            try:
                await interaction.edit_original_response(content=f"🛠️ {text}")
            except discord.HTTPException:
                pass

        try:
            report = await apply_plan(
                guild, self.plan, progress=progress,
                reason=f"Luna.AI /customize by {interaction.user} ({interaction.user.id})",
            )
        except Exception:
            log.exception("apply_plan crashed in guild %s", guild.id)
            await interaction.edit_original_response(
                content="❌ Something went wrong while building. Some items may have been created; "
                        "run `/customize` again and I'll skip what already exists.",
                embed=None,
            )
            return
        finally:
            self.cog.busy.discard(guild.id)

        await interaction.edit_original_response(content=None, embed=build_result_embed(report))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", embed=None, view=None)


class Customizer(commands.Cog):
    """Prompt-driven server customisation."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.busy: set[int] = set()   # guild ids currently being built

    @app_commands.command(name="customize", description="Describe your server and I'll design its roles and channels")
    @app_commands.describe(prompt="What should the server be? e.g. a study group for NEET students with a private mentor area")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, manage_channels=True)
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.guild_id)
    async def customize(self, interaction: discord.Interaction, prompt: app_commands.Range[str, 10, 1500]) -> None:
        guild = interaction.guild
        if guild.id in self.busy:
            await interaction.response.send_message("I'm already building something in this server. Wait for it to finish.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            plan = await generate_plan(prompt, snapshot_guild(guild))
        except PlannerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        preview = await apply_plan(guild, plan, dry_run=True)
        embed = build_preview_embed(plan, preview)

        if preview.nothing_to_do:
            embed.set_footer(text="Everything in this plan already exists, so there's nothing to build.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.followup.send(embed=embed, view=ConfirmView(self, interaction, plan), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await respond_to_error(interaction, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Customizer(bot))
