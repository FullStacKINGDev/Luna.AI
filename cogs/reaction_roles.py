"""Reaction roles: members react to a message to give themselves a role."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from luna.errors import respond_to_error

log = logging.getLogger("luna.reaction_roles")

# Stored next to bot.py regardless of where the bot is launched from.
DATA_FILE = Path(os.getenv("REACTION_ROLES_FILE") or Path(__file__).resolve().parent.parent / "reaction_roles.json")

# A role carrying any of these can't be handed out through a reaction.
DANGEROUS_PERMISSIONS = discord.Permissions(
    administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
    manage_webhooks=True, manage_expressions=True, ban_members=True, kick_members=True,
    mention_everyone=True,
)

_ROLE_MENTION = re.compile(r"^<@&(\d+)>$")
_PAIR_SPLIT = re.compile(r"[,;\n]+")
_ARROW = re.compile(r"\s*(?:=|->|→)\s*")
MAX_ROLES_PER_MESSAGE = 20  # Discord allows 20 reactions on a message


# ── Storage ──────────────────────────────────────────────────────────────────
def load_data(path: Path = DATA_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        log.exception("Couldn't read %s; starting with no reaction roles", path)
        return {}


def save_data(data: dict, path: Path = DATA_FILE) -> None:
    """Write atomically so a crash mid-write can't corrupt the file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ── Parsing ──────────────────────────────────────────────────────────────────
class MappingError(ValueError):
    """The admin's emoji=role list has a problem they can fix."""


def parse_mappings(text: str) -> list[tuple[str, str]]:
    """'🎮=Gamer, 🎵=Music' -> [('🎮', 'Gamer'), ('🎵', 'Music')]"""
    pairs = []
    for part in _PAIR_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        pieces = _ARROW.split(part, maxsplit=1)
        if len(pieces) != 2 or not pieces[0].strip() or not pieces[1].strip():
            raise MappingError(f"I couldn't read `{part}`. Use the form `emoji=Role name`, separated by commas.")
        pairs.append((pieces[0].strip(), pieces[1].strip()))
    if not pairs:
        raise MappingError("Give me at least one `emoji=Role name` pair.")
    if len(pairs) > MAX_ROLES_PER_MESSAGE:
        raise MappingError(f"That's too many; a message can hold {MAX_ROLES_PER_MESSAGE} reactions.")
    return pairs


def parse_emoji(token: str) -> discord.PartialEmoji:
    emoji = discord.PartialEmoji.from_str(token)
    # A bare ASCII word like "gamer" parses as an emoji name; real unicode emoji are non-ASCII.
    if not emoji.id and token.isascii():
        raise MappingError(f"`{token}` isn't an emoji.")
    return emoji


def find_role(guild: discord.Guild, ref: str) -> Optional[discord.Role]:
    match = _ROLE_MENTION.match(ref)
    if match:
        return guild.get_role(int(match.group(1)))
    wanted = ref.lstrip("@").strip().lower()
    return next((r for r in guild.roles if r.name.lower() == wanted), None)


