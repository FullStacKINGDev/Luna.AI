import asyncio
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import discord

from luna import applier
from luna.plan import parse_plan

_ids = itertools.count(1000)


def run(coro):
    return asyncio.run(coro)


def http_error(status, text="boom"):
    return discord.HTTPException(mock.Mock(status=status, reason="x"), text)


def forbidden(text="Missing Permissions"):
    return discord.Forbidden(mock.Mock(status=403, reason="x"), text)


def fake_role(name, *, default=False, managed=False, position=1, perms=0, colour=0, hoist=False, mentionable=False):
    role = mock.MagicMock(spec=discord.Role)
    role.id = next(_ids)
    role.name, role.position, role.managed = name, position, managed
    role.permissions = discord.Permissions(perms)
    role.colour = discord.Colour(colour)
    role.hoist, role.mentionable = hoist, mentionable
    role.is_default.return_value = default
    role.__lt__ = lambda self, other: self.position < other.position
    role.edit = mock.AsyncMock()
    role.delete = mock.AsyncMock()
    return role


def fake_channel(cls, name, category=None, topic=None):
    """A channel whose permission overwrites behave like Discord's (stored per target)."""
    ch = mock.MagicMock(spec=cls)
    ch.id = next(_ids)
    ch.name, ch.topic, ch.category = name, topic, category
    ch.category_id = category.id if category else None
    ch.position = 0
    ch.type = "x"
    ch.overwrites = {}
    ch.channels = []
    if cls is discord.TextChannel:
        ch.is_news.return_value = False

    def overwrites_for(target):
        existing = ch.overwrites.get(target)
        return discord.PermissionOverwrite.from_pair(*existing.pair()) if existing else discord.PermissionOverwrite()

    async def set_permissions(target, *, overwrite, reason=None):
        if overwrite is None:
            ch.overwrites.pop(target, None)
        else:
            ch.overwrites[target] = overwrite

    ch.overwrites_for = overwrites_for
    ch.set_permissions = mock.AsyncMock(side_effect=set_permissions)
    ch.edit = mock.AsyncMock()
    ch.delete = mock.AsyncMock()
    ch.move = mock.AsyncMock()
    return ch


class FakeMe:
    id = 1

    def __init__(self, top_role):
        self.top_role = top_role


class FakeGuild:
    """Just enough of discord.Guild for apply_plan; records what gets created."""

    id = 42
    name = "Test Guild"
    owner_id = 999          # somebody else; set to 1 (the bot's id) to make the bot the owner

    def __init__(self, roles=(), categories=(), channels=(), bot_position=100):
        self.default_role = fake_role("@everyone", default=True, position=0)
        self.bot_role = fake_role("Luna", managed=True, position=bot_position)
        self.me = FakeMe(self.bot_role)
        self.roles = [self.default_role, *roles, self.bot_role]
        self.categories = list(categories)
        self.channels = list(channels) + list(categories)
        for ch in channels:
            if ch.category is not None and ch not in ch.category.channels:
                ch.category.channels.append(ch)
        self.rules_channel = self.public_updates_channel = self.system_channel = None
        self.safety_alerts_channel = self.afk_channel = None
        self.created = []
        self.fail = {}          # method name -> exception to raise
        self.moved = None
        self._next_pos = 1

    def _maybe_fail(self, method):
        exc = self.fail.get(method)
        if exc:
            raise exc

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, channel_id):
        return next((c for c in self.channels if c.id == channel_id), None)

    async def create_role(self, **kw):
        self._maybe_fail("create_role")
        role = fake_role(kw["name"], position=self._next_pos)
        self._next_pos += 1
        self.roles.append(role)
        self.created.append(("role", kw))
        return role

    async def fetch_roles(self):
        return list(self.roles)

    async def edit_role_positions(self, positions, reason=None):
        self.moved = dict(positions)

    async def create_category(self, name, **kw):
        self._maybe_fail("create_category")
        cat = fake_channel(discord.CategoryChannel, name)
        self.categories.append(cat)
        self.channels.append(cat)
        self.created.append(("category", name, kw))
        return cat

    async def _chan(self, kind, cls, name, **kw):
        self._maybe_fail(kind)
        ch = fake_channel(cls, name, kw.get("category"))
        self.channels.append(ch)
        self.created.append((kind, name, kw))
        return ch

    async def create_text_channel(self, name, **kw):
        if kw.get("news"):
            self._maybe_fail("news")
        return await self._chan("text", discord.TextChannel, name, **kw)

    async def create_voice_channel(self, name, **kw):
        return await self._chan("voice", discord.VoiceChannel, name, **kw)

    async def create_forum(self, name, **kw):
        return await self._chan("forum", discord.ForumChannel, name, **kw)


