import unittest

from luna import plan as P
from luna.applier import ApplyReport, ServerIndex
from luna.embeds import build_preview_embed, build_result_embed

EMBED_LIMIT = 6000
FIELD_LIMIT = 1024


def worst_case_plan():
    """As large as a plan can legally get, with the longest allowed names."""
    roles = [{"name": f"R{i:02d}" + "x" * 90, "permissions": []} for i in range(P.MAX_ROLES)]
    cats, remaining = [], P.MAX_CHANNELS
    for i in range(P.MAX_CATEGORIES):
        n = min(P.MAX_CHANNELS_PER_CATEGORY, remaining)
        remaining -= n
        cats.append({"name": f"C{i:02d}" + "y" * 90, "private_to": ["Staff"], "channels": [
            {"name": f"ch{i:02d}-{j:02d}" + "z" * 80, "kind": "text", "read_only": True, "private_to": ["Staff"]}
            for j in range(n)]})
    plan = P.parse_plan({"summary": "s" * 1000, "roles": roles, "categories": cats})
    plan.warnings.extend(f"warning number {i} " + "w" * 100 for i in range(30))
    plan.notes = "n" * P.MAX_NOTES
    return plan


def index_with(*items):
    """ServerIndex from (ref, kind, name) tuples."""
    index = ServerIndex()
    for n, (ref, kind, name) in enumerate(items, start=1):
        index.add(ref, n, kind, name)
    return index


def edit_role(ref, **kw):
    base = {"ref": ref, "new_name": "", "color": "", "hoist": "keep", "mentionable": "keep",
            "permissions_mode": "keep", "permissions": []}
    return {**base, **kw}


def destructive_plan(n_delete=60, n_edit=40):
    """As big and as long-named as a destructive plan can legally get."""
    index = ServerIndex()
    for i in range(1, n_delete + 1):
        index.add(f"ch:{i}", i, "text", f"channel-{i}-" + "z" * 60)
    for j in range(n_edit):
        index.add(f"role:{j}", 5000 + j, "role", f"role-{j}-" + "r" * 60)
    plan = P.parse_plan({
        "summary": "s" * 400, "notes": "n" * P.MAX_NOTES,
        "roles": [{"name": f"New{i}" + "x" * 90, "permissions": []} for i in range(P.MAX_ROLES)],
        "categories": [{"name": f"C{i}" + "y" * 90, "private_to": [], "channels": [
            {"name": f"ch{i}-{j}" + "z" * 80, "kind": "text"} for j in range(10)]} for i in range(10)],
        "edit_roles": [edit_role(f"role:{j}", new_name=f"renamed-{j}-" + "q" * 70) for j in range(n_edit)],
        "delete": [f"ch:{i}" for i in range(1, n_delete + 1)],
    }, index.kinds)
    plan.warnings.extend(f"warning {i} " + "w" * 100 for i in range(30))
    return plan, index


class ChangeSectionTests(unittest.TestCase):
    @staticmethod
    def fields(embed):
        return {f.name: f.value for f in embed.fields}

    def test_edits_and_deletions_get_their_own_sections(self):
        index = index_with(("role:1", "role", "Mod"), ("ch:1", "text", "old-chat"))
        plan = P.parse_plan({"edit_roles": [edit_role("role:1", new_name="Moderator")], "delete": ["ch:1"]}, index.kinds)
        embed = build_preview_embed(plan, ApplyReport(dry_run=True), index)
        fields = self.fields(embed)
        self.assertIn("Moderator", fields["✏️ Changes to existing items"])
        self.assertIn("old-chat", fields["🗑️ Will be permanently deleted"])
        self.assertIn("changes existing items", embed.description)

    def test_risky_plans_look_different_and_say_why_confirmation_is_needed(self):
        index = index_with(("ch:1", "text", "old-chat"))
        risky = build_preview_embed(P.parse_plan({"delete": ["ch:1"]}, index.kinds), ApplyReport(dry_run=True), index)
        calm = build_preview_embed(P.parse_plan({"roles": [{"name": "A"}]}), ApplyReport(dry_run=True))
        self.assertIn("Review carefully", risky.title)
        self.assertIn("CONFIRM", risky.footer.text)
        self.assertIn("deletion", risky.footer.text)
        self.assertNotIn("CONFIRM", calm.footer.text)
        self.assertNotEqual(risky.color, calm.color)

    def test_a_create_only_plan_says_nothing_existing_is_touched(self):
        plan = P.parse_plan({"roles": [{"name": "A"}]})
        self.assertIn("Nothing existing is touched", build_preview_embed(plan, ApplyReport(dry_run=True)).description)

    def test_the_deletion_list_survives_when_the_channel_list_is_trimmed(self):
        plan, index = destructive_plan()
        embed = build_preview_embed(plan, ApplyReport(dry_run=True), index)
        fields = self.fields(embed)
        self.assertIn("channel-1-", fields["🗑️ Will be permanently deleted"])
        self.assertLessEqual(len(embed), EMBED_LIMIT)
        self.assertTrue(any("not shown here" in v for v in fields.values()))   # channels were what got trimmed


class EmbedLimitTests(unittest.TestCase):
    def check(self, embed):
        self.assertLessEqual(len(embed), EMBED_LIMIT)
        self.assertLessEqual(len(embed.fields), 25)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), FIELD_LIMIT)
            self.assertTrue(field.value.strip())

    def test_preview_of_a_maximum_size_plan_fits_discords_limits(self):
        plan = worst_case_plan()
        self.assertEqual(len(plan.roles), P.MAX_ROLES)
        self.check(build_preview_embed(plan, ApplyReport(dry_run=True)))

    def test_preview_of_a_tiny_plan(self):
        plan = P.parse_plan({"roles": [{"name": "A"}], "categories": [{"name": "B", "channels": [{"name": "c"}]}]})
        embed = build_preview_embed(plan, ApplyReport(dry_run=True))
        self.check(embed)
        self.assertIn("only **create**", embed.description)

    def test_preview_shows_the_models_note_only_when_there_is_one(self):
        plan = P.parse_plan({"roles": [{"name": "A"}], "categories": [], "notes": "I can't rename channels."})
        names = [f.name for f in build_preview_embed(plan, ApplyReport(dry_run=True)).fields]
        self.assertIn("📝 A note from me", names)
        plain = P.parse_plan({"roles": [{"name": "A"}], "categories": []})
        names = [f.name for f in build_preview_embed(plain, ApplyReport(dry_run=True)).fields]
        self.assertNotIn("📝 A note from me", names)

    def test_preview_marks_existing_items(self):
        plan = P.parse_plan({"roles": [{"name": "A"}], "categories": [{"name": "B", "channels": [{"name": "c"}]}]})
        preview = ApplyReport(dry_run=True, roles_existing=["A"], categories_existing=["B"], channels_existing=["B / c"])
        text = " ".join(f.value for f in build_preview_embed(plan, preview).fields)
        self.assertIn("exists, skipped", text)

    def test_a_maximum_size_destructive_preview_fits_discords_limits(self):
        plan, index = destructive_plan()
        self.assertEqual(len(plan.deletions), P.MAX_DELETIONS)
        self.check(build_preview_embed(plan, ApplyReport(dry_run=True), index))

    def test_result_embed_lists_what_was_deleted(self):
        embed = build_result_embed(ApplyReport(deleted=["channel old-chat", "role Old"]))
        self.assertIn("old-chat", {f.name: f.value for f in embed.fields}["Deleted"])

    def test_result_embed_variants_fit_limits(self):
        long = ["e" * 300] * 20
        for report in (
            ApplyReport(roles_created=["a"]),
            ApplyReport(errors=long, notes=long),
            ApplyReport(aborted="a" * 2000, errors=long),
        ):
            self.check(build_result_embed(report))

    def test_result_embed_titles(self):
        self.assertIn("Done", build_result_embed(ApplyReport(roles_created=["a"])).title)
        self.assertIn("problems", build_result_embed(ApplyReport(errors=["x"])).title)
        self.assertIn("stop", build_result_embed(ApplyReport(aborted="x")).title)


if __name__ == "__main__":
    unittest.main()
