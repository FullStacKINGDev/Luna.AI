"""Server plan: the blueprint the AI proposes and the applier carries out.

Pure Python (no discord import) so it can be unit-tested without a bot.
Everything the model returns is treated as untrusted input: ``parse_plan``
clamps sizes, sanitises names, validates every reference to an existing item
and records a warning for every change it makes, so the admin sees exactly
what was adjusted.

Existing roles, categories and channels are referred to by opaque refs such as
``role:3`` / ``cat:1`` / ``ch:12`` that Luna hands the model in the server
snapshot. A ref the snapshot never contained (invented, or a locked item) is
dropped, so the model cannot touch anything it was not shown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# ── Limits (well under Discord's hard limits: 250 roles, 500 channels) ───────
MAX_ROLES = 25
MAX_CATEGORIES = 15
MAX_CHANNELS_PER_CATEGORY = 20
MAX_CHANNELS = 80
MAX_EDITS = 100
MAX_DELETIONS = 60
MAX_NAME = 100
MAX_TOPIC = 1024
MAX_SUMMARY = 400
MAX_NOTES = 600

CHANNEL_KINDS = ("text", "announcement", "forum", "voice")
TEXT_LIKE = frozenset({"text", "announcement", "forum"})

# Everyday permissions. The model may hand these out freely.
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

# Powers that can take over or damage a server. The model may only use these
# when the admin explicitly asks, and a plan that does is held behind a typed
# confirmation before anything is applied.
ELEVATED_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "manage_expressions", "manage_events",
    "ban_members", "kick_members", "mention_everyone",
)
_SAFE_SET = frozenset(SAFE_PERMISSIONS)
_ELEVATED_SET = frozenset(ELEVATED_PERMISSIONS)
_ALLOWED_PERMISSIONS = _SAFE_SET | _ELEVATED_SET
PERMISSION_CHOICES = SAFE_PERMISSIONS + ELEVATED_PERMISSIONS

_RESERVED_ROLE_NAMES = frozenset({"everyone", "@everyone", "here", "@here"})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
_REF = re.compile(r"^(role|cat|ch):\d{1,6}$")
DEFAULT_ROLE_COLOR = 0x99AAB5  # Discord's neutral grey


class PlanError(ValueError):
    """The model's output could not be turned into a usable plan."""


class EmptyPlanError(PlanError):
    """The model understood the request but found nothing to do.

    ``notes`` carries its explanation (for example "That role is locked").
    """

    def __init__(self, message: str, notes: str = "", summary: str = ""):
        super().__init__(message)
        self.notes = notes
        self.summary = summary


def elevated_in(permissions) -> tuple[str, ...]:
    return tuple(p for p in permissions or () if p in _ELEVATED_SET)


# ── Data model: things to create ─────────────────────────────────────────────
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


# ── Data model: changes to existing items (all by ref) ───────────────────────
@dataclass
class RoleEdit:
    ref: str
    new_name: str = ""
    color: Optional[int] = None
    hoist: Optional[bool] = None
    mentionable: Optional[bool] = None
    permissions: Optional[tuple[str, ...]] = None   # None = keep; a tuple REPLACES the role's permissions


@dataclass
class CategoryEdit:
    ref: str
    new_name: str = ""


@dataclass
class ChannelEdit:
    ref: str
    new_name: str = ""
    topic: str = ""
    move_to: str = ""                 # "" keep | "none" no category | a category ref | a category this plan creates
    visibility: str = "keep"          # keep | public | private
    private_to: tuple[str, ...] = ()
    read_only: Optional[bool] = None
    writers: tuple[str, ...] = ()


