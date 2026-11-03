"""Applies a ``ServerPlan`` to a guild: create, edit, reorder and delete.

Safety model
------------
* Existing items are only touched through refs from ``snapshot_guild``. Anything
  not in the snapshot (locked roles, protected channels) cannot be referenced.
* Locked items are re-checked at apply time: roles at or above the bot's top
  role, managed (integration) roles, @everyone, and the channels Discord's
  Community features depend on (rules, updates, system messages, safety alerts,
  AFK) are never edited or deleted.
* A structure backup is written to disk before any edit or deletion.
* Deletions run last, children before parents, so a failure earlier in the run
  never leaves a half-deleted structure behind.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

import discord

from .plan import ChannelEdit, ChannelSpec, ServerPlan, TEXT_LIKE

log = logging.getLogger("luna.applier")

ProgressFn = Callable[[str], Awaitable[None]]

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"

_VOICE_TYPES = (discord.VoiceChannel, discord.StageChannel)
_TEXT_TYPES = (discord.TextChannel, discord.ForumChannel)
_SNAPSHOT_ROLE_LIMIT = 100
_SNAPSHOT_CHANNEL_LIMIT = 300
_TOPIC_PREVIEW = 80


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
    edited: list[str] = field(default_factory=list)               # one entry per existing item changed
    reordered: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    backup_path: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    aborted: Optional[str] = None

    @property
    def nothing_to_do(self) -> bool:
        return not (self.roles_created or self.categories_created or self.channels_created
                    or self.edited or self.reordered or self.deleted)


# ── Reading the server ───────────────────────────────────────────────────────
@dataclass
class ServerIndex:
    """Maps the refs the model sees to real Discord objects (kept local)."""
    ids: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)   # "role" | "category" | channel kind
    names: dict[str, str] = field(default_factory=dict)

    def add(self, ref: str, discord_id: int, kind: str, name: str) -> None:
        self.ids[ref], self.kinds[ref], self.names[ref] = discord_id, kind, name


def protected_channel_ids(guild: discord.Guild) -> set[int]:
    """Channels Discord's Community features depend on."""
    special = (guild.rules_channel, guild.public_updates_channel, guild.system_channel,
               guild.safety_alerts_channel, guild.afk_channel)
    return {c.id for c in special if c}


def role_lock_reason(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    if role.is_default():
        return "@everyone can't be changed"
    if role.managed:
        return "it's managed by an integration"
    if guild.owner_id != guild.me.id and not role < guild.me.top_role:   # the owner ignores the hierarchy
        return "it's at or above my highest role"
    return None


def channel_lock_reason(guild: discord.Guild, channel) -> Optional[str]:
    if channel.id in protected_channel_ids(guild):
        return "Discord's Community features depend on it"
    return None


def channel_kind(channel) -> Optional[str]:
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, _VOICE_TYPES):
        return "voice"
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    if isinstance(channel, discord.TextChannel):
        return "announcement" if channel.is_news() else "text"
    return None   # media channels etc.: not supported, treated as locked


def snapshot_guild(guild: discord.Guild) -> tuple[dict, ServerIndex]:
    """Describe the server for the model, and remember what each ref points at.

    Locked items are listed by name only, with no ref, so the model knows they
    exist but cannot edit or delete them.
    """
    index = ServerIndex()
    counters = {"role": 0, "cat": 0, "ch": 0}

    def new_ref(prefix: str, kind: str, obj_id: int, name: str) -> str:
        counters[prefix] += 1
        ref = f"{prefix}:{counters[prefix]}"
        index.add(ref, obj_id, kind, name)
        return ref

    roles, locked_roles = [], []
    for role in reversed(guild.roles):
        if role.is_default():
            continue
        if role_lock_reason(guild, role):
            locked_roles.append(role.name)
        elif len(roles) < _SNAPSHOT_ROLE_LIMIT:
            roles.append({
                "ref": new_ref("role", "role", role.id, role.name),
                "name": role.name,
                "color": f"#{role.colour.value:06x}",
                "hoist": role.hoist,
            })

    protected = protected_channel_ids(guild)
    locked_channels: list[str] = []
    budget = _SNAPSHOT_CHANNEL_LIMIT

    def channel_view(ch) -> Optional[dict]:
        nonlocal budget
        kind = channel_kind(ch)
        if kind is None or ch.id in protected:
            locked_channels.append(ch.name)
            return None
        if budget <= 0:
            return None
        budget -= 1
        view = {
            "ref": new_ref("ch", kind, ch.id, ch.name),
            "name": ch.name,
            "type": kind,
            "private": ch.overwrites_for(guild.default_role).view_channel is False,
        }
        topic = getattr(ch, "topic", None)
        if topic:
            view["topic"] = topic[:_TOPIC_PREVIEW]
        return view

    categories = []
    for cat in guild.categories:
        children = [v for v in (channel_view(ch) for ch in cat.channels) if v]
        categories.append({"ref": new_ref("cat", "category", cat.id, cat.name), "name": cat.name, "channels": children})
    loose = [v for v in (channel_view(ch) for ch in guild.channels
                         if not isinstance(ch, discord.CategoryChannel) and ch.category is None) if v]

    snapshot = {
        "roles_most_senior_first": roles,
        "locked_roles_cannot_be_changed": locked_roles,
        "categories": categories,
        "channels_without_category": loose,
        "locked_channels_cannot_be_changed": locked_channels,
    }
    return snapshot, index