def sample_plan():
    return parse_plan({
        "summary": "s",
        "roles": [
            {"name": "Admin", "color": "#ff0000", "hoist": True, "mentionable": False, "permissions": ["manage_messages"]},
            {"name": "Member", "color": "#00ff00", "hoist": False, "mentionable": True, "permissions": []},
        ],
        "categories": [
            {"name": "Info", "private_to": [], "channels": [
                {"name": "rules", "kind": "text", "topic": "Read me", "read_only": True,
                 "writers": ["Admin"], "private_to": []},
            ]},
            {"name": "Staff", "private_to": ["Admin"], "channels": [
                {"name": "chat", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []},
                {"name": "Voice", "kind": "voice", "topic": "", "read_only": False, "writers": [], "private_to": []},
            ]},
        ],
    })


class ApplyPlanTests(unittest.TestCase):
    def test_dry_run_creates_nothing_and_reports_what_would_happen(self):
        guild = FakeGuild(roles=[fake_role("Member")])
        report = run(applier.apply_plan(guild, sample_plan(), dry_run=True))
        self.assertEqual(guild.created, [])
        self.assertEqual(report.roles_created, ["Admin"])
        self.assertEqual(report.roles_existing, ["Member"])
        self.assertEqual(report.categories_created, ["Info", "Staff"])
        self.assertEqual(len(report.channels_created), 3)
        # Staff is private to a role the dry run would create: that must not be flagged as missing.
        self.assertEqual(report.notes, [])

    def test_real_run_builds_everything_with_the_right_permissions(self):
        guild = FakeGuild()
        report = run(applier.apply_plan(guild, sample_plan()))
        self.assertIsNone(report.aborted)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.roles_created, ["Admin", "Member"])
        kinds = [c[0] for c in guild.created]
        self.assertEqual(kinds, ["role", "role", "category", "text", "category", "text", "voice"])

        by_name = {c[1]: c for c in guild.created if c[0] in ("category", "text", "voice")}
        admin_role = next(r for r in guild.roles if r.name == "Admin")

        rules_ow = by_name["rules"][2]["overwrites"]
        self.assertFalse(rules_ow[guild.default_role].send_messages)       # read-only for everyone
        self.assertTrue(rules_ow[admin_role].send_messages)                # writers can post
        self.assertTrue(rules_ow[guild.me].send_messages)                  # bot keeps access
        self.assertEqual(by_name["rules"][2]["topic"], "Read me")

        staff_ow = by_name["Staff"][2]["overwrites"]
        self.assertFalse(staff_ow[guild.default_role].view_channel)        # hidden from everyone
        self.assertTrue(staff_ow[admin_role].view_channel)                 # visible to Admin
        self.assertFalse(by_name["chat"][2]["overwrites"][guild.default_role].view_channel)
        self.assertFalse(by_name["Voice"][2]["overwrites"][guild.default_role].view_channel)
        self.assertNotIn("overwrites", by_name["Info"][2])                 # public category: none at all

    def test_new_roles_are_put_in_plan_order(self):
        guild = FakeGuild()
        run(applier.apply_plan(guild, sample_plan()))
        admin = next(r for r in guild.roles if r.name == "Admin")
        member = next(r for r in guild.roles if r.name == "Member")
        # Admin was created first so it sat lower; the fix swaps their slots.
        self.assertEqual(guild.moved, {admin: member.position, member: admin.position})

    def test_is_idempotent(self):
        guild = FakeGuild()
        run(applier.apply_plan(guild, sample_plan()))
        count = len(guild.created)
        again = run(applier.apply_plan(guild, sample_plan()))
        self.assertEqual(len(guild.created), count)
        self.assertTrue(again.nothing_to_do)

    def test_existing_channel_elsewhere_is_left_alone(self):
        other = fake_channel(discord.CategoryChannel, "Text Channels")
        rules = fake_channel(discord.TextChannel, "rules", other)
        guild = FakeGuild(categories=[other], channels=[rules])
        report = run(applier.apply_plan(guild, sample_plan()))
        self.assertIn("Info / rules", report.channels_existing)
        self.assertTrue(any("already exists" in n for n in report.notes))
        self.assertNotIn(("text", "rules"), [(c[0], c[1]) for c in guild.created])

    def test_unknown_role_reference_is_noted_and_hides_the_channel(self):
        plan = parse_plan({"roles": [], "categories": [{"name": "Staff", "private_to": ["Ghost"], "channels": [
            {"name": "chat", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []}]}]})
        guild = FakeGuild()
        report = run(applier.apply_plan(guild, plan))
        self.assertTrue(any("Ghost" in n for n in report.notes))
        chat = next(c for c in guild.created if c[0] == "text")
        self.assertFalse(chat[2]["overwrites"][guild.default_role].view_channel)

    def test_forbidden_aborts_cleanly_with_partial_results(self):
        guild = FakeGuild()
        guild.fail["create_category"] = forbidden()
        report = run(applier.apply_plan(guild, sample_plan()))
        self.assertIn("Manage", report.aborted)
        self.assertEqual(report.roles_created, ["Admin", "Member"])
        self.assertEqual(report.channels_created, [])

    def test_other_http_errors_are_recorded_and_the_run_continues(self):
        guild = FakeGuild()
        guild.fail["voice"] = http_error(500, "server error")
        report = run(applier.apply_plan(guild, sample_plan()))
        self.assertIsNone(report.aborted)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("Voice", report.errors[0])
        self.assertEqual(len(report.channels_created), 2)

    def test_announcement_falls_back_to_text_without_community(self):
        plan = parse_plan({"roles": [], "categories": [{"name": "News", "channels": [
            {"name": "news", "kind": "announcement", "topic": "", "read_only": False, "writers": [], "private_to": []}]}]})
        guild = FakeGuild()
        guild.fail["news"] = http_error(400, "Community required")
        report = run(applier.apply_plan(guild, plan))
        self.assertEqual(report.channels_created, ["News / news"])
        self.assertTrue(any("Community" in n for n in report.notes))
        self.assertFalse(guild.created[-1][2].get("news"))

    def test_progress_callback_is_called(self):
        seen = []

        async def progress(text):
            seen.append(text)

        run(applier.apply_plan(FakeGuild(), sample_plan(), progress=progress))
        self.assertTrue(any("Admin" in s for s in seen))
        self.assertTrue(any("rules" in s for s in seen))