@dataclass
class ServerPlan:
    summary: str = ""
    notes: str = ""   # the model's heads-up: what it couldn't do, or why there is nothing to do
    roles: list[RoleSpec] = field(default_factory=list)
    categories: list[CategorySpec] = field(default_factory=list)
    role_edits: list[RoleEdit] = field(default_factory=list)
    category_edits: list[CategoryEdit] = field(default_factory=list)
    channel_edits: list[ChannelEdit] = field(default_factory=list)
    role_order: list[str] = field(default_factory=list)       # refs, most senior first
    category_order: list[str] = field(default_factory=list)   # refs, top to bottom
    deletions: list[str] = field(default_factory=list)        # refs of roles / categories / channels
    warnings: list[str] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        return sum(len(c.channels) for c in self.categories)

    @property
    def edit_count(self) -> int:
        return (len(self.role_edits) + len(self.category_edits) + len(self.channel_edits)
                + (1 if self.role_order else 0) + (1 if self.category_order else 0))

    @property
    def has_changes_to_existing(self) -> bool:
        return bool(self.edit_count or self.deletions)

    def is_empty(self) -> bool:
        return not (self.roles or self.categories or self.edit_count or self.deletions)

    def risk_reasons(self) -> list[str]:
        """Why this plan needs a typed confirmation (empty list = ordinary plan)."""
        reasons = []
        if self.deletions:
            reasons.append(f"{len(self.deletions)} permanent deletion(s)")
        grants = [r for r in self.roles if elevated_in(r.permissions)]
        grants += [e for e in self.role_edits if elevated_in(e.permissions)]
        if grants:
            reasons.append(f"{len(grants)} role(s) given powerful permissions")
        resets = [e for e in self.role_edits if e.permissions is not None]
        if resets:
            reasons.append(f"{len(resets)} role(s) having their permissions replaced")
        return reasons


# ── JSON schema sent to the model (structured outputs, strict mode) ──────────
# Every property is required and every object is closed, which is what strict
# structured outputs need. "No change" is spelled "" / "keep" rather than a
# missing field. Size limits are enforced in ``parse_plan``, because strict mode
# supports few constraints.
_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}
_PERMS = {"type": "array", "items": {"type": "string", "enum": list(PERMISSION_CHOICES)}}


def _tri(description: str) -> dict:
    return {"type": "string", "enum": ["keep", "yes", "no"], "description": description}


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


PLAN_SCHEMA: dict[str, Any] = _obj({
    "summary": {"type": "string", "description": "One or two sentences describing the result."},
    "notes": {
        "type": "string",
        "description": (
            "Plain-language heads-up for the admin: anything they asked for that you cannot do, "
            "or why there is nothing to do. Empty string if there is nothing to flag."
        ),
    },
    "roles": {
        "type": "array",
        "description": "NEW roles to create, most senior first.",
        "items": _obj({
            "name": _STR,
            "color": {"type": "string", "description": "Hex colour such as #5865F2"},
            "hoist": {"type": "boolean", "description": "Show separately in the member list"},
            "mentionable": {"type": "boolean"},
            "permissions": _PERMS,
        }),
    },
    "categories": {
        "type": "array",
        "description": "NEW categories to create, each with its NEW channels.",
        "items": _obj({
            "name": _STR,
            "private_to": {"type": "array", "items": _STR,
                           "description": "Role names that can see this category. Empty = everyone."},
            "channels": {
                "type": "array",
                "items": _obj({
                    "name": _STR,
                    "kind": {"type": "string", "enum": list(CHANNEL_KINDS)},
                    "topic": {"type": "string", "description": "Short description, or empty."},
                    "read_only": {"type": "boolean", "description": "Members can read but not post."},
                    "writers": {"type": "array", "items": _STR,
                                "description": "Role names allowed to post in a read-only channel."},
                    "private_to": {"type": "array", "items": _STR,
                                   "description": "Role names that can see this channel. Empty = inherit the category."},
                }),
            },
        }),
    },
    "edit_roles": {
        "type": "array",
        "description": "Changes to EXISTING roles, by ref.",
        "items": _obj({
            "ref": {"type": "string", "description": "The role's ref, e.g. role:3"},
            "new_name": {"type": "string", "description": "Empty = keep the name."},
            "color": {"type": "string", "description": "Hex colour, or empty to keep."},
            "hoist": _tri("Show separately in the member list"),
            "mentionable": _tri("Anyone can @mention the role"),
            "permissions_mode": {"type": "string", "enum": ["keep", "replace"],
                                 "description": "replace = the role gets exactly `permissions` and loses all others."},
            "permissions": _PERMS,
        }),
    },
    "edit_categories": {
        "type": "array",
        "description": "Changes to EXISTING categories, by ref.",
        "items": _obj({
            "ref": {"type": "string", "description": "The category's ref, e.g. cat:2"},
            "new_name": {"type": "string", "description": "Empty = keep the name."},
        }),
    },
    "edit_channels": {
        "type": "array",
        "description": "Changes to EXISTING channels, by ref.",
        "items": _obj({
            "ref": {"type": "string", "description": "The channel's ref, e.g. ch:12"},
            "new_name": {"type": "string", "description": "Empty = keep the name."},
            "topic": {"type": "string", "description": "New topic, or empty to keep."},
            "move_to": {"type": "string",
                        "description": "Empty = stay. 'none' = remove from its category. Otherwise a category ref "
                                       "(cat:2) or the exact name of a category created in this plan."},
            "visibility": {"type": "string", "enum": ["keep", "public", "private"]},
            "private_to": {"type": "array", "items": _STR,
                           "description": "With visibility=private: role names that can see it."},
            "read_only": _tri("Members can read but not post"),
            "writers": {"type": "array", "items": _STR,
                        "description": "With read_only=yes: role names that may still post."},
        }),
    },
    "role_order": {"type": "array", "items": _STR,
                   "description": "Role refs in the order they should sit, most senior first. Empty = leave."},
    "category_order": {"type": "array", "items": _STR,
                       "description": "Category refs in the order they should appear, top first. Empty = leave."},
    "delete": {"type": "array", "items": _STR,
               "description": "Refs of roles, categories or channels to PERMANENTLY delete. "
                              "Only what the admin clearly asked to remove."},
})


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


