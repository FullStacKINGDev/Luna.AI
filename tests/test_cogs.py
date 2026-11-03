import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import reaction_roles as rr
from cogs import server_setup


def run(coro):
    return asyncio.run(coro)


def role(name, *, perms=0, position=1, managed=False, default=False, rid=1):
    r = mock.MagicMock(spec=discord.Role)
    r.name, r.id, r.position, r.managed = name, rid, position, managed
    r.permissions = discord.Permissions(perms)
    r.is_default.return_value = default
    # Role comparison is by position.
    r.__ge__ = lambda self, other: self.position >= other.position
    return r


class StarterTemplateTests(unittest.TestCase):
    def test_template_is_valid_and_produces_no_warnings(self):
        plan = server_setup.starter_plan(roles=True, channels=True)
        self.assertEqual(plan.warnings, [])
        self.assertEqual(len(plan.roles), 5)
        self.assertEqual(len(plan.categories), 5)

    def test_split_commands_only_include_their_half(self):
        self.assertEqual(server_setup.starter_plan(roles=True, channels=False).categories, [])
        self.assertEqual(server_setup.starter_plan(roles=False, channels=True).roles, [])

    def test_admin_category_is_private_to_staff(self):
        admin = next(c for c in server_setup.starter_plan(roles=True, channels=True).categories if "Admin" in c.name)
        self.assertEqual(set(admin.private_to), set(server_setup.STAFF))


class ReactionRoleParsingTests(unittest.TestCase):
    def test_parse_mappings(self):
        self.assertEqual(rr.parse_mappings("🎮=Gamer, 🎵 = Music Lovers;📢->News\n❤️→Fans"),
                         [("🎮", "Gamer"), ("🎵", "Music Lovers"), ("📢", "News"), ("❤️", "Fans")])

    def test_parse_mappings_rejects_bad_input(self):
        for bad in ("", "   ", "🎮", "🎮=", "=Gamer"):
            with self.assertRaises(rr.MappingError):
                rr.parse_mappings(bad)
        with self.assertRaises(rr.MappingError):
            rr.parse_mappings(",".join(f"🎮{i}=R{i}" for i in range(rr.MAX_ROLES_PER_MESSAGE + 1)))

    def test_parse_emoji(self):
        self.assertEqual(str(rr.parse_emoji("🎮")), "🎮")
        custom = rr.parse_emoji("<:party:123456789012345678>")
        self.assertEqual(custom.id, 123456789012345678)
        self.assertTrue(rr.parse_emoji("<a:dance:123456789012345678>").animated)
        for bad in ("gamer", "<:party:123>"):   # plain words and malformed custom emoji
            with self.assertRaises(rr.MappingError):
                rr.parse_emoji(bad)

    def test_dangerous_permission_names_are_valid(self):
        # Would raise TypeError if discord.py renamed any of these flags.
        self.assertTrue(rr.DANGEROUS_PERMISSIONS.administrator)
        self.assertTrue(rr.DANGEROUS_PERMISSIONS.manage_expressions)

    def test_check_assignable(self):
        guild = SimpleNamespace(me=SimpleNamespace(top_role=role("Luna", position=10)))
        self.assertIsNone(rr.check_assignable(guild, role("Gamer", position=3)))
        self.assertIn("above my highest", rr.check_assignable(guild, role("Boss", position=11)))
        self.assertIn("powerful", rr.check_assignable(guild, role("Mod", perms=discord.Permissions(kick_members=True).value)))
        self.assertIn("powerful", rr.check_assignable(guild, role("Adm", perms=discord.Permissions(administrator=True).value)))
        self.assertIn("managed", rr.check_assignable(guild, role("Bot", managed=True)))
        self.assertIn("@everyone", rr.check_assignable(guild, role("@everyone", default=True)))

    def test_find_role_by_name_and_mention(self):
        gamer = role("Gamer", rid=42)
        guild = SimpleNamespace(roles=[gamer], get_role=lambda i: gamer if i == 42 else None)
        self.assertIs(rr.find_role(guild, "gamer"), gamer)
        self.assertIs(rr.find_role(guild, "@Gamer"), gamer)
        self.assertIs(rr.find_role(guild, "<@&42>"), gamer)
        self.assertIsNone(rr.find_role(guild, "nobody"))


