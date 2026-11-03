import asyncio
import unittest
from unittest import mock

import discord

from luna import applier
from luna.plan import parse_plan


def run(coro):
    return asyncio.run(coro)


def fake_role(name, *, default=False, managed=False, position=1):
    role = mock.MagicMock(spec=discord.Role)
    role.name = name
    role.id = abs(hash(name)) % 10**9
    role.position = position
    role.managed = managed
    role.is_default.return_value = default
    return role


def fake_channel(cls, name, category=None):
    ch = mock.MagicMock(spec=cls)
    ch.name = name
    ch.category = category
    return ch


def http_error(status, text="boom"):
    response = mock.Mock(status=status, reason="x")
    return discord.HTTPException(response, text)


class FakeGuild:
    """Just enough of discord.Guild for apply_plan; records what gets created."""

    def __init__(self, roles=(), categories=(), channels=()):
        self.default_role = fake_role("@everyone", default=True, position=0)
        self.me = object()
        self.roles = [self.default_role, *roles]
        self.categories = list(categories)
        self.channels = list(channels) + list(categories)
        self.created = []
        self.fail = {}          # method name -> exception to raise
        self.moved = None
        self._next_pos = 1

    def _maybe_fail(self, method):
        exc = self.fail.get(method)
        if exc:
            raise exc

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
        # Channels inherit the category's privacy.
        self.assertFalse(by_name["chat"][2]["overwrites"][guild.default_role].view_channel)
        self.assertFalse(by_name["Voice"][2]["overwrites"][guild.default_role].view_channel)
        # Public category has no overwrites at all.
        self.assertNotIn("overwrites", by_name["Info"][2])

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
        guild.fail["create_category"] = discord.Forbidden(mock.Mock(status=403, reason="x"), "Missing Permissions")
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


class HelperTests(unittest.TestCase):
    def test_build_permissions(self):
        perms = applier.build_permissions(["manage_messages", "view_channel"])
        self.assertTrue(perms.manage_messages and perms.view_channel)
        self.assertFalse(perms.administrator)

    def test_chunk_lines_respects_limit(self):
        chunks = applier.chunk_lines([f"line {i}" * 20 for i in range(40)], limit=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual(sum(c.count("\n") + 1 for c in chunks), 40)

    def test_snapshot_skips_default_and_managed_roles(self):
        guild = mock.MagicMock()
        guild.roles = [fake_role("@everyone", default=True), fake_role("Bot", managed=True), fake_role("Mod")]
        guild.categories = []
        guild.channels = []
        self.assertEqual(applier.snapshot_guild(guild)["roles_most_senior_first"], ["Mod"])


if __name__ == "__main__":
    unittest.main()