def _optional_color(value: Any) -> Optional[int]:
    if isinstance(value, str) and _HEX_COLOR.match(value.strip()):
        return parse_color(value)
    return None


def _yes_no(value: Any) -> Optional[bool]:
    return True if value == "yes" else False if value == "no" else None


def _ref(value: Any) -> str:
    text = clean_text(value, 20).lower()
    return text if _REF.match(text) else ""


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


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_plan(data: Any, refs: Optional[Mapping[str, str]] = None) -> ServerPlan:
    """Validate and normalise the model's JSON into a ``ServerPlan``.

    ``refs`` maps each ref the model was shown to its kind ("role", "category",
    or a channel kind). Edits and deletions naming any other ref are dropped.
    Never raises for individual bad items - they are dropped with a warning.
    Raises ``EmptyPlanError`` (a ``PlanError``) when nothing usable is left.
    """
    if not isinstance(data, dict):
        raise PlanError("The AI response was not a JSON object.")
    refs = refs or {}

    plan = ServerPlan(
        summary=clean_text(data.get("summary"), MAX_SUMMARY),
        notes=clean_text(data.get("notes"), MAX_NOTES),
    )
    _parse_new_roles(plan, data)
    _parse_new_categories(plan, data)
    _parse_changes(plan, data, refs)

    if plan.is_empty():
        raise EmptyPlanError("The AI response did not contain anything to do.", notes=plan.notes, summary=plan.summary)
    return plan


def _parse_new_roles(plan: ServerPlan, data: dict) -> None:
    warn = plan.warnings.append
    seen: set[str] = set()
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
        if key in seen:
            continue
        if len(plan.roles) >= MAX_ROLES:
            warn(f"Role limit reached ({MAX_ROLES}); extra roles were dropped.")
            break
        seen.add(key)
        plan.roles.append(RoleSpec(
            name=name,
            color=parse_color(raw.get("color")),
            hoist=bool(raw.get("hoist")),
            mentionable=bool(raw.get("mentionable")),
            permissions=_clean_permissions(raw.get("permissions"), name, warn),
        ))


def _clean_permissions(value: Any, role_name: str, warn) -> tuple[str, ...]:
    perms: list[str] = []
    for perm in _as_list(value):
        if isinstance(perm, str) and perm in _ALLOWED_PERMISSIONS:
            if perm not in perms:
                perms.append(perm)
        else:
            warn(f"Removed unknown permission '{perm}' from role '{role_name}'.")
    return tuple(perms)


def _parse_new_categories(plan: ServerPlan, data: dict) -> None:
    warn = plan.warnings.append
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
            name = channel_slug(raw_ch.get("name", "")) if kind in TEXT_LIKE else clean_text(raw_ch.get("name"), MAX_NAME)
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


