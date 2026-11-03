"""Applies a ``ServerPlan`` to a guild.

Create-only: existing roles, categories and channels are matched by name and
left exactly as they are. Nothing is renamed, moved, edited or deleted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

import discord

from .plan import ChannelSpec, ServerPlan, TEXT_LIKE

log = logging.getLogger("luna.applier")

ProgressFn = Callable[[str], Awaitable[None]]

_VOICE_TYPES = (discord.VoiceChannel, discord.StageChannel)
_TEXT_TYPES = (discord.TextChannel, discord.ForumChannel)
_SNAPSHOT_ROLE_LIMIT = 100
_SNAPSHOT_CHANNEL_LIMIT = 150


# ── Report ───────────────────────────────────────────────────────────────────
@dataclass
class ApplyReport:
    """What happened (or, for a dry run, what would happen)."""
    dry_run: bool = False
    roles_created: list[str] = field(default_factory=list)
    roles_existing: list[str] = field(default_factory=list)
    categories_created: list[str] = field(default_factory=list)
    categories_existing: list[str] = field(default_factory=list)
    channels_created: list[str] = field(default_factory=list)    # "Category / name"
    channels_existing: list[str] = field(default_factory=list)   # "Category / name"
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    aborted: Optional[str] = None

    @property
    def nothing_to_do(self) -> bool:
        return not (self.roles_created or self.categories_created or self.channels_created)


# ── Reading the server ───────────────────────────────────────────────────────
def snapshot_guild(guild: discord.Guild) -> dict:
    """A compact description of the server, given to the model as context."""
    roles = [r.name for r in reversed(guild.roles) if not r.is_default() and not r.managed]
    categories = [
        {"name": c.name, "channels": [ch.name for ch in c.channels]}
        for c in guild.categories
    ]
    loose = [ch.name for ch in guild.channels
             if not isinstance(ch, discord.CategoryChannel) and ch.category is None]
    snap = {
        "roles_most_senior_first": roles[:_SNAPSHOT_ROLE_LIMIT],
        "categories": categories,
        "channels_without_category": loose,
    }
    # Keep the prompt bounded on very large servers.
    budget = _SNAPSHOT_CHANNEL_LIMIT
    for cat in snap["categories"]:
        cat["channels"] = cat["channels"][:budget]
        budget = max(0, budget - len(cat["channels"]))
    snap["channels_without_category"] = snap["channels_without_category"][:budget]
    return snap


class _RoleResolver:
    """Looks roles up by (case-insensitive) name, including ones created this run."""

    def __init__(self, guild: discord.Guild):
        self._roles = {r.name.lower(): r for r in guild.roles if not r.is_default()}
        self._planned: set[str] = set()  # dry run: roles that would be created

    def get(self, name: str) -> Optional[discord.Role]:
        return self._roles.get(name.lower())

    def add(self, role: discord.Role) -> None:
        self._roles[role.name.lower()] = role

    def plan(self, name: str) -> None:
        self._planned.add(name.lower())

    def resolve(self, names: Iterable[str]) -> tuple[list[discord.Role], list[str]]:
        """(roles found, names that don't exist). Roles a dry run would create count as neither."""
        found, missing = [], []
        for name in names:
            role = self.get(name)
            if role:
                found.append(role)
            elif name.lower() not in self._planned:
                missing.append(name)
        return found, missing


def build_permissions(names: Iterable[str]) -> discord.Permissions:
    return discord.Permissions(**{name: True for name in names})


def build_overwrites(
    guild: discord.Guild,
    *,
    private_roles: Optional[list],
    read_only: bool,
    writers: list,
    kind: str,
) -> dict:
    """Permission overwrites for one category/channel.

    ``private_roles`` is ``None`` for "visible to everyone", otherwise the roles
    that may see it (an empty list hides it from everyone but admins).
    The bot always keeps access so it can post in the channels it creates.
    """
    overwrites: dict = {}
    everyone = guild.default_role

    if private_roles is not None:
        overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
        for role in private_roles:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True)

    if read_only and kind != "voice":
        deny = overwrites.setdefault(everyone, discord.PermissionOverwrite())
        deny.update(
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
        )
        for role in writers:
            overwrites.setdefault(role, discord.PermissionOverwrite()).update(send_messages=True)

    if overwrites:
        me = overwrites.setdefault(guild.me, discord.PermissionOverwrite())
        me.update(view_channel=True, send_messages=True)
    return overwrites