def write_backup(guild: discord.Guild, directory: Path = BACKUP_DIR) -> Path:
    """Save the server's roles, categories, channels and permission overwrites as JSON.

    Discord can't undo a deletion, so this is the record to rebuild from. It holds
    structure only (no messages) and no secrets.
    """
    def overwrites(ch) -> list[dict]:
        out = []
        for target, ow in ch.overwrites.items():
            allow, deny = ow.pair()
            out.append({"target_id": target.id, "target_is_role": isinstance(target, discord.Role),
                        "allow": allow.value, "deny": deny.value})
        return out

    data = {
        "guild": {"id": guild.id, "name": guild.name},
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "roles": [{"id": r.id, "name": r.name, "position": r.position, "colour": r.colour.value,
                   "hoist": r.hoist, "mentionable": r.mentionable, "managed": r.managed,
                   "permissions": r.permissions.value} for r in guild.roles],
        "channels": [{"id": c.id, "name": c.name, "type": str(c.type), "position": c.position,
                      "category_id": c.category_id, "topic": getattr(c, "topic", None),
                      "overwrites": overwrites(c)} for c in guild.channels],
    }
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = directory / f"guild-{guild.id}-{stamp}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


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
    """Permission overwrites for one NEW category/channel.

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
    index: Optional[ServerIndex] = None,
    dry_run: bool = False,
    progress: Optional[ProgressFn] = None,
    reason: str = "Luna.AI /customize",
    backup_dir: Path = BACKUP_DIR,
) -> ApplyReport:
    """Carry out ``plan`` in ``guild``. With ``dry_run`` nothing is changed.

    ``index`` resolves the refs used by edits and deletions; without it those
    parts of the plan are skipped.
    """
    report = ApplyReport(dry_run=dry_run)
    index = index or ServerIndex()
    try:
        if plan.has_changes_to_existing and not dry_run:
            try:
                report.backup_path = str(write_backup(guild, backup_dir))
            except OSError as exc:
                log.exception("Backup failed")
                report.aborted = f"I couldn't save a backup first ({exc}), so I changed nothing."
                return report

        resolver = _RoleResolver(guild)
        await _apply_roles(guild, plan, resolver, report, dry_run, progress, reason)
        categories = await _apply_structure(guild, plan, resolver, report, dry_run, progress, reason)
        await _apply_edits(guild, plan, index, resolver, categories, report, dry_run, progress, reason)
        await _apply_order(guild, plan, index, report, dry_run, progress, reason)
        await _apply_deletions(guild, plan, index, report, dry_run, progress, reason)
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


async def _order_roles(guild, ordered: list, report: ApplyReport, reason: str) -> None:
    """Put ``ordered`` roles in that order (most senior first).

    Only these roles move, and only among the slots they already occupy, so every
    other role keeps its position and nothing can end up above the bot.
    """
    try:
        fresh = {r.id: r.position for r in await guild.fetch_roles()}
        slots = sorted((fresh[r.id] for r in ordered), reverse=True)
        desired = dict(zip(ordered, slots))
        if any(fresh[r.id] != pos for r, pos in desired.items()):
            await guild.edit_role_positions(desired, reason=reason)
    except discord.Forbidden:
        raise
    except (discord.HTTPException, KeyError) as exc:
        log.warning("Could not order roles: %s", exc)
        report.notes.append("I couldn't put the roles in order; drag them into place in Server Settings → Roles.")


async def _apply_structure(guild, plan, resolver, report, dry_run, progress, reason) -> dict:
    """Create the plan's categories and channels. Returns {name.lower(): category}."""
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
            known = text_index if ch_spec.kind in TEXT_LIKE else voice_index
            existing = known.get(ch_spec.name.lower())
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
            known[ch_spec.name.lower()] = channel
            report.channels_created.append(label)
    return categories


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