def _parse_changes(plan: ServerPlan, data: dict, refs: Mapping[str, str]) -> None:
    """Edits, ordering and deletions of existing items."""
    warn = plan.warnings.append

    def known(raw: Any, *kinds: str) -> str:
        ref = _ref(raw)
        if ref and refs.get(ref) in kinds:
            return ref
        warn(f"Ignored a change to an item I can't touch ('{clean_text(raw, 20) or '?'}').")
        return ""

    # Deletions first, so edits to a doomed item can be dropped.
    seen_delete: set[str] = set()
    for raw in _as_list(data.get("delete")):
        ref = known(raw, "role", "category", *CHANNEL_KINDS)
        if not ref or ref in seen_delete:
            continue
        if len(plan.deletions) >= MAX_DELETIONS:
            warn(f"Deletion limit reached ({MAX_DELETIONS}); the rest were not scheduled.")
            break
        seen_delete.add(ref)
        plan.deletions.append(ref)

    def alive(ref: str) -> bool:
        if ref in seen_delete:
            warn(f"Dropped an edit to '{ref}' because it is being deleted.")
            return False
        return True

    edits_left = MAX_EDITS
    seen: set[str] = set()

    for raw in _as_list(data.get("edit_roles")):
        if not isinstance(raw, dict) or edits_left <= 0:
            continue
        ref = known(raw.get("ref"), "role")
        if not ref or ref in seen or not alive(ref):
            continue
        new_name = clean_text(raw.get("new_name"), MAX_NAME).lstrip("@").strip()
        if new_name.lower() in _RESERVED_ROLE_NAMES:
            warn(f"Skipped renaming a role to '{new_name}': that name is reserved by Discord.")
            new_name = ""
        edit = RoleEdit(
            ref=ref, new_name=new_name,
            color=_optional_color(raw.get("color")),
            hoist=_yes_no(raw.get("hoist")),
            mentionable=_yes_no(raw.get("mentionable")),
            permissions=(_clean_permissions(raw.get("permissions"), ref, warn)
                         if raw.get("permissions_mode") == "replace" else None),
        )
        if any(v not in ("", None) for v in (edit.new_name, edit.color, edit.hoist, edit.mentionable, edit.permissions)):
            seen.add(ref)
            plan.role_edits.append(edit)
            edits_left -= 1

    for raw in _as_list(data.get("edit_categories")):
        if not isinstance(raw, dict) or edits_left <= 0:
            continue
        ref = known(raw.get("ref"), "category")
        new_name = clean_text(raw.get("new_name"), MAX_NAME)
        if ref and ref not in seen and alive(ref) and new_name:
            seen.add(ref)
            plan.category_edits.append(CategoryEdit(ref=ref, new_name=new_name))
            edits_left -= 1

    new_category_names = {c.name.lower() for c in plan.categories}
    for raw in _as_list(data.get("edit_channels")):
        if not isinstance(raw, dict) or edits_left <= 0:
            continue
        ref = known(raw.get("ref"), *CHANNEL_KINDS)
        if not ref or ref in seen or not alive(ref):
            continue
        kind = refs[ref]
        text_like = kind in TEXT_LIKE
        new_name = channel_slug(raw.get("new_name", "")) if text_like else clean_text(raw.get("new_name"), MAX_NAME)

        move_to = clean_text(raw.get("move_to"), MAX_NAME)
        if move_to.lower() == "none":
            move_to = "none"
        elif _ref(move_to):
            move_to = _ref(move_to)
            if refs.get(move_to) != "category" or move_to in seen_delete:
                warn(f"Ignored moving a channel to '{move_to}': not a category I can use.")
                move_to = ""
        elif move_to and move_to.lower() not in new_category_names:
            warn(f"Ignored moving a channel to '{move_to}': no such category in this plan.")
            move_to = ""

        visibility = raw.get("visibility") if raw.get("visibility") in ("public", "private") else "keep"
        edit = ChannelEdit(
            ref=ref, new_name=new_name,
            topic=clean_text(raw.get("topic"), MAX_TOPIC) if text_like else "",
            move_to=move_to, visibility=visibility,
            private_to=_clean_names(raw.get("private_to")),
            read_only=_yes_no(raw.get("read_only")) if kind != "voice" else None,
            writers=_clean_names(raw.get("writers")),
        )
        if any(v not in ("", None, "keep") for v in (edit.new_name, edit.topic, edit.move_to, edit.visibility, edit.read_only)):
            seen.add(ref)
            plan.channel_edits.append(edit)
            edits_left -= 1

    plan.role_order = _parse_order(data.get("role_order"), "role", known, seen_delete)
    plan.category_order = _parse_order(data.get("category_order"), "category", known, seen_delete)


def _parse_order(value: Any, kind: str, known, deleted: set[str]) -> list[str]:
    ordered: list[str] = []
    for raw in _as_list(value):
        ref = known(raw, kind)
        if ref and ref not in ordered and ref not in deleted:
            ordered.append(ref)
    return ordered if len(ordered) >= 2 else []
