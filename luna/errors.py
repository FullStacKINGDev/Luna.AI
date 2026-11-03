"""One place that turns slash-command failures into friendly replies."""
from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger("luna.errors")


async def respond_to_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Administrator** permission to use this."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = ("I'm missing permissions here. Give my role **Manage Roles** and **Manage Channels** "
                   "(or Administrator), then try again.")
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Easy there. Try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This only works inside a server."
    else:
        name = interaction.command.name if interaction.command else "?"
        log.error("Unhandled error in /%s", name, exc_info=error)
        message = "Something went wrong on my side. Try again in a moment."

    if interaction.response.is_done():
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ {message}", ephemeral=True)
