import discord
from discord.ext import commands
from discord import app_commands
import json
import os

REACTION_ROLES_FILE = "reaction_roles.json"

def load_data():
    if os.path.exists(REACTION_ROLES_FILE):
        with open(REACTION_ROLES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(REACTION_ROLES_FILE, "w") as f:
        json.dump(data, f, indent=2)


class ReactionRoles(commands.Cog):
    """Cog for managing reaction roles."""

    def __init__(self, bot):
        self.bot = bot
        self.reaction_roles = load_data()

    # ── /reaction-role setup ───────────────────────────────────────────────
    @app_commands.command(name="reaction-role", description="Post a reaction role message in a channel")
    @app_commands.default_permissions(administrator=True)
    async def reaction_role(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Posts a reaction role embed in the specified channel."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # ── Define your reaction roles here ──────────────────────────────
        reaction_role_map = {
            "🎮": "✅ Member",       # emoji -> role name
            "🎵": "🎖️ VIP",
            "📢": "✅ Member",
        }

        embed = discord.Embed(
            title="🎭 Get Your Roles!",
            description="React below to assign yourself a role:\n\n" +
                        "\n".join(f"{emoji} → **{role}**" for emoji, role in reaction_role_map.items()),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Click a reaction to get/remove a role")

        msg = await channel.send(embed=embed)

        # Add reactions to the message
        for emoji in reaction_role_map.keys():
            await msg.add_reaction(emoji)

        # Save mapping: message_id -> {emoji: role_name}
        self.reaction_roles[str(msg.id)] = {
            "channel_id": channel.id,
            "roles": reaction_role_map
        }
        save_data(self.reaction_roles)

        await interaction.followup.send(f"✅ Reaction role message posted in {channel.mention}!", ephemeral=True)

    # ── on_raw_reaction_add ────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        msg_id = str(payload.message_id)
        if msg_id not in self.reaction_roles:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        emoji = str(payload.emoji)
        role_name = self.reaction_roles[msg_id]["roles"].get(emoji)

        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role and role < guild.me.top_role:  # Only assign roles below bot's top role (safe)
                await member.add_roles(role)

    # ── on_raw_reaction_remove ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        msg_id = str(payload.message_id)
        if msg_id not in self.reaction_roles:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        emoji = str(payload.emoji)
        role_name = self.reaction_roles[msg_id]["roles"].get(emoji)

        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role and role < guild.me.top_role:  # Only remove roles below bot's top role (safe)
                await member.remove_roles(role)


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