# ── Editing existing items ───────────────────────────────────────────────────
def _lookup(guild: discord.Guild, index: ServerIndex, ref: str):
    discord_id = index.ids.get(ref)
    if discord_id is None:
        return None
    return guild.get_role(discord_id) if index.kinds.get(ref) == "role" else guild.get_channel(discord_id)


def _label(index: ServerIndex, ref: str) -> str:
    return index.names.get(ref, ref)


async def _guarded(report: ApplyReport, what: str, coro) -> bool:
    """Await one change; record a failure instead of aborting the whole run."""
    try:
        await coro
        return True
    except discord.Forbidden:
        report.errors.append(f"{what}: I don't have permission (check my role's position and permissions).")
    except discord.HTTPException as exc:
        report.errors.append(f"{what}: {exc.text or exc}")
    return False


async def _apply_edits(guild, plan, index, resolver, categories, report, dry_run, progress, reason) -> None:
    for edit in plan.role_edits:
        name = _label(index, edit.ref)
        role = _lookup(guild, index, edit.ref)
        if role is None:
            report.notes.append(f"Role **{name}** no longer exists, so I skipped changing it.")
            continue
        if (why := role_lock_reason(guild, role)):
            report.errors.append(f"Role **{name}** wasn't changed: {why}.")
            continue
        kwargs: dict = {}
        if edit.new_name and edit.new_name != role.name:
            kwargs["name"] = edit.new_name
        if edit.color is not None and edit.color != role.colour.value:
            kwargs["colour"] = discord.Colour(edit.color)
        if edit.hoist is not None and edit.hoist != role.hoist:
            kwargs["hoist"] = edit.hoist
        if edit.mentionable is not None and edit.mentionable != role.mentionable:
            kwargs["mentionable"] = edit.mentionable
        if edit.permissions is not None:
            wanted = build_permissions(edit.permissions)
            if wanted != role.permissions:
                kwargs["permissions"] = wanted
        if not kwargs:
            continue
        if dry_run:
            report.edited.append(f"role {name}")
            continue
        await _say(progress, f"Updating role **{name}**…")
        if await _guarded(report, f"Role **{name}**", role.edit(reason=reason, **kwargs)):
            report.edited.append(f"role {name}")

    for edit in plan.category_edits:
        name = _label(index, edit.ref)
        category = _lookup(guild, index, edit.ref)
        if category is None:
            report.notes.append(f"Category **{name}** no longer exists, so I skipped renaming it.")
        elif edit.new_name != category.name:
            if dry_run:
                report.edited.append(f"category {name}")
                continue
            await _say(progress, f"Renaming category **{name}**…")
            if await _guarded(report, f"Category **{name}**", category.edit(name=edit.new_name, reason=reason)):
                report.edited.append(f"category {name}")

    for edit in plan.channel_edits:
        await _edit_channel(guild, edit, index, resolver, categories, report, dry_run, progress, reason)


async def _edit_channel(guild, edit: ChannelEdit, index, resolver, categories, report, dry_run, progress, reason) -> None:
    name = _label(index, edit.ref)
    channel = _lookup(guild, index, edit.ref)
    if channel is None:
        report.notes.append(f"Channel **{name}** no longer exists, so I skipped changing it.")
        return
    if (why := channel_lock_reason(guild, channel)):
        report.errors.append(f"Channel **{name}** wasn't changed: {why}.")
        return

    voice = isinstance(channel, _VOICE_TYPES)
    kwargs: dict = {}
    if edit.new_name and edit.new_name != channel.name:
        kwargs["name"] = edit.new_name
    if edit.topic and not voice and edit.topic != (getattr(channel, "topic", None) or ""):
        kwargs["topic"] = edit.topic
    if edit.move_to:
        target, found = None, True
        if edit.move_to != "none":
            target = (_lookup(guild, index, edit.move_to) if edit.move_to in index.ids
                      else categories.get(edit.move_to.lower()))
            found = target is not None
        if not found:
            report.notes.append(f"Couldn't find the category to move **{name}** into, so it stays put.")
        elif channel.category != target:
            kwargs["category"] = target

    changes = _overwrite_changes(guild, channel, edit, resolver, report)
    if not kwargs and not changes:
        return
    if dry_run:
        report.edited.append(f"channel {name}")
        return

    await _say(progress, f"Updating **{name}**…")
    ok = True
    if kwargs:
        ok = await _guarded(report, f"Channel **{name}**", channel.edit(reason=reason, **kwargs))
    for target, perms in changes.items():
        ok = await _guarded(report, f"Permissions on **{name}**", _merge_overwrite(channel, target, perms, reason)) and ok
    if ok:
        report.edited.append(f"channel {name}")