# ── Applying ─────────────────────────────────────────────────────────────────
async def apply_plan(
    guild: discord.Guild,
    plan: ServerPlan,
    *,
    dry_run: bool = False,
    progress: Optional[ProgressFn] = None,
    reason: str = "Luna.AI /customize",
) -> ApplyReport:
    """Build ``plan`` in ``guild``. With ``dry_run`` nothing is created."""
    report = ApplyReport(dry_run=dry_run)
    try:
        resolver = _RoleResolver(guild)
        await _apply_roles(guild, plan, resolver, report, dry_run, progress, reason)
        await _apply_structure(guild, plan, resolver, report, dry_run, progress, reason)
    except discord.Forbidden:
        report.aborted = (
            "I'm missing a permission. Make sure my role has **Manage Roles** and "
            "**Manage Channels** (or Administrator) and sits above any roles I need to manage."
        )
    return report


async def _say(progress: Optional[ProgressFn], text: str) -> None:
    if progress:
        await progress(text)


async def _apply_roles(guild, plan, resolver, report, dry_run, progress, reason) -> None:
    created: list[discord.Role] = []
    for spec in plan.roles:
        if resolver.get(spec.name):
            report.roles_existing.append(spec.name)
            continue
        if dry_run:
            resolver.plan(spec.name)
            report.roles_created.append(spec.name)
            continue
        await _say(progress, f"Creating role **{spec.name}**…")
        try:
            role = await guild.create_role(
                name=spec.name,
                colour=discord.Colour(spec.color),
                hoist=spec.hoist,
                mentionable=spec.mentionable,
                permissions=build_permissions(spec.permissions),
                reason=reason,
            )
        except discord.Forbidden:
            raise
        except discord.HTTPException as exc:
            report.errors.append(f"Role **{spec.name}**: {exc.text or exc}")
            continue
        resolver.add(role)
        created.append(role)
        report.roles_created.append(spec.name)

    if len(created) > 1:
        await _order_roles(guild, created, report, reason)


async def _order_roles(guild, created: list, report: ApplyReport, reason: str) -> None:
    """Make the new roles sit in the plan's order (most senior first).

    Only the new roles are moved, and only among the slots they already occupy,
    so existing roles keep their relative positions.
    """
    try:
        fresh = {r.id: r.position for r in await guild.fetch_roles()}
        slots = sorted((fresh[r.id] for r in created), reverse=True)
        desired = dict(zip(created, slots))
        if any(fresh[r.id] != pos for r, pos in desired.items()):
            await guild.edit_role_positions(desired, reason=reason)
    except discord.Forbidden:
        raise
    except (discord.HTTPException, KeyError) as exc:
        log.warning("Could not order new roles: %s", exc)
        report.notes.append("I couldn't put the new roles in order; drag them into place in Server Settings → Roles.")


async def _apply_structure(guild, plan, resolver, report, dry_run, progress, reason) -> None:
    categories = {c.name.lower(): c for c in guild.categories}
    text_index: dict[str, discord.abc.GuildChannel] = {}
    voice_index: dict[str, discord.abc.GuildChannel] = {}
    for ch in guild.channels:
        if isinstance(ch, _VOICE_TYPES):
            voice_index[ch.name.lower()] = ch
        elif isinstance(ch, _TEXT_TYPES):
            text_index[ch.name.lower()] = ch

    for cat_spec in plan.categories:
        cat_roles, cat_missing = resolver.resolve(cat_spec.private_to)
        _note_missing_roles(report, cat_missing, f"category '{cat_spec.name}'")

        category = categories.get(cat_spec.name.lower())
        if category:
            report.categories_existing.append(cat_spec.name)
        elif dry_run:
            report.categories_created.append(cat_spec.name)
        else:
            await _say(progress, f"Creating category **{cat_spec.name}**…")
            overwrites = build_overwrites(
                guild,
                private_roles=cat_roles if cat_spec.private_to else None,
                read_only=False, writers=[], kind="text",
            )
            try:
                category = await guild.create_category(
                    cat_spec.name, reason=reason, **({"overwrites": overwrites} if overwrites else {})
                )
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                report.errors.append(f"Category **{cat_spec.name}**: {exc.text or exc}")
                continue  # its channels would end up uncategorised; skip them
            categories[cat_spec.name.lower()] = category
            report.categories_created.append(cat_spec.name)

        for ch_spec in cat_spec.channels:
            label = f"{cat_spec.name} / {ch_spec.name}"
            index = text_index if ch_spec.kind in TEXT_LIKE else voice_index
            existing = index.get(ch_spec.name.lower())
            if existing:
                where = existing.category.name if getattr(existing, "category", None) else "no category"
                report.channels_existing.append(label)
                if existing.category is None or existing.category.name.lower() != cat_spec.name.lower():
                    report.notes.append(f"**{ch_spec.name}** already exists ({where}), so I left it there.")
                continue
            if dry_run:
                report.channels_created.append(label)
                continue

            await _say(progress, f"Creating **{ch_spec.name}**…")
            channel_names = ch_spec.private_to or cat_spec.private_to
            roles, missing = resolver.resolve(channel_names)
            if ch_spec.private_to:
                _note_missing_roles(report, missing, f"channel '{ch_spec.name}'")
            writers, writers_missing = resolver.resolve(ch_spec.writers)
            _note_missing_roles(report, writers_missing, f"channel '{ch_spec.name}' (writers)")
            overwrites = build_overwrites(
                guild,
                private_roles=roles if channel_names else None,
                read_only=ch_spec.read_only,
                writers=writers,
                kind=ch_spec.kind,
            )
            try:
                channel = await _create_channel(guild, ch_spec, category, overwrites, reason, report)
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                report.errors.append(f"Channel **{ch_spec.name}**: {exc.text or exc}")
                continue
            index[ch_spec.name.lower()] = channel
            report.channels_created.append(label)


