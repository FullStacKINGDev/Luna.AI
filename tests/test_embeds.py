import unittest

from luna import plan as P
from luna.applier import ApplyReport
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
    return plan


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

    def test_preview_marks_existing_items(self):
        plan = P.parse_plan({"roles": [{"name": "A"}], "categories": [{"name": "B", "channels": [{"name": "c"}]}]})
        preview = ApplyReport(dry_run=True, roles_existing=["A"], categories_existing=["B"], channels_existing=["B / c"])
        text = " ".join(f.value for f in build_preview_embed(plan, preview).fields)
        self.assertIn("exists, skipped", text)

    def test_result_embed_variants_fit_limits(self):
        long = ["e" * 300] * 20
        for report in (
            ApplyReport(roles_created=["a"]),
            ApplyReport(errors=long, notes=long),
            ApplyReport(aborted="a" * 2000, errors=long),
        ):
            self.check(build_result_embed(report))

    def test_result_embed_titles(self):
        self.assertIn("ready", build_result_embed(ApplyReport(roles_created=["a"])).title)
        self.assertIn("problems", build_result_embed(ApplyReport(errors=["x"])).title)
        self.assertIn("stop", build_result_embed(ApplyReport(aborted="x")).title)


if __name__ == "__main__":
    unittest.main()