class ReactionRoleStorageTests(unittest.TestCase):
    def test_roundtrip_is_atomic_and_unicode_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rr.json"
            data = {"1": {"guild_id": 2, "channel_id": 3, "roles": {"🎮": 4}}}
            rr.save_data(data, path)
            self.assertEqual(rr.load_data(path), data)
            self.assertFalse(Path(str(path) + ".tmp").exists())
            self.assertIn("🎮", path.read_text(encoding="utf-8"))

    def test_missing_or_corrupt_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rr.json"
            self.assertEqual(rr.load_data(path), {})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(rr.load_data(path), {})


class ReactionListenerTests(unittest.TestCase):
    def setUp(self):
        self.gamer = role("Gamer", position=2, rid=7)
        self.guild = mock.MagicMock()
        self.guild.me.top_role = role("Luna", position=10)
        self.guild.get_role = lambda i: self.gamer if i == 7 else None
        self.bot = mock.MagicMock()
        self.bot.user.id = 999
        self.bot.get_guild.return_value = self.guild
        with mock.patch.object(rr, "load_data", return_value={"55": {"guild_id": 1, "channel_id": 2, "roles": {"🎮": 7}}}):
            self.cog = rr.ReactionRoles(self.bot)

    def payload(self, **kw):
        base = dict(message_id=55, guild_id=1, user_id=5, emoji="🎮", member=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_adds_role_using_payload_member(self):
        member = mock.MagicMock(bot=False)
        member.add_roles = mock.AsyncMock()
        run(self.cog.on_raw_reaction_add(self.payload(member=member)))
        member.add_roles.assert_awaited_once()
        self.assertIs(member.add_roles.await_args.args[0], self.gamer)

    def test_remove_fetches_member_when_not_cached(self):
        member = mock.MagicMock(bot=False)
        member.remove_roles = mock.AsyncMock()
        self.guild.get_member.return_value = None
        self.guild.fetch_member = mock.AsyncMock(return_value=member)
        run(self.cog.on_raw_reaction_remove(self.payload()))
        member.remove_roles.assert_awaited_once()

    def test_ignores_other_messages_the_bot_itself_and_other_bots(self):
        member = mock.MagicMock(bot=False)
        member.add_roles = mock.AsyncMock()
        run(self.cog.on_raw_reaction_add(self.payload(message_id=1, member=member)))
        run(self.cog.on_raw_reaction_add(self.payload(user_id=999, member=member)))
        run(self.cog.on_raw_reaction_add(self.payload(emoji="🎵", member=member)))
        bot_member = mock.MagicMock(bot=True)
        bot_member.add_roles = mock.AsyncMock()
        run(self.cog.on_raw_reaction_add(self.payload(member=bot_member)))
        member.add_roles.assert_not_awaited()
        bot_member.add_roles.assert_not_awaited()

    def test_refuses_a_role_that_became_dangerous(self):
        self.gamer.permissions = discord.Permissions(administrator=True)
        member = mock.MagicMock(bot=False)
        member.add_roles = mock.AsyncMock()
        run(self.cog.on_raw_reaction_add(self.payload(member=member)))
        member.add_roles.assert_not_awaited()

    def test_http_errors_are_swallowed_not_raised(self):
        member = mock.MagicMock(bot=False)
        member.add_roles = mock.AsyncMock(side_effect=discord.HTTPException(mock.Mock(status=500, reason="x"), "boom"))
        run(self.cog.on_raw_reaction_add(self.payload(member=member)))  # must not raise

    def test_deleting_the_channel_forgets_its_messages(self):
        with mock.patch.object(rr, "save_data") as save:
            run(self.cog.on_guild_channel_delete(SimpleNamespace(id=999)))     # some other channel
            save.assert_not_called()
            self.assertIn("55", self.cog.reaction_roles)
            run(self.cog.on_guild_channel_delete(SimpleNamespace(id=2)))       # the panel's channel
            save.assert_called_once()
        self.assertNotIn("55", self.cog.reaction_roles)

    def test_deleting_the_message_forgets_it(self):
        with mock.patch.object(rr, "save_data") as save:
            run(self.cog.on_raw_message_delete(SimpleNamespace(message_id=55)))
            save.assert_called_once()
        self.assertNotIn("55", self.cog.reaction_roles)


if __name__ == "__main__":
    unittest.main()