def _note_missing_roles(report: ApplyReport, missing: list[str], where: str) -> None:
    for name in missing:
        note = f"Role **{name}** (used by {where}) doesn't exist, so it was ignored."
        if note not in report.notes:
            report.notes.append(note)


async def _create_channel(guild, spec: ChannelSpec, category, overwrites, reason, report):
    kwargs: dict = {"reason": reason}
    if category is not None:
        kwargs["category"] = category
    if overwrites:
        kwargs["overwrites"] = overwrites
    topic = {"topic": spec.topic} if spec.topic else {}

    if spec.kind == "voice":
        return await guild.create_voice_channel(spec.name, **kwargs)
    if spec.kind == "forum":
        try:
            return await guild.create_forum(spec.name, **topic, **kwargs)
        except discord.Forbidden:
            raise
        except discord.HTTPException as exc:
            if exc.status != 400:
                raise
            report.notes.append(f"**{spec.name}** was made a text channel (forums need Community enabled).")
    elif spec.kind == "announcement":
        try:
            return await guild.create_text_channel(spec.name, news=True, **topic, **kwargs)
        except discord.Forbidden:
            raise
        except discord.HTTPException as exc:
            if exc.status != 400:
                raise
            report.notes.append(f"**{spec.name}** was made a text channel (announcement channels need Community enabled).")
    return await guild.create_text_channel(spec.name, **topic, **kwargs)


# ── Presentation ─────────────────────────────────────────────────────────────
_ICONS = {"text": "#", "announcement": "📣", "forum": "💬", "voice": "🔊"}


def plan_lines(plan: ServerPlan, preview: ApplyReport) -> tuple[list[str], list[str]]:
    """(role_lines, structure_lines) for showing the plan, marking what already exists."""
    have_roles = {n.lower() for n in preview.roles_existing}
    have_cats = {n.lower() for n in preview.categories_existing}
    have_channels = {n.lower() for n in preview.channels_existing}

    role_lines = [
        f"• **{r.name}**" + (" — exists, skipped" if r.name.lower() in have_roles else "")
        for r in plan.roles
    ]

    structure: list[str] = []
    for cat in plan.categories:
        lock = " 🔒" if cat.private_to else ""
        tag = " — exists" if cat.name.lower() in have_cats else ""
        structure.append(f"**{cat.name}**{lock}{tag}")
        for ch in cat.channels:
            mark = " 🔒" if ch.private_to else ""
            ro = " (read-only)" if ch.read_only else ""
            skip = " — exists, skipped" if f"{cat.name} / {ch.name}".lower() in have_channels else ""
            structure.append(f"　{_ICONS.get(ch.kind, '#')} {ch.name}{mark}{ro}{skip}")
    return role_lines, structure


def chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    """Join lines into chunks that each fit an embed field value."""
    chunks, current = [], ""
    for line in lines:
        line = line[: limit - 1]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def result_lines(report: ApplyReport) -> list[str]:
    """Human summary of a finished (or dry) run."""
    verb = "Would create" if report.dry_run else "Created"
    lines = [
        f"{verb} **{len(report.roles_created)}** role(s), "
        f"**{len(report.categories_created)}** categor{'y' if len(report.categories_created) == 1 else 'ies'}, "
        f"**{len(report.channels_created)}** channel(s)."
    ]
    skipped = len(report.roles_existing) + len(report.categories_existing) + len(report.channels_existing)
    if skipped:
        lines.append(f"Left **{skipped}** existing item(s) untouched.")
    return lines