# ── Snapshot ─────────────────────────────────────────────────────────────────
def server():
    """A small server: a mod role, locked roles, two categories and some channels."""
    mod = fake_role("Mod", position=10, colour=0x3498DB, hoist=True)
    boss = fake_role("Boss", position=20)
    above = fake_role("Owner", position=500)            # above the bot: locked
    booster = fake_role("Booster", managed=True, position=5)

    chat_cat = fake_channel(discord.CategoryChannel, "Chat")
    staff_cat = fake_channel(discord.CategoryChannel, "Staff")
    general = fake_channel(discord.TextChannel, "general", chat_cat, topic="Hello there")
    memes = fake_channel(discord.TextChannel, "memes", chat_cat)
    lounge = fake_channel(discord.VoiceChannel, "Lounge", chat_cat)
    rules = fake_channel(discord.TextChannel, "rules", staff_cat)
    loose = fake_channel(discord.TextChannel, "loose")

    guild = FakeGuild(roles=[mod, boss, above, booster], categories=[chat_cat, staff_cat],
                      channels=[general, memes, lounge, rules, loose])
    guild.rules_channel = rules
    return guild, SimpleBag(mod=mod, boss=boss, above=above, booster=booster, chat=chat_cat, staff=staff_cat,
                            general=general, memes=memes, lounge=lounge, rules=rules, loose=loose)


