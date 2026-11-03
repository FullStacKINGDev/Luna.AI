"""Server plan: the blueprint Claude proposes and the applier builds.

Pure Python (no discord import) so it can be unit-tested without a bot.
Everything the model returns is treated as untrusted input: ``parse_plan``
clamps sizes, sanitises names, drops unsafe permissions and records a warning
for every change it makes, so the admin sees exactly what was adjusted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Limits (well under Discord's hard limits: 250 roles, 500 channels) ───────
MAX_ROLES = 25
MAX_CATEGORIES = 15
MAX_CHANNELS_PER_CATEGORY = 20
MAX_CHANNELS = 80
MAX_NAME = 100
MAX_TOPIC = 1024
MAX_SUMMARY = 400

CHANNEL_KINDS = ("text", "announcement", "forum", "voice")
TEXT_LIKE = frozenset({"text", "announcement", "forum"})

# Permissions a generated role may carry. Anything that lets a role take over
# the server (administrator, manage_guild/roles/channels/webhooks, ban/kick,
# mention_everyone, ...) is deliberately absent - the admin can grant those by
# hand after reviewing the result.
SAFE_PERMISSIONS = (
    "view_channel", "send_messages", "send_messages_in_threads",
    "create_public_threads", "embed_links", "attach_files", "add_reactions",
    "use_external_emojis", "use_external_stickers", "read_message_history",
    "use_application_commands", "change_nickname", "create_instant_invite",
    "connect", "speak", "stream", "use_voice_activation", "priority_speaker",
    "request_to_speak",
    # Moderation
    "manage_messages", "manage_threads", "manage_nicknames",
    "moderate_members", "mute_members", "deafen_members", "move_members",
    "view_audit_log",
)
_SAFE_SET = frozenset(SAFE_PERMISSIONS)

_RESERVED_ROLE_NAMES = frozenset({"everyone", "@everyone", "here", "@here"})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
DEFAULT_ROLE_COLOR = 0x99AAB5  # Discord's neutral grey


class PlanError(ValueError):
    """The model's output could not be turned into a usable plan."""


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class RoleSpec:
    name: str
    color: int = DEFAULT_ROLE_COLOR
    hoist: bool = False
    mentionable: bool = False
    permissions: tuple[str, ...] = ()


@dataclass
class ChannelSpec:
    name: str
    kind: str = "text"
    topic: str = ""
    read_only: bool = False
    writers: tuple[str, ...] = ()      # roles that may post in a read-only channel
    private_to: tuple[str, ...] = ()   # non-empty => hidden from @everyone


@dataclass
class CategorySpec:
    name: str
    private_to: tuple[str, ...] = ()
    channels: list[ChannelSpec] = field(default_factory=list)


@dataclass
class ServerPlan:
    summary: str = ""
    roles: list[RoleSpec] = field(default_factory=list)
    categories: list[CategorySpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        return sum(len(c.channels) for c in self.categories)

    def is_empty(self) -> bool:
        return not self.roles and not self.categories


# ── JSON schema sent to Claude (structured outputs) ──────────────────────────
# Every property is required and every object is closed, which is what the
# structured-outputs subset needs. Length/size limits are enforced in
# ``parse_plan`` because min/max constraints are not supported by the API.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences describing the server design.",
        },
        "roles": {
            "type": "array",
            "description": "Roles ordered from most to least senior.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "Hex colour such as #5865F2"},
                    "hoist": {"type": "boolean", "description": "Show separately in the member list"},
                    "mentionable": {"type": "boolean"},
                    "permissions": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SAFE_PERMISSIONS)},
                    },
                },
                "required": ["name", "color", "hoist", "mentionable", "permissions"],
                "additionalProperties": False,
            },
        },
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "private_to": {
                        "type": "array",
                        "description": "Role names that can see this category. Empty = everyone.",
                        "items": {"type": "string"},
                    },
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "kind": {"type": "string", "enum": list(CHANNEL_KINDS)},
                                "topic": {"type": "string", "description": "Short description, or empty."},
                                "read_only": {"type": "boolean", "description": "Members can read but not post."},
                                "writers": {
                                    "type": "array",
                                    "description": "Role names allowed to post in a read-only channel.",
                                    "items": {"type": "string"},
                                },
                                "private_to": {
                                    "type": "array",
                                    "description": "Role names that can see this channel. Empty = inherit the category.",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["name", "kind", "topic", "read_only", "writers", "private_to"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "private_to", "channels"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "roles", "categories"],
    "additionalProperties": False,
}


# ── Sanitising helpers ───────────────────────────────────────────────────────
def clean_text(value: Any, limit: int) -> str:
    """Collapse whitespace, drop control characters and truncate."""
    if not isinstance(value, str):
        return ""
    text = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", value)).strip()
    return text[:limit].strip()