def _overwrite_changes(guild, channel, edit: ChannelEdit, resolver, report) -> dict:
    """Group every permission change for a channel by target, so each target is written once.

    Writes must be merged into the existing overwrite, and discord.py's cache only
    catches up after the gateway event, so two separate merges for one target in the
    same run would clobber each other.
    """
    changes: dict = defaultdict(dict)
    everyone = guild.default_role
    if edit.visibility == "private":
        roles, missing = resolver.resolve(edit.private_to)
        _note_missing_roles(report, missing, "a channel being made private")
        changes[everyone]["view_channel"] = False
        for role in roles:
            changes[role]["view_channel"] = True
        changes[guild.me].update(view_channel=True, send_messages=True)
    elif edit.visibility == "public":
        changes[everyone]["view_channel"] = None

    if edit.read_only is True:
        writers, missing = resolver.resolve(edit.writers)
        _note_missing_roles(report, missing, "a read-only channel (writers)")
        changes[everyone].update(send_messages=False, send_messages_in_threads=False,
                                 create_public_threads=False, create_private_threads=False)
        for role in writers:
            changes[role]["send_messages"] = True
        changes[guild.me]["send_messages"] = True
    elif edit.read_only is False:
        changes[everyone].update(send_messages=None, send_messages_in_threads=None,
                                 create_public_threads=None, create_private_threads=None)

    # Keep only what would actually change, so a no-op is neither reported nor sent to Discord.
    effective: dict = {}
    for target, perms in changes.items():
        current = channel.overwrites_for(target)
        differing = {name: value for name, value in perms.items() if getattr(current, name) != value}
        if differing:
            effective[target] = differing
    return effective


async def _merge_overwrite(channel, target, perms: dict, reason: str) -> None:
    """Change some permissions for ``target`` without disturbing the rest of its overwrite."""
    overwrite = channel.overwrites_for(target)
    overwrite.update(**perms)
    if overwrite.is_empty():
        if target in channel.overwrites:
            await channel.set_permissions(target, overwrite=None, reason=reason)
        return
    await channel.set_permissions(target, overwrite=overwrite, reason=reason)


# ── Ordering ─────────────────────────────────────────────────────────────────
async def _apply_order(guild, plan, index, report, dry_run, progress, reason) -> None:
    if plan.role_order:
        roles = [r for r in (_lookup(guild, index, ref) for ref in plan.role_order)
                 if r is not None and not role_lock_reason(guild, r)]
        if len(roles) >= 2:
            report.reordered.append("roles")
            if not dry_run:
                await _say(progress, "Putting roles in order…")
                await _order_roles(guild, roles, report, reason)

    if plan.category_order:
        cats = [c for c in (_lookup(guild, index, ref) for ref in plan.category_order) if c is not None]
        if len(cats) >= 2:
            report.reordered.append("categories")
            if not dry_run:
                await _say(progress, "Putting categories in order…")
                for previous, category in zip(cats, cats[1:]):
                    if not await _guarded(report, f"Category **{category.name}**", category.move(after=previous, reason=reason)):
                        break


# ── Deleting ─────────────────────────────────────────────────────────────────
_DELETE_ORDER = {"channel": 0, "category": 1, "role": 2}


async def _apply_deletions(guild, plan, index, report, dry_run, progress, reason) -> None:
    def order(ref: str) -> int:
        kind = index.kinds.get(ref)
        return _DELETE_ORDER["role" if kind == "role" else "category" if kind == "category" else "channel"]

    doomed = set(plan.deletions)
    for ref in sorted(plan.deletions, key=order):
        name = _label(index, ref)
        kind = index.kinds.get(ref, "item")
        item = _lookup(guild, index, ref)
        if item is None:
            report.notes.append(f"**{name}** is already gone.")
            continue
        why = role_lock_reason(guild, item) if kind == "role" else channel_lock_reason(guild, item)
        if why:
            report.errors.append(f"**{name}** wasn't deleted: {why}.")
            continue
        if kind == "category":
            stay = [c for c in item.channels if _ref_of(index, c.id) not in doomed]
            if stay:
                report.notes.append(f"Category **{name}** is deleted but its {len(stay)} channel(s) stay, uncategorised.")
        label = f"{'channel' if kind not in ('role', 'category') else kind} {name}"
        if dry_run:
            report.deleted.append(label)
            continue
        await _say(progress, f"Deleting **{name}**…")
        if await _guarded(report, f"**{name}**", item.delete(reason=reason)):
            report.deleted.append(label)