class SimpleBag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def ref_for(index, discord_id):
    return next(ref for ref, i in index.ids.items() if i == discord_id)


class SnapshotTests(unittest.TestCase):
    def test_locked_items_have_no_ref_and_are_listed_by_name(self):
        guild, s = server()
        snap, index = applier.snapshot_guild(guild)
        role_names = [r["name"] for r in snap["roles_most_senior_first"]]
        self.assertEqual(role_names, ["Boss", "Mod"])                      # senior first
        self.assertEqual(sorted(snap["locked_roles_cannot_be_changed"]), ["Booster", "Luna", "Owner"])
        self.assertEqual(snap["locked_channels_cannot_be_changed"], ["rules"])   # Community rules channel
        all_names = {index.names[ref] for ref in index.ids}
        self.assertFalse({"Owner", "Booster", "Luna", "rules", "@everyone"} & all_names)

    def test_refs_describe_structure_and_kinds(self):
        guild, s = server()
        snap, index = applier.snapshot_guild(guild)
        chat = next(c for c in snap["categories"] if c["name"] == "Chat")
        self.assertEqual([c["name"] for c in chat["channels"]], ["general", "memes", "Lounge"])
        self.assertEqual(chat["channels"][0]["topic"], "Hello there")
        self.assertEqual([c["name"] for c in snap["channels_without_category"]], ["loose"])
        self.assertEqual(index.kinds[ref_for(index, s.lounge.id)], "voice")
        self.assertEqual(index.kinds[ref_for(index, s.chat.id)], "category")
        self.assertEqual(index.kinds[ref_for(index, s.mod.id)], "role")
        self.assertEqual(index.kinds[ref_for(index, s.general.id)], "text")
        mod = next(r for r in snap["roles_most_senior_first"] if r["name"] == "Mod")
        self.assertEqual((mod["color"], mod["hoist"]), ("#3498db", True))

    def test_private_flag_reflects_the_everyone_overwrite(self):
        guild, s = server()
        s.memes.overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        snap, _ = applier.snapshot_guild(guild)
        chat = next(c for c in snap["categories"] if c["name"] == "Chat")
        flags = {c["name"]: c["private"] for c in chat["channels"]}
        self.assertEqual(flags, {"general": False, "memes": True, "Lounge": False})

    def test_snapshot_is_bounded(self):
        many = [fake_channel(discord.TextChannel, f"c{i}") for i in range(applier._SNAPSHOT_CHANNEL_LIMIT + 50)]
        guild = FakeGuild(channels=many)
        snap, index = applier.snapshot_guild(guild)
        self.assertEqual(len(snap["channels_without_category"]), applier._SNAPSHOT_CHANNEL_LIMIT)
        self.assertEqual(sum(1 for k in index.ids if k.startswith("ch:")), applier._SNAPSHOT_CHANNEL_LIMIT)


# ── Editing / ordering / deleting existing items ─────────────────────────────
def plan_for(guild, data):
    snap, index = applier.snapshot_guild(guild)
    return parse_plan(data, index.kinds), index


def role_edit(ref, **kw):
    base = {"ref": ref, "new_name": "", "color": "", "hoist": "keep", "mentionable": "keep",
            "permissions_mode": "keep", "permissions": []}
    return {**base, **kw}


def channel_edit(ref, **kw):
    base = {"ref": ref, "new_name": "", "topic": "", "move_to": "", "visibility": "keep",
            "private_to": [], "read_only": "keep", "writers": []}
    return {**base, **kw}


