"""Discord embeds shared by the commands that apply plans."""
from __future__ import annotations

import discord

from .applier import ApplyReport, chunk_lines, plan_lines, result_lines
from .plan import ServerPlan

BRAND = discord.Color.from_rgb(139, 92, 246)
REVIEW_TIMEOUT = 300       # seconds the admin has to press Build
MAX_STRUCTURE_CHUNKS = 3   # keeps the preview embed under Discord's 6000-char limit


def build_preview_embed(plan: ServerPlan, preview: ApplyReport) -> discord.Embed:
    role_lines, structure_lines = plan_lines(plan, preview)
    embed = discord.Embed(title="🌙 Here's my plan", color=BRAND)

    counts = (
        f"**{len(plan.roles)}** roles · **{len(plan.categories)}** categories · "
        f"**{plan.channel_count}** channels"
    )
    summary = f"{plan.summary}\n\n" if plan.summary else ""
    embed.description = (
        f"{summary}{counts}\n"
        "I only **create** things. Nothing existing is renamed, moved or deleted."
    )

    if role_lines:
        chunks = chunk_lines(role_lines)
        embed.add_field(name="Roles (most senior first)", value=chunks[0], inline=False)
        if len(chunks) > 1:
            embed.add_field(name="​", value="…plus more roles not shown here. Everything in the plan will be built.", inline=False)

    chunks = chunk_lines(structure_lines)
    for i, chunk in enumerate(chunks[:MAX_STRUCTURE_CHUNKS]):
        embed.add_field(name="Channels" if i == 0 else "​", value=chunk, inline=False)
    if len(chunks) > MAX_STRUCTURE_CHUNKS:
        embed.add_field(name="​", value="…plus more channels not shown here. Everything in the plan will be built.", inline=False)

    heads_up = plan.warnings + preview.notes
    if heads_up:
        text = "\n".join(f"• {line}" for line in heads_up)
        embed.add_field(name="Heads-up", value=text[:1000], inline=False)

    embed.set_footer(text=f"Review it, then press Build. Expires in {REVIEW_TIMEOUT // 60} minutes.")
    return embed


def build_result_embed(report: ApplyReport) -> discord.Embed:
    if report.aborted:
        title, color = "⚠️ I had to stop", discord.Color.orange()
    elif report.errors:
        title, color = "⚠️ Finished, with some problems", discord.Color.orange()
    else:
        title, color = "✅ Your server is ready", discord.Color.green()

    embed = discord.Embed(title=title, color=color, description="\n".join(result_lines(report)))
    if report.aborted:
        embed.add_field(name="What happened", value=report.aborted[:1000], inline=False)
    if report.errors:
        embed.add_field(name="Couldn't create", value="\n".join(f"• {e}" for e in report.errors[:8])[:1000], inline=False)
    if report.notes:
        embed.add_field(name="Notes", value="\n".join(f"• {n}" for n in report.notes[:8])[:1000], inline=False)
    return embed
