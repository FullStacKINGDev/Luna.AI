"""Discord embeds shared by the commands that apply plans."""
from __future__ import annotations

import discord

from .applier import ApplyReport, ServerIndex, change_lines, chunk_lines, plan_lines, result_lines
from .plan import ServerPlan

BRAND = discord.Color.from_rgb(139, 92, 246)
DANGER = discord.Color.red()
REVIEW_TIMEOUT = 300       # seconds the admin has to press the button
MAX_STRUCTURE_CHUNKS = 3
EMBED_BUDGET = 5800        # Discord rejects an embed over 6000 characters in total
_MORE_ROLES = "…plus more roles not shown here. Everything in the plan will be built."
_MORE_CHANNELS = "…plus more channels not shown here. Everything in the plan will be built."
_SEE_FILE = "…and more, see the attached plan"


def _limited(lines: list[str]) -> str:
    """Lines that fit one embed field, with a pointer to the attachment if some don't."""
    chunks = chunk_lines(lines, limit=960)
    return chunks[0] + (f"\n{_SEE_FILE}" if len(chunks) > 1 else "")


def build_preview_embed(plan: ServerPlan, preview: ApplyReport, index: ServerIndex | None = None) -> discord.Embed:
    index = index or ServerIndex()
    role_lines, structure_lines = plan_lines(plan, preview)
    edit_lines, delete_lines = change_lines(plan, index)
    risks = plan.risk_reasons()

    embed = discord.Embed(title="⚠️ Review carefully" if risks else "🌙 Here's my plan",
                          color=DANGER if risks else BRAND)

    counts = [f"**{len(plan.roles)}** new roles", f"**{len(plan.categories)}** new categories",
              f"**{plan.channel_count}** new channels"]
    if plan.edit_count:
        counts.append(f"**{plan.edit_count}** changes to existing items")
    if plan.deletions:
        counts.append(f"**{len(plan.deletions)}** deletions")
    summary = f"{plan.summary}\n\n" if plan.summary else ""
    stance = ("This plan **changes existing items**. Review every line before you confirm."
              if plan.has_changes_to_existing
              else "I only **create** new things here. Nothing existing is touched.")
    embed.description = f"{summary}{' · '.join(counts)}\n{stance}"
    footer = ("You'll be asked to type CONFIRM because of: " + "; ".join(risks) + "."
              if risks else f"Review it, then press the button. Expires in {REVIEW_TIMEOUT // 60} minutes.")
    embed.set_footer(text=footer[:2000])

    if role_lines:
        chunks = chunk_lines(role_lines)
        embed.add_field(name="New roles (most senior first)", value=chunks[0], inline=False)
        if len(chunks) > 1:
            embed.add_field(name="​", value=_MORE_ROLES, inline=False)

    # Fields that come after the channel list. Their size is reserved up front so the
    # channel list, the longest part, is what gets trimmed when space runs out.
    tail: list[tuple[str, str]] = []
    if edit_lines:
        tail.append(("✏️ Changes to existing items", _limited(edit_lines)))
    if delete_lines:
        tail.append(("🗑️ Will be permanently deleted", _limited(delete_lines)))
    if plan.notes:
        tail.append(("📝 A note from me", plan.notes[:1000]))
    heads_up = plan.warnings + preview.notes
    if heads_up:
        tail.append(("Heads-up", "\n".join(f"• {line}" for line in heads_up)[:1000]))
    reserved = sum(len(name) + len(value) for name, value in tail) + len(_MORE_CHANNELS) + 1

    chunks = chunk_lines(structure_lines)
    shown = 0
    for i, chunk in enumerate(chunks[:MAX_STRUCTURE_CHUNKS]):
        name = "New channels" if i == 0 else "​"
        if len(embed) + len(name) + len(chunk) + reserved > EMBED_BUDGET:
            break
        embed.add_field(name=name, value=chunk, inline=False)
        shown += 1
    if shown < len(chunks):
        embed.add_field(name="​", value=_MORE_CHANNELS, inline=False)

    for name, value in tail:
        embed.add_field(name=name, value=value, inline=False)
    return embed


def build_result_embed(report: ApplyReport) -> discord.Embed:
    if report.aborted:
        title, color = "⚠️ I had to stop", discord.Color.orange()
    elif report.errors:
        title, color = "⚠️ Finished, with some problems", discord.Color.orange()
    else:
        title, color = "✅ Done", discord.Color.green()

    embed = discord.Embed(title=title, color=color, description="\n".join(result_lines(report)))
    if report.aborted:
        embed.add_field(name="What happened", value=report.aborted[:1000], inline=False)
    if report.deleted:
        embed.add_field(name="Deleted", value="\n".join(f"• {d}" for d in report.deleted)[:1000], inline=False)
    if report.errors:
        embed.add_field(name="Couldn't do", value="\n".join(f"• {e}" for e in report.errors[:8])[:1000], inline=False)
    if report.notes:
        embed.add_field(name="Notes", value="\n".join(f"• {n}" for n in report.notes[:8])[:1000], inline=False)
    return embed