def apply(guild, plan, index, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        return run(applier.apply_plan(guild, plan, index=index, backup_dir=Path(tmp), **kw))


class EditTests(unittest.TestCase):
    def setUp(self):
        self.guild, self.s = server()
        _, self.index = applier.snapshot_guild(self.guild)
        self.ref = lambda obj: ref_for(self.index, obj.id)

    def plan(self, **data):
        return parse_plan(data, self.index.kinds)

    def test_role_edit_changes_only_what_differs(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="Moderator", color="#3498db",
                                               hoist="yes", mentionable="yes")])
        report = apply(self.guild, plan, self.index)
        kwargs = self.s.mod.edit.await_args.kwargs
        self.assertEqual(kwargs["name"], "Moderator")
        self.assertEqual(kwargs["mentionable"], True)
        self.assertNotIn("colour", kwargs)            # already that colour
        self.assertNotIn("hoist", kwargs)             # already hoisted
        self.assertIn("Luna.AI", kwargs["reason"])
        self.assertEqual(report.edited, ["role Mod"])

    def test_role_permissions_are_replaced_when_asked(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), permissions_mode="replace",
                                               permissions=["view_channel", "manage_messages"])])
        apply(self.guild, plan, self.index)
        self.assertEqual(self.s.mod.edit.await_args.kwargs["permissions"],
                         discord.Permissions(view_channel=True, manage_messages=True))

    def test_no_op_edit_is_not_reported(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="Mod")])
        report = apply(self.guild, plan, self.index)
        self.s.mod.edit.assert_not_awaited()
        self.assertEqual(report.edited, [])

    def test_role_that_became_locked_is_refused(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="X")])
        self.s.mod.position = 999                     # promoted above the bot after the snapshot
        report = apply(self.guild, plan, self.index)
        self.s.mod.edit.assert_not_awaited()
        self.assertTrue(any("above my highest" in e for e in report.errors))

    def test_missing_item_is_noted_not_fatal(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="X")])
        self.guild.roles.remove(self.s.mod)
        report = apply(self.guild, plan, self.index)
        self.assertTrue(any("no longer exists" in n for n in report.notes))
        self.assertIsNone(report.aborted)

    def test_forbidden_on_one_edit_does_not_abort_the_rest(self):
        self.s.mod.edit.side_effect = forbidden()
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="X"),
                                     role_edit(self.ref(self.s.boss), new_name="Y")])
        report = apply(self.guild, plan, self.index)
        self.assertIsNone(report.aborted)
        self.assertEqual(len(report.errors), 1)
        self.s.boss.edit.assert_awaited_once()
        self.assertEqual(report.edited, ["role Boss"])

    def test_category_and_channel_rename_topic_and_move(self):
        plan = self.plan(
            edit_categories=[{"ref": self.ref(self.s.chat), "new_name": "Lounge Area"}],
            edit_channels=[channel_edit(self.ref(self.s.memes), new_name="fun", topic="Jokes",
                                        move_to=self.ref(self.s.staff))])
        report = apply(self.guild, plan, self.index)
        self.s.chat.edit.assert_awaited_once()
        self.assertEqual(self.s.chat.edit.await_args.kwargs["name"], "Lounge Area")
        kwargs = self.s.memes.edit.await_args.kwargs
        self.assertEqual((kwargs["name"], kwargs["topic"], kwargs["category"]), ("fun", "Jokes", self.s.staff))
        self.assertEqual(sorted(report.edited), ["category Chat", "channel memes"])

    def test_move_to_none_and_move_into_a_category_created_by_the_plan(self):
        plan = self.plan(
            categories=[{"name": "Fresh", "private_to": [], "channels": [
                {"name": "new-one", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []}]}],
            edit_channels=[channel_edit(self.ref(self.s.general), move_to="none"),
                           channel_edit(self.ref(self.s.memes), move_to="Fresh")])
        apply(self.guild, plan, self.index)
        self.assertIsNone(self.s.general.edit.await_args.kwargs["category"])
        fresh = next(c for c in self.guild.categories if c.name == "Fresh")
        self.assertIs(self.s.memes.edit.await_args.kwargs["category"], fresh)

    def test_voice_channels_never_get_a_topic(self):
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.lounge), new_name="Hangout", topic="nope")])
        apply(self.guild, plan, self.index)
        self.assertNotIn("topic", self.s.lounge.edit.await_args.kwargs)

    def test_making_a_channel_private_merges_into_existing_overwrites(self):
        everyone, mod = self.guild.default_role, self.s.mod
        self.s.memes.overwrites[everyone] = discord.PermissionOverwrite(add_reactions=False)   # must survive
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.memes), visibility="private", private_to=["Mod"])])
        apply(self.guild, plan, self.index)
        ow = self.s.memes.overwrites
        self.assertIs(ow[everyone].view_channel, False)
        self.assertIs(ow[everyone].add_reactions, False)         # the pre-existing deny is still there
        self.assertIs(ow[mod].view_channel, True)
        self.assertIs(ow[self.guild.me].view_channel, True)      # the bot keeps access

    def test_private_and_read_only_write_each_target_once(self):
        # Two merges into the same target in one run would read a stale cache and undo each other.
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.memes), visibility="private", private_to=["Mod"],
                                                     read_only="yes", writers=["Boss"])])
        apply(self.guild, plan, self.index)
        targets = [c.args[0] for c in self.s.memes.set_permissions.await_args_list]
        self.assertEqual(len(targets), len(set(targets)))
        everyone_ow = self.s.memes.overwrites[self.guild.default_role]
        self.assertIs(everyone_ow.view_channel, False)
        self.assertIs(everyone_ow.send_messages, False)
        self.assertIs(self.s.memes.overwrites[self.s.boss].send_messages, True)

    def test_making_a_channel_public_clears_only_the_view_deny(self):
        everyone = self.guild.default_role
        self.s.memes.overwrites[everyone] = discord.PermissionOverwrite(view_channel=False, add_reactions=False)
        self.s.general.overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.memes), visibility="public"),
                                        channel_edit(self.ref(self.s.general), visibility="public")])
        apply(self.guild, plan, self.index)
        self.assertIsNone(self.s.memes.overwrites[everyone].view_channel)
        self.assertIs(self.s.memes.overwrites[everyone].add_reactions, False)
        self.assertNotIn(everyone, self.s.general.overwrites)    # became empty, so it was removed

    def test_public_when_nothing_is_set_makes_no_call(self):
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.memes), visibility="public")])
        report = apply(self.guild, plan, self.index)
        self.s.memes.set_permissions.assert_not_awaited()
        self.assertEqual(report.edited, [])

    def test_protected_channel_cannot_be_edited_even_if_the_ref_were_forced(self):
        self.guild.rules_channel = self.s.general          # became protected after the snapshot
        plan = self.plan(edit_channels=[channel_edit(self.ref(self.s.general), new_name="x")])
        report = apply(self.guild, plan, self.index)
        self.s.general.edit.assert_not_awaited()
        self.assertTrue(any("Community" in e for e in report.errors))

    def test_reorder_roles_permutes_only_their_own_slots(self):
        plan = self.plan(role_order=[self.ref(self.s.mod), self.ref(self.s.boss)])   # Mod above Boss
        report = apply(self.guild, plan, self.index)
        self.assertEqual(self.guild.moved, {self.s.mod: 20, self.s.boss: 10})
        self.assertEqual(report.reordered, ["roles"])

    def test_reorder_categories_chains_move_after(self):
        plan = self.plan(category_order=[self.ref(self.s.staff), self.ref(self.s.chat)])
        report = apply(self.guild, plan, self.index)
        self.s.chat.move.assert_awaited_once()
        self.assertIs(self.s.chat.move.await_args.kwargs["after"], self.s.staff)
        self.assertEqual(report.reordered, ["categories"])

    def test_dry_run_changes_nothing(self):
        plan = self.plan(edit_roles=[role_edit(self.ref(self.s.mod), new_name="X")],
                         edit_channels=[channel_edit(self.ref(self.s.memes), visibility="private")],
                         role_order=[self.ref(self.s.mod), self.ref(self.s.boss)],
                         delete=[self.ref(self.s.loose)])
        report = apply(self.guild, plan, self.index, dry_run=True)
        for obj in (self.s.mod, self.s.memes, self.s.loose):
            obj.edit.assert_not_awaited()
            obj.delete.assert_not_awaited()
        self.s.memes.set_permissions.assert_not_awaited()
        self.assertIsNone(self.guild.moved)
        self.assertEqual(sorted(report.edited), ["channel memes", "role Mod"])
        self.assertEqual(report.deleted, ["channel loose"])
        self.assertIsNone(report.backup_path)


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.guild, self.s = server()
        _, self.index = applier.snapshot_guild(self.guild)
        self.ref = lambda obj: ref_for(self.index, obj.id)
        self.order = []
        for name in ("memes", "loose", "chat", "mod"):
            obj = getattr(self.s, name)
            obj.delete.side_effect = (lambda reason=None, n=name: self.order.append(n))   # records, returns None

    def plan(self, *objs):
        return parse_plan({"delete": [self.ref(o) for o in objs]}, self.index.kinds)

    def test_deletes_channels_then_categories_then_roles(self):
        plan = self.plan(self.s.mod, self.s.chat, self.s.memes, self.s.loose)
        report = apply(self.guild, plan, self.index)
        self.assertEqual(self.order.index("mod"), len(self.order) - 1)           # role last
        self.assertLess(max(self.order.index("memes"), self.order.index("loose")), self.order.index("chat"))
        self.assertEqual(sorted(report.deleted), ["category Chat", "channel loose", "channel memes", "role Mod"])
        self.assertIn("Luna.AI", self.s.memes.delete.await_args.kwargs["reason"])

    def test_protected_channel_is_never_deleted(self):
        plan = self.plan(self.s.memes)
        self.guild.rules_channel = self.s.memes            # became a Community channel after the plan
        report = apply(self.guild, plan, self.index)
        self.s.memes.delete.assert_not_awaited()
        self.assertTrue(any("Community" in e for e in report.errors))
        self.assertEqual(report.deleted, [])

    def test_role_at_or_above_the_bot_is_never_deleted(self):
        plan = self.plan(self.s.mod)
        self.s.mod.position = 100                          # same height as the bot
        report = apply(self.guild, plan, self.index)
        self.s.mod.delete.assert_not_awaited()
        self.assertTrue(any("above my highest" in e for e in report.errors))

    def test_the_owner_ignores_the_role_hierarchy(self):
        self.guild.owner_id = self.guild.me.id             # the bot owns the server
        plan = self.plan(self.s.mod)
        self.s.mod.position = 999                           # far above the bot's own role
        report = apply(self.guild, plan, self.index)
        self.s.mod.delete.assert_awaited_once()
        self.assertEqual(report.errors, [])

    def test_something_already_gone_is_a_note(self):
        plan = self.plan(self.s.memes)
        self.guild.channels.remove(self.s.memes)
        report = apply(self.guild, plan, self.index)
        self.assertTrue(any("already gone" in n for n in report.notes))
        self.assertIsNone(report.aborted)

    def test_deleting_a_category_warns_about_the_channels_that_stay(self):
        report = apply(self.guild, self.plan(self.s.chat), self.index, dry_run=True)
        self.assertTrue(any("3 channel(s) stay" in n for n in report.notes))
        both = self.plan(self.s.chat, self.s.general, self.s.memes, self.s.lounge)
        report = apply(self.guild, both, self.index, dry_run=True)
        self.assertFalse(any("stay" in n for n in report.notes))

    def test_forbidden_on_one_deletion_does_not_stop_the_others(self):
        self.s.memes.delete.side_effect = forbidden()
        report = apply(self.guild, self.plan(self.s.memes, self.s.loose), self.index)
        self.assertIsNone(report.aborted)
        self.assertEqual(report.deleted, ["channel loose"])
        self.assertEqual(len(report.errors), 1)

    def test_creation_happens_before_deletion(self):
        plan = parse_plan({"roles": [{"name": "Fresh"}], "delete": [self.ref(self.s.loose)]}, self.index.kinds)
        events = []
        self.s.loose.delete.side_effect = lambda reason=None: events.append("delete")
        original = self.guild.create_role

        async def spy(**kw):
            events.append("create")
            return await original(**kw)

        self.guild.create_role = spy
        apply(self.guild, plan, self.index)
        self.assertEqual(events, ["create", "delete"])