def _ref_of(index: ServerIndex, discord_id: int) -> Optional[str]:
    return next((ref for ref, i in index.ids.items() if i == discord_id), None)


# ── Presentation ─────────────────────────────────────────────────────────────
_ICONS = {"text": "#", "announcement": "📣", "forum": "💬", "voice": "🔊"}


def plan_lines(plan: ServerPlan, preview: ApplyReport) -> tuple[list[str], list[str]]:
    """(role_lines, structure_lines) for what the plan creates, marking what already exists."""
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


def change_lines(plan: ServerPlan, index: ServerIndex) -> tuple[list[str], list[str]]:
    """(edit_lines, delete_lines): what the plan does to items that already exist."""
    def nm(ref: str) -> str:
        return index.names.get(ref, ref)

    edits: list[str] = []
    for e in plan.role_edits:
        parts = []
        if e.new_name:
            parts.append(f"rename → **{e.new_name}**")
        if e.color is not None:
            parts.append(f"colour #{e.color:06x}")
        if e.hoist is not None:
            parts.append("shown separately" if e.hoist else "not shown separately")
        if e.mentionable is not None:
            parts.append("mentionable" if e.mentionable else "not mentionable")
        if e.permissions is not None:
            parts.append(f"⚠️ permissions replaced ({len(e.permissions)} kept)")
        edits.append(f"• role **{nm(e.ref)}**: " + ", ".join(parts))
    for e in plan.category_edits:
        edits.append(f"• category **{nm(e.ref)}**: rename → **{e.new_name}**")
    for e in plan.channel_edits:
        parts = []
        if e.new_name:
            parts.append(f"rename → **{e.new_name}**")
        if e.topic:
            parts.append("new topic")
        if e.move_to:
            parts.append("remove from category" if e.move_to == "none"
                         else f"move → **{nm(e.move_to) if e.move_to in index.names else e.move_to}**")
        if e.visibility != "keep":
            parts.append("make private 🔒" if e.visibility == "private" else "make public")
        if e.read_only is not None:
            parts.append("read-only" if e.read_only else "allow posting")
        edits.append(f"• channel **{nm(e.ref)}**: " + ", ".join(parts))
    if plan.role_order:
        edits.append("• reorder roles: " + " › ".join(nm(r) for r in plan.role_order))
    if plan.category_order:
        edits.append("• reorder categories: " + " › ".join(nm(r) for r in plan.category_order))

    deletes: list[str] = []
    for ref in plan.deletions:
        kind = index.kinds.get(ref, "item")
        deletes.append(f"🗑️ {'channel' if kind not in ('role', 'category') else kind} **{nm(ref)}**")
    return edits, deletes


def full_plan_text(plan: ServerPlan, preview: ApplyReport, index: ServerIndex) -> str:
    """Plain-text version of the whole plan, attached to the preview when it is long or risky."""
    role_lines, structure = plan_lines(plan, preview)
    edits, deletes = change_lines(plan, index)
    plain = lambda lines: [line.replace("**", "") for line in lines]   # noqa: E731
    sections = [
        ("SUMMARY", [plan.summary] if plan.summary else []),
        ("NOTE", [plan.notes] if plan.notes else []),
        ("NEW ROLES", plain(role_lines)),
        ("NEW CHANNELS", plain(structure)),
        ("CHANGES TO EXISTING ITEMS", plain(edits)),
        ("WILL BE DELETED", plain(deletes)),
        ("WARNINGS", plan.warnings + preview.notes),
    ]
    return "\n\n".join(f"== {title} ==\n" + "\n".join(lines) for title, lines in sections if lines) + "\n"


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
    if report.edited or report.reordered:
        lines.append(f"{'Would change' if report.dry_run else 'Changed'} **{len(report.edited)}** existing item(s)"
                     + (f" and reordered {' and '.join(report.reordered)}." if report.reordered else "."))
    if report.deleted:
        lines.append(f"{'Would delete' if report.dry_run else 'Deleted'} **{len(report.deleted)}** item(s).")
    skipped = len(report.roles_existing) + len(report.categories_existing) + len(report.channels_existing)
    if skipped:
        lines.append(f"Left **{skipped}** existing item(s) untouched.")
    if report.backup_path:
        lines.append(f"Backup of the old structure saved to `{report.backup_path}`.")
    return lines
