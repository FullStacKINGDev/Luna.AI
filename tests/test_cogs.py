import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import discord
import httpx

from cogs import reaction_roles as rr
from cogs import server_setup
from luna import planner


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

    def test_deleting_the_message_forgets_it(self):
        with mock.patch.object(rr, "save_data") as save:
            run(self.cog.on_raw_message_delete(SimpleNamespace(message_id=55)))
            save.assert_called_once()
        self.assertNotIn("55", self.cog.reaction_roles)


# ── Planner (Claude call is mocked) ──────────────────────────────────────────
GOOD_JSON = json.dumps({
    "summary": "Study group",
    "roles": [{"name": "Mentor", "color": "#123456", "hoist": True, "mentionable": False, "permissions": []}],
    "categories": [{"name": "Hub", "private_to": [], "channels": [
        {"name": "chat", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []}]}],
})


def reply(text=GOOD_JSON, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def api_error(cls, status, message):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=request), body=None)


def patched_client(beta_create=None, create=None):
    client = mock.MagicMock()
    client.beta.messages.create = beta_create or mock.AsyncMock(return_value=reply())
    client.messages.create = create or mock.AsyncMock(return_value=reply())
    return mock.patch.object(planner.anthropic, "AsyncAnthropic", return_value=client), client


class PlannerTests(unittest.TestCase):
    def test_success_requests_structured_output_with_fallbacks(self):
        patcher, client = patched_client()
        with patcher:
            plan = run(planner.generate_plan("a study server for students", {"roles_most_senior_first": []}))
        self.assertEqual(plan.roles[0].name, "Mentor")
        kwargs = client.beta.messages.create.await_args.kwargs
        self.assertEqual(kwargs["fallbacks"], "default")
        self.assertEqual(kwargs["betas"], [planner.FALLBACK_BETA])
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertIn("a study server for students", kwargs["messages"][0]["content"])
        self.assertNotIn("thinking", kwargs)          # thinking is always on for this model

    def test_model_can_be_overridden(self):
        patcher, client = patched_client()
        with patcher, mock.patch.dict("os.environ", {"LUNA_MODEL": "claude-sonnet-5-5"}):
            run(planner.generate_plan("x" * 20, {}))
        self.assertEqual(client.beta.messages.create.await_args.kwargs["model"], "claude-sonnet-5-5")

    def test_retries_without_fallbacks_if_the_beta_is_rejected(self):
        err = api_error(anthropic.BadRequestError, 400, "unknown parameter: fallbacks")
        patcher, client = patched_client(beta_create=mock.AsyncMock(side_effect=err))
        with patcher:
            plan = run(planner.generate_plan("x" * 20, {}))
        self.assertEqual(plan.roles[0].name, "Mentor")
        client.messages.create.assert_awaited_once()
        self.assertNotIn("fallbacks", client.messages.create.await_args.kwargs)

    def test_other_bad_requests_are_not_retried(self):
        err = api_error(anthropic.BadRequestError, 400, "schema invalid")
        patcher, client = patched_client(beta_create=mock.AsyncMock(side_effect=err))
        with patcher, self.assertRaises(planner.PlannerError):
            run(planner.generate_plan("x" * 20, {}))
        client.messages.create.assert_not_awaited()

    def test_friendly_messages_for_common_failures(self):
        cases = [
            (api_error(anthropic.AuthenticationError, 401, "bad key"), "API key"),
            (api_error(anthropic.RateLimitError, 429, "slow"), "rate limited"),
            (api_error(anthropic.NotFoundError, 404, "no model"), "not found"),
            (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "reach"),
            (TypeError("Could not resolve authentication method. Expected api_key"), "ANTHROPIC_API_KEY"),
        ]
        for exc, fragment in cases:
            patcher, _ = patched_client(beta_create=mock.AsyncMock(side_effect=exc))
            with patcher, self.assertRaises(planner.PlannerError) as ctx:
                run(planner.generate_plan("x" * 20, {}))
            self.assertIn(fragment, str(ctx.exception), exc)

    def test_refusal_truncation_and_garbage(self):
        for response, fragment in [
            (reply("", stop="refusal"), "declined"),
            (reply(GOOD_JSON[:20], stop="max_tokens"), "too large"),
            (reply("not json"), "couldn't use"),
            (reply('{"summary": "", "roles": [], "categories": []}'), "couldn't use"),
        ]:
            patcher, _ = patched_client(beta_create=mock.AsyncMock(return_value=response))
            with patcher, self.assertRaises(planner.PlannerError) as ctx:
                run(planner.generate_plan("x" * 20, {}))
            self.assertIn(fragment, str(ctx.exception))

    def test_user_message_wraps_request_and_snapshot(self):
        text = planner.build_user_message("make it cozy", {"roles_most_senior_first": ["Boss"]})
        self.assertIn("<request>\nmake it cozy\n</request>", text)
        self.assertIn("Boss", text)


if __name__ == "__main__":
    unittest.main()