class BackupTests(unittest.TestCase):
    def test_backup_records_structure_and_overwrites(self):
        guild, s = server()
        s.memes.overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = applier.write_backup(guild, Path(tmp))
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["guild"]["id"], guild.id)
        self.assertIn("Mod", [r["name"] for r in data["roles"]])
        memes = next(c for c in data["channels"] if c["name"] == "memes")
        self.assertEqual(memes["category_id"], s.chat.id)
        self.assertEqual(len(memes["overwrites"]), 1)
        self.assertTrue(memes["overwrites"][0]["target_is_role"])
        self.assertGreater(memes["overwrites"][0]["deny"], 0)

    def test_backup_is_written_before_changes_but_not_for_dry_runs_or_plain_creation(self):
        guild, s = server()
        _, index = applier.snapshot_guild(guild)
        delete = parse_plan({"delete": [ref_for(index, s.loose.id)]}, index.kinds)
        with tempfile.TemporaryDirectory() as tmp:
            report = run(applier.apply_plan(guild, delete, index=index, backup_dir=Path(tmp)))
            self.assertTrue(Path(report.backup_path).exists())
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)
            run(applier.apply_plan(guild, delete, index=index, backup_dir=Path(tmp), dry_run=True))
            run(applier.apply_plan(guild, sample_plan(), backup_dir=Path(tmp)))
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)

    def test_if_the_backup_fails_nothing_is_changed(self):
        guild, s = server()
        _, index = applier.snapshot_guild(guild)
        plan = parse_plan({"delete": [ref_for(index, s.loose.id)]}, index.kinds)
        with tempfile.NamedTemporaryFile() as blocker:          # a file where the folder should be
            report = run(applier.apply_plan(guild, plan, index=index, backup_dir=Path(blocker.name) / "sub"))
        self.assertIn("backup", report.aborted)
        s.loose.delete.assert_not_awaited()


