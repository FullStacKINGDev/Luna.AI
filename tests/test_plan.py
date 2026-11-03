import unittest

import discord

from luna import plan as P


def make(**overrides):
    data = {
        "summary": "  A   study server ",
        "roles": [
            {"name": "Mentor", "color": "#3498db", "hoist": True, "mentionable": True,
             "permissions": ["manage_messages", "administrator", "ban_members", "manage_messages"]},
            {"name": "Student", "color": "nonsense", "hoist": False, "mentionable": False, "permissions": []},
        ],
        "categories": [
            {"name": "Info", "private_to": [], "channels": [
                {"name": "Welcome Here!!", "kind": "text", "topic": "hi", "read_only": True,
                 "writers": ["Mentor", "@everyone"], "private_to": []},
                {"name": "Lounge", "kind": "voice", "topic": "ignored", "read_only": True,
                 "writers": [], "private_to": []},
            ]},
        ],
    }
    data.update(overrides)
    return data


class ParsePlanTests(unittest.TestCase):
    def test_happy_path_normalises_everything(self):
        plan = P.parse_plan(make())
        self.assertEqual(plan.summary, "A study server")
        mentor, student = plan.roles
        self.assertEqual(mentor.color, 0x3498DB)
        self.assertEqual(student.color, P.DEFAULT_ROLE_COLOR)       # bad colour falls back
        self.assertEqual(mentor.permissions, ("manage_messages",))   # unsafe + duplicate removed
        text, voice = plan.categories[0].channels
        self.assertEqual(text.name, "welcome-here!!")                # slugged
        self.assertEqual(text.writers, ("Mentor",))                  # @everyone dropped
        self.assertEqual(voice.name, "Lounge")                       # voice keeps case
        self.assertFalse(voice.read_only)                            # read-only is meaningless for voice
        self.assertEqual(voice.topic, "")

    def test_unsafe_permissions_are_reported(self):
        plan = P.parse_plan(make())
        joined = " ".join(plan.warnings)
        self.assertIn("administrator", joined)
        self.assertIn("ban_members", joined)

    def test_empty_or_garbage_raises(self):
        for bad in (None, [], "x", {}, {"roles": [], "categories": []},
                    {"roles": "nope", "categories": 5}):
            with self.assertRaises(P.PlanError):
                P.parse_plan(bad)

    def test_reserved_and_duplicate_roles(self):
        plan = P.parse_plan(make(roles=[
            {"name": "@everyone"}, {"name": "here"}, {"name": "Mod"}, {"name": "mod"}, {"name": "  "},
        ]))
        self.assertEqual([r.name for r in plan.roles], ["Mod"])
        self.assertTrue(any("reserved" in w for w in plan.warnings))

    def test_limits(self):
        roles = [{"name": f"r{i}"} for i in range(P.MAX_ROLES + 10)]
        cats = [{"name": f"c{i}", "channels": [{"name": f"ch{i}-{j}"} for j in range(P.MAX_CHANNELS_PER_CATEGORY + 5)]}
                for i in range(P.MAX_CATEGORIES + 3)]
        plan = P.parse_plan({"roles": roles, "categories": cats})
        self.assertEqual(len(plan.roles), P.MAX_ROLES)
        self.assertLessEqual(len(plan.categories), P.MAX_CATEGORIES)
        self.assertLessEqual(plan.channel_count, P.MAX_CHANNELS)
        self.assertTrue(all(len(c.channels) <= P.MAX_CHANNELS_PER_CATEGORY for c in plan.categories))

    def test_duplicate_channels_and_unknown_kind(self):
        plan = P.parse_plan({"roles": [], "categories": [{"name": "A", "channels": [
            {"name": "Chat"}, {"name": "chat", "kind": "text"}, {"name": "chat", "kind": "weird"},
            {"name": "Chat", "kind": "voice"},
        ]}]})
        kinds = [(c.name, c.kind) for c in plan.categories[0].channels]
        self.assertEqual(kinds, [("chat", "text"), ("Chat", "voice")])

    def test_names_are_clipped_and_control_chars_removed(self):
        plan = P.parse_plan({"roles": [{"name": "X" * 500 + "\n\x00y"}], "categories": []})
        self.assertLessEqual(len(plan.roles[0].name), P.MAX_NAME)
        self.assertNotIn("\n", plan.roles[0].name)

    def test_empty_category_is_skipped_with_warning(self):
        plan = P.parse_plan({"roles": [{"name": "A"}], "categories": [{"name": "Empty", "channels": []}]})
        self.assertEqual(plan.categories, [])
        self.assertTrue(any("empty category" in w for w in plan.warnings))


class SchemaTests(unittest.TestCase):
    def test_safe_permissions_are_real_discord_flags(self):
        for name in P.SAFE_PERMISSIONS:
            self.assertIn(name, discord.Permissions.VALID_FLAGS, name)

    def test_dangerous_permissions_are_not_allowed(self):
        for name in ("administrator", "manage_guild", "manage_roles", "manage_channels",
                     "manage_webhooks", "ban_members", "kick_members", "mention_everyone"):
            self.assertNotIn(name, P.SAFE_PERMISSIONS)

    def test_every_object_in_schema_is_closed_and_fully_required(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(P.PLAN_SCHEMA)

    def test_schema_uses_no_unsupported_constraints(self):
        text = repr(P.PLAN_SCHEMA)
        for banned in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