def channel_slug(name: str) -> str:
    """Discord-style text channel name: lowercase, hyphens instead of spaces."""
    slug = clean_text(name, MAX_NAME * 2).lower().replace(" ", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:MAX_NAME].strip("-")


def parse_color(value: Any) -> int:
    if isinstance(value, str):
        match = _HEX_COLOR.match(value.strip())
        if match:
            return int(match.group(1), 16)
    return DEFAULT_ROLE_COLOR


def _clean_names(value: Any, limit: int = MAX_NAME) -> tuple[str, ...]:
    """A list of role-name references: cleaned, de-duplicated, order kept."""
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        name = clean_text(item, limit).lstrip("@").strip()
        key = name.lower()
        if name and key not in _RESERVED_ROLE_NAMES and key not in seen:
            seen.add(key)
            out.append(name)
    return tuple(out)


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_plan(data: Any) -> ServerPlan:
    """Validate and normalise the model's JSON into a ``ServerPlan``.

    Never raises for individual bad items - they are dropped with a warning.
    Raises ``PlanError`` only when nothing usable is left.
    """
    if not isinstance(data, dict):
        raise PlanError("The AI response was not a JSON object.")

    plan = ServerPlan(summary=clean_text(data.get("summary"), MAX_SUMMARY))
    warn = plan.warnings.append

    # Roles -------------------------------------------------------------------
    seen_roles: set[str] = set()
    for raw in _as_list(data.get("roles")):
        if not isinstance(raw, dict):
            continue
        name = clean_text(raw.get("name"), MAX_NAME).lstrip("@").strip()
        key = name.lower()
        if not name:
            continue
        if key in _RESERVED_ROLE_NAMES:
            warn(f"Skipped role '{name}': that name is reserved by Discord.")
            continue
        if key in seen_roles:
            continue
        if len(plan.roles) >= MAX_ROLES:
            warn(f"Role limit reached ({MAX_ROLES}); extra roles were dropped.")
            break
        seen_roles.add(key)

        perms: list[str] = []
        for perm in _as_list(raw.get("permissions")):
            if isinstance(perm, str) and perm in _SAFE_SET:
                if perm not in perms:
                    perms.append(perm)
            else:
                warn(f"Removed permission '{perm}' from role '{name}' (not allowed for generated roles).")
        plan.roles.append(RoleSpec(
            name=name,
            color=parse_color(raw.get("color")),
            hoist=bool(raw.get("hoist")),
            mentionable=bool(raw.get("mentionable")),
            permissions=tuple(perms),
        ))

    # Categories & channels ---------------------------------------------------
    seen_categories: set[str] = set()
    total_channels = 0
    for raw_cat in _as_list(data.get("categories")):
        if not isinstance(raw_cat, dict):
            continue
        cat_name = clean_text(raw_cat.get("name"), MAX_NAME)
        cat_key = cat_name.lower()
        if not cat_name or cat_key in seen_categories:
            continue
        if len(plan.categories) >= MAX_CATEGORIES:
            warn(f"Category limit reached ({MAX_CATEGORIES}); extra categories were dropped.")
            break
        seen_categories.add(cat_key)

        category = CategorySpec(name=cat_name, private_to=_clean_names(raw_cat.get("private_to")))
        seen_channels: set[tuple[bool, str]] = set()
        for raw_ch in _as_list(raw_cat.get("channels")):
            if not isinstance(raw_ch, dict):
                continue
            kind = raw_ch.get("kind") if raw_ch.get("kind") in CHANNEL_KINDS else "text"
            if kind in TEXT_LIKE:
                name = channel_slug(raw_ch.get("name", ""))
            else:
                name = clean_text(raw_ch.get("name"), MAX_NAME)
            if not name:
                continue
            dedupe = (kind == "voice", name.lower())
            if dedupe in seen_channels:
                continue
            if len(category.channels) >= MAX_CHANNELS_PER_CATEGORY:
                warn(f"Category '{cat_name}' is capped at {MAX_CHANNELS_PER_CATEGORY} channels; extras were dropped.")
                break
            if total_channels >= MAX_CHANNELS:
                warn(f"Channel limit reached ({MAX_CHANNELS}); extra channels were dropped.")
                break
            seen_channels.add(dedupe)
            total_channels += 1
            category.channels.append(ChannelSpec(
                name=name,
                kind=kind,
                topic=clean_text(raw_ch.get("topic"), MAX_TOPIC) if kind != "voice" else "",
                read_only=bool(raw_ch.get("read_only")) and kind != "voice",
                writers=_clean_names(raw_ch.get("writers")),
                private_to=_clean_names(raw_ch.get("private_to")),
            ))
        if category.channels:
            plan.categories.append(category)
        else:
            warn(f"Skipped empty category '{cat_name}'.")

    if plan.is_empty():
        raise PlanError("The AI response did not contain any roles or channels.")
    return plan


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []
