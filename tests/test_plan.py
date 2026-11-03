import unittest

import discord

from luna import plan as P


def make(**overrides):
    data = {
        "summary": "  A   study server ",
        "roles": [
            {"name": "Mentor", "color": "#3498db", "hoist": True, "mentionable": True,
             "permissions": ["manage_messages", "fly", "ban_members", "manage_messages"]},
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
        self.assertEqual(mentor.permissions, ("manage_messages", "ban_members"))  # unknown + duplicate removed
        text, voice = plan.categories[0].channels
        self.assertEqual(text.name, "welcome-here!!")                # slugged
        self.assertEqual(text.writers, ("Mentor",))                  # @everyone dropped
        self.assertEqual(voice.name, "Lounge")                       # voice keeps case
        self.assertFalse(voice.read_only)                            # read-only is meaningless for voice
        self.assertEqual(voice.topic, "")

    def test_unknown_permissions_are_reported(self):
        plan = P.parse_plan(make())
        self.assertTrue(any("fly" in w for w in plan.warnings))

    def test_elevated_permissions_are_kept_but_make_the_plan_risky(self):
        plan = P.parse_plan(make())
        self.assertEqual(P.elevated_in(plan.roles[0].permissions), ("ban_members",))
        self.assertTrue(any("powerful permissions" in r for r in plan.risk_reasons()))
        calm = P.parse_plan({"roles": [{"name": "A", "permissions": ["manage_messages"]}], "categories": []})
        self.assertEqual(calm.risk_reasons(), [])

    def test_empty_or_garbage_raises(self):
        for bad in (None, [], "x", {}, {"roles": [], "categories": []},
                    {"roles": "nope", "categories": 5}):
            with self.assertRaises(P.PlanError):
                P.parse_plan(bad)

    def test_notes_are_cleaned_and_kept(self):
        plan = P.parse_plan(make(notes="  I can't   rename\nchannels. " + "x" * 2000))
        self.assertTrue(plan.notes.startswith("I can't rename channels."))
        self.assertLessEqual(len(plan.notes), P.MAX_NOTES)
        self.assertEqual(P.parse_plan(make()).notes, "")

    def test_empty_plan_raises_a_specific_error_that_carries_the_notes(self):
        with self.assertRaises(P.EmptyPlanError) as ctx:
            P.parse_plan({"summary": "", "notes": "Nothing to add.", "roles": [], "categories": []})
        self.assertEqual(ctx.exception.notes, "Nothing to add.")
        self.assertIsInstance(ctx.exception, P.PlanError)   # existing handlers still catch it

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
    def test_all_permission_names_are_real_discord_flags(self):
        for name in P.SAFE_PERMISSIONS + P.ELEVATED_PERMISSIONS:
            self.assertIn(name, discord.Permissions.VALID_FLAGS, name)

    def test_schema_offers_every_permission_and_no_others(self):
        enum = P.PLAN_SCHEMA["properties"]["roles"]["items"]["properties"]["permissions"]["items"]["enum"]
        self.assertEqual(sorted(enum), sorted(P.SAFE_PERMISSIONS + P.ELEVATED_PERMISSIONS))

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


REFS = {
    "role:1": "role", "role:2": "role", "role:3": "role",
    "cat:1": "category", "cat:2": "category",
    "ch:1": "text", "ch:2": "voice", "ch:3": "announcement",
}


def change(**data):
    return P.parse_plan(data, REFS)


def role_edit(**kw):
    base = {"ref": "role:1", "new_name": "", "color": "", "hoist": "keep", "mentionable": "keep",
            "permissions_mode": "keep", "permissions": []}
    return {**base, **kw}


def channel_edit(**kw):
    base = {"ref": "ch:1", "new_name": "", "topic": "", "move_to": "", "visibility": "keep",
            "private_to": [], "read_only": "keep", "writers": []}
    return {**base, **kw}


class ChangeParsingTests(unittest.TestCase):
    def test_role_edit_normalises_and_treats_blank_as_no_change(self):
        plan = change(edit_roles=[role_edit(new_name="  @Boss  ", color="#FF0000", hoist="yes", mentionable="no")])
        e = plan.role_edits[0]
        self.assertEqual((e.ref, e.new_name, e.color, e.hoist, e.mentionable, e.permissions),
                         ("role:1", "Boss", 0xFF0000, True, False, None))

    def test_a_no_op_edit_is_dropped(self):
        with self.assertRaises(P.EmptyPlanError):
            change(edit_roles=[role_edit()], edit_channels=[channel_edit()])

    def test_permissions_are_only_replaced_when_asked(self):
        keep = change(edit_roles=[role_edit(new_name="X", permissions=["view_channel"])])
        self.assertIsNone(keep.role_edits[0].permissions)
        replace = change(edit_roles=[role_edit(permissions_mode="replace", permissions=["view_channel", "fly"])])
        self.assertEqual(replace.role_edits[0].permissions, ("view_channel",))
        self.assertTrue(any("replaced" in r for r in replace.risk_reasons()))

    def test_unknown_or_wrong_kind_refs_are_dropped_with_a_warning(self):
        plan = change(
            roles=[{"name": "New"}],
            edit_roles=[role_edit(ref="role:99", new_name="A"), role_edit(ref="ch:1", new_name="B"),
                        role_edit(ref="ROLE:2", new_name="Ok")],
            delete=["role:42", "nonsense", "", 7],
        )
        self.assertEqual([e.ref for e in plan.role_edits], ["role:2"])      # case-normalised
        self.assertEqual(plan.deletions, [])
        self.assertGreaterEqual(sum("can't touch" in w for w in plan.warnings), 5)

    def test_without_refs_nothing_existing_can_be_touched(self):
        plan = P.parse_plan({"roles": [{"name": "New"}], "delete": ["role:1"],
                             "edit_roles": [role_edit(new_name="X")]})
        self.assertEqual((plan.deletions, plan.role_edits), ([], []))

    def test_deletions_are_deduplicated_capped_and_flag_the_plan_risky(self):
        many = {f"ch:{i}": "text" for i in range(100, 100 + P.MAX_DELETIONS + 10)}
        plan = P.parse_plan({"delete": list(many) + ["ch:100"]}, many)
        self.assertEqual(len(plan.deletions), P.MAX_DELETIONS)
        self.assertEqual(len(set(plan.deletions)), P.MAX_DELETIONS)
        self.assertTrue(any("deletion" in r for r in plan.risk_reasons()))
        self.assertTrue(any("Deletion limit" in w for w in plan.warnings))

    def test_edits_to_something_being_deleted_are_dropped(self):
        plan = change(delete=["role:1"], edit_roles=[role_edit(new_name="X"), role_edit(ref="role:2", new_name="Y")])
        self.assertEqual([e.ref for e in plan.role_edits], ["role:2"])
        self.assertTrue(any("being deleted" in w for w in plan.warnings))

    def test_channel_edit_uses_the_channel_kind_for_names_and_topics(self):
        plan = change(edit_channels=[
            channel_edit(ref="ch:1", new_name="Study Help!", topic="Ask here"),
            channel_edit(ref="ch:2", new_name="Group Study", topic="ignored for voice", read_only="yes"),
        ])
        text, voice = plan.channel_edits
        self.assertEqual((text.new_name, text.topic), ("study-help!", "Ask here"))
        self.assertEqual((voice.new_name, voice.topic, voice.read_only), ("Group Study", "", None))

    def test_move_to_accepts_none_a_known_category_or_a_new_one_only(self):
        plan = change(
            categories=[{"name": "Fresh", "private_to": [], "channels": [{"name": "c", "kind": "text"}]}],
            edit_channels=[channel_edit(ref="ch:1", move_to="cat:2"),
                           channel_edit(ref="ch:2", move_to="none"),
                           channel_edit(ref="ch:3", move_to="fresh")],
        )
        self.assertEqual([e.move_to for e in plan.channel_edits], ["cat:2", "none", "fresh"])
        bad = change(roles=[{"name": "R"}], edit_channels=[
            channel_edit(ref="ch:1", move_to="cat:99", new_name="a"),
            channel_edit(ref="ch:2", move_to="Nowhere", new_name="b")])
        self.assertEqual([e.move_to for e in bad.channel_edits], ["", ""])
        self.assertEqual(sum("Ignored moving" in w for w in bad.warnings), 2)

    def test_cannot_move_into_a_category_that_is_being_deleted(self):
        plan = change(delete=["cat:2"], edit_channels=[channel_edit(move_to="cat:2", new_name="x")])
        self.assertEqual(plan.channel_edits[0].move_to, "")

    def test_visibility_and_read_only(self):
        plan = change(edit_channels=[channel_edit(visibility="private", private_to=["Staff", "@everyone"],
                                                  read_only="yes", writers=["Staff"])])
        e = plan.channel_edits[0]
        self.assertEqual((e.visibility, e.private_to, e.read_only, e.writers), ("private", ("Staff",), True, ("Staff",)))

    def test_ordering_needs_two_valid_unique_refs(self):
        plan = change(role_order=["role:2", "role:1", "role:2", "role:99"], category_order=["cat:1"])
        self.assertEqual(plan.role_order, ["role:2", "role:1"])
        self.assertEqual(plan.category_order, [])

    def test_category_edit(self):
        plan = change(edit_categories=[{"ref": "cat:1", "new_name": "  Study  Zone "}, {"ref": "cat:2", "new_name": ""}])
        self.assertEqual([(e.ref, e.new_name) for e in plan.category_edits], [("cat:1", "Study Zone")])

    def test_counts_and_flags(self):
        plan = change(edit_roles=[role_edit(new_name="X")], role_order=["role:1", "role:2"], delete=["ch:3"])
        self.assertEqual(plan.edit_count, 2)
        self.assertTrue(plan.has_changes_to_existing)
        self.assertFalse(plan.is_empty())


if __name__ == "__main__":
    unittest.main()
