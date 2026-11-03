"""/customize - describe what you want, review Luna's plan, then she carries it out."""
from __future__ import annotations

import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from luna.applier import ServerIndex, apply_plan, full_plan_text, snapshot_guild
from luna.embeds import REVIEW_TIMEOUT, build_preview_embed, build_result_embed
from luna.errors import respond_to_error
from luna.plan import ServerPlan
from luna.planner import PlannerError, generate_plan

log = logging.getLogger("luna.customize")

PROGRESS_INTERVAL = 2.0     # seconds between progress edits (keeps clear of rate limits)
CONFIRM_WORD = "CONFIRM"


class ConfirmModal(discord.ui.Modal, title="Confirm these changes"):
    """Risky plans (deletions, powerful permissions) need the word typed out."""

    answer = discord.ui.TextInput(label=f"Type {CONFIRM_WORD} to apply", placeholder=CONFIRM_WORD,
                                  min_length=1, max_length=20)

    def __init__(self, view: "ConfirmView"):
        super().__init__()
        self.confirm_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.answer.value.strip().upper() != CONFIRM_WORD:
            await interaction.response.send_message(
                f"That wasn't `{CONFIRM_WORD}`, so nothing was changed. Press the button to try again.", ephemeral=True)
            return
        await self.confirm_view.run(interaction)


class ConfirmView(discord.ui.View):
    """Apply / Cancel buttons, usable only by the admin who ran the command."""

    def __init__(self, cog: "Customizer", interaction: discord.Interaction, plan: ServerPlan, index: ServerIndex):
        super().__init__(timeout=REVIEW_TIMEOUT)
        self.cog = cog
        self.origin = interaction
        self.owner_id = interaction.user.id
        self.plan = plan
        self.index = index
        self.risky = bool(plan.risk_reasons())
        if self.risky:
            self.apply_button.label = "Apply changes…"
            self.apply_button.style = discord.ButtonStyle.danger
            self.apply_button.emoji = "⚠️"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not interaction.permissions.administrator:
            await interaction.response.send_message("Only the admin who ran `/customize` can use these buttons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.origin.edit_original_response(
                content="⌛ That plan expired and nothing was changed. Run `/customize` again to start over.",
                embed=None, view=None, attachments=[],
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.success, emoji="🛠️")
    async def apply_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.risky:
            await interaction.response.send_modal(ConfirmModal(self))
        else:
            await self.run(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", embed=None, view=None, attachments=[])

    async def run(self, interaction: discord.Interaction) -> None:
        """Carry the plan out. ``interaction`` is the button press or the confirmed modal."""
        guild = interaction.guild
        if guild.id in self.cog.busy:
            await interaction.response.send_message("I'm already working in this server. Wait for it to finish.", ephemeral=True)
            return
        self.cog.busy.add(guild.id)
        self.stop()
        await interaction.response.edit_message(content="🛠️ Working… this can take a minute.", embed=None, view=None, attachments=[])

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
                guild, self.plan, index=self.index, progress=progress,
                reason=f"Luna.AI /customize by {interaction.user} ({interaction.user.id})",
            )
        except Exception:
            log.exception("apply_plan crashed in guild %s", guild.id)
            await interaction.edit_original_response(
                content="❌ Something went wrong part-way. Some changes may have been made; "
                        "run `/customize` again to see what's left. A backup of the old structure is in the bot's `backups` folder.",
                embed=None,
            )
            return
        finally:
            self.cog.busy.discard(guild.id)

        attachments = [discord.File(report.backup_path, filename="luna-backup.json")] if report.backup_path else []
        await interaction.edit_original_response(content=None, embed=build_result_embed(report), attachments=attachments)


class Customizer(commands.Cog):
    """Prompt-driven server management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.busy: set[int] = set()   # guild ids currently being changed

    @app_commands.command(name="customize", description="Describe what you want and I'll create, edit or delete roles and channels")
    @app_commands.describe(prompt="e.g. a study group for NEET students with a private mentor area, or: rename #chat to #general")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, manage_channels=True)
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.guild_id)
    async def customize(self, interaction: discord.Interaction, prompt: app_commands.Range[str, 10, 1500]) -> None:
        guild = interaction.guild
        if guild.id in self.busy:
            await interaction.response.send_message("I'm already working in this server. Wait for it to finish.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        snapshot, index = snapshot_guild(guild)
        try:
            plan = await generate_plan(prompt, snapshot, index.kinds)
        except PlannerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        preview = await apply_plan(guild, plan, index=index, dry_run=True)
        embed = build_preview_embed(plan, preview, index)

        if preview.nothing_to_do:
            embed.set_footer(text="Everything in this plan already matches the server, so there's nothing to do.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        files = []
        if plan.has_changes_to_existing:
            text = full_plan_text(plan, preview, index)
            files.append(discord.File(io.BytesIO(text.encode("utf-8")), filename="luna-plan.txt"))
        await interaction.followup.send(embed=embed, view=ConfirmView(self, interaction, plan, index),
                                        files=files, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await respond_to_error(interaction, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Customizer(bot))