def check_assignable(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Why ``role`` can't be given out by reaction, or ``None`` if it can."""
    if role.is_default():
        return "@everyone can't be assigned."
    if role.managed:
        return f"**{role.name}** is managed by an integration."
    if role >= guild.me.top_role:
        return f"**{role.name}** is above my highest role, so I can't assign it. Move my role higher."
    if role.permissions.value & DANGEROUS_PERMISSIONS.value:
        return f"**{role.name}** has powerful permissions (admin/management/kick/ban), so it can't be self-assigned."
    return None


class ReactionRoles(commands.Cog):
    """Cog for managing reaction roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # message_id -> {"guild_id", "channel_id", "roles": {emoji: role_id}}
        self.reaction_roles = load_data()

    # ── /reaction-role ─────────────────────────────────────────────────────
    @app_commands.command(name="reaction-role", description="Post a message where members react to get roles")
    @app_commands.describe(
        channel="Where to post it",
        roles="Pairs like: 🎮=Gamer, 🎵=Music (one role per emoji)",
        title="Heading for the message",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def reaction_role(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        roles: str,
        title: app_commands.Range[str, 1, 200] = "🎭 Get Your Roles!",
    ) -> None:
        guild = interaction.guild

        # Validate everything before posting anything.
        try:
            mapping, problems = self._resolve_mapping(guild, roles)
        except MappingError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if problems:
            await interaction.response.send_message("❌ " + "\n".join(problems), ephemeral=True)
            return

        needed = channel.permissions_for(guild.me)
        if not (needed.view_channel and needed.send_messages and needed.embed_links
                and needed.add_reactions and needed.read_message_history):
            await interaction.response.send_message(
                f"❌ In {channel.mention} I need **View Channel, Send Messages, Embed Links, "
                "Add Reactions** and **Read Message History**.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title=title,
            description="React below to get or remove a role:\n\n"
                        + "\n".join(f"{emoji} → **{role.name}**" for emoji, role in mapping),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Click a reaction to get the role, click again to remove it")
        message = await channel.send(embed=embed)

        stored: dict[str, int] = {}
        failed: list[str] = []
        for emoji, role in mapping:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                failed.append(str(emoji))
                continue
            stored[str(emoji)] = role.id

        if not stored:
            await interaction.followup.send(
                f"❌ I posted the message in {channel.mention} but couldn't add any of those emoji, "
                "so it won't hand out roles. Delete it and check the emoji.", ephemeral=True)
            return

        self.reaction_roles[str(message.id)] = {
            "guild_id": guild.id, "channel_id": channel.id, "roles": stored,
        }
        save_data(self.reaction_roles)

        text = f"✅ Reaction role message posted in {channel.mention}!"
        if failed:
            text += f"\n⚠️ I couldn't use these emoji, so they won't work: {' '.join(failed)}"
        await interaction.followup.send(text, ephemeral=True)

    def _resolve_mapping(self, guild: discord.Guild, text: str) -> tuple[list[tuple[discord.PartialEmoji, discord.Role]], list[str]]:
        """(valid pairs, problems to show the admin). Raises MappingError for syntax errors."""
        mapping: list[tuple[discord.PartialEmoji, discord.Role]] = []
        problems: list[str] = []
        seen_emoji: set[str] = set()
        seen_roles: set[int] = set()
        for emoji_token, role_ref in parse_mappings(text):
            emoji = parse_emoji(emoji_token)
            role = find_role(guild, role_ref)
            if not role:
                problems.append(f"I can't find a role called **{role_ref}**. Create it first (try `/setup-roles` or `/customize`).")
                continue
            if (reason := check_assignable(guild, role)):
                problems.append(reason)
                continue
            if str(emoji) in seen_emoji:
                problems.append(f"{emoji} is used twice.")
                continue
            if role.id in seen_roles:
                problems.append(f"**{role.name}** has more than one emoji; use one emoji per role.")
                continue
            seen_emoji.add(str(emoji))
            seen_roles.add(role.id)
            mapping.append((emoji, role))
        return mapping, problems

    # ── Reaction listeners ─────────────────────────────────────────────────
    def _role_for(self, payload: discord.RawReactionActionEvent) -> Optional[tuple[discord.Guild, discord.Role]]:
        entry = self.reaction_roles.get(str(payload.message_id))
        if not entry or payload.guild_id is None or payload.user_id == self.bot.user.id:
            return None
        role_id = entry["roles"].get(str(payload.emoji))
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id) if (guild and role_id) else None
        if role is None or check_assignable(guild, role) is not None:
            return None
        return guild, role

    async def _member_for(self, guild: discord.Guild, payload: discord.RawReactionActionEvent) -> Optional[discord.Member]:
        member = getattr(payload, "member", None) or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return None
        return None if member.bot else member

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        target = self._role_for(payload)
        if not target:
            return
        guild, role = target
        try:
            member = await self._member_for(guild, payload)
            if member:
                await member.add_roles(role, reason="Reaction role")
        except discord.HTTPException:
            log.exception("Couldn't give role %s to %s", role.id, payload.user_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        target = self._role_for(payload)
        if not target:
            return
        guild, role = target
        try:
            member = await self._member_for(guild, payload)
            if member:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.HTTPException:
            log.exception("Couldn't remove role %s from %s", role.id, payload.user_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self.reaction_roles.pop(str(payload.message_id), None) is not None:
            save_data(self.reaction_roles)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        stale = [m for m, entry in self.reaction_roles.items() if entry.get("channel_id") == channel.id]
        for message_id in stale:
            del self.reaction_roles[message_id]
        if stale:
            save_data(self.reaction_roles)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await respond_to_error(interaction, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))