class HelperTests(unittest.TestCase):
    def test_build_permissions(self):
        perms = applier.build_permissions(["manage_messages", "view_channel"])
        self.assertTrue(perms.manage_messages and perms.view_channel)
        self.assertFalse(perms.administrator)

    def test_chunk_lines_respects_limit(self):
        chunks = applier.chunk_lines([f"line {i}" * 20 for i in range(40)], limit=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual(sum(c.count("\n") + 1 for c in chunks), 40)

    def test_change_lines_and_plan_text_name_every_change(self):
        guild, s = server()
        _, index = applier.snapshot_guild(guild)
        ref = lambda obj: ref_for(index, obj.id)           # noqa: E731
        plan = parse_plan({
            "edit_roles": [role_edit(ref(s.mod), new_name="Moderator", permissions_mode="replace", permissions=["view_channel"])],
            "edit_channels": [channel_edit(ref(s.memes), new_name="fun", move_to="none", visibility="private")],
            "role_order": [ref(s.mod), ref(s.boss)],
            "delete": [ref(s.loose), ref(s.chat), ref(s.lounge)],
        }, index.kinds)
        edits, deletes = applier.change_lines(plan, index)
        text = "\n".join(edits)
        for fragment in ("Mod", "Moderator", "permissions replaced", "memes", "fun", "remove from category", "private", "reorder roles"):
            self.assertIn(fragment, text)
        self.assertEqual(deletes, ["🗑️ channel **loose**", "🗑️ category **Chat**", "🗑️ channel **Lounge**"])
        full = applier.full_plan_text(plan, applier.ApplyReport(dry_run=True), index)
        self.assertIn("WILL BE DELETED", full)
        self.assertNotIn("**", full)

    def test_result_lines_mention_edits_deletes_and_backup(self):
        report = applier.ApplyReport(edited=["role A"], reordered=["roles"], deleted=["channel x"], backup_path="b.json")
        text = "\n".join(applier.result_lines(report))
        self.assertIn("Changed **1** existing", text)
        self.assertIn("Deleted **1**", text)
        self.assertIn("b.json", text)


if __name__ == "__main__":
    unittest.main()
