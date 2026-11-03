import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import ai_customizer as cz
from luna.applier import ApplyReport, ServerIndex
from luna.plan import parse_plan
from luna.planner import PlannerError


def run(coro):
    return asyncio.run(coro)


def interaction(*, user_id=7, admin=True, guild_id=42):
    i = mock.MagicMock()
    i.user.id = user_id
    i.permissions = SimpleNamespace(administrator=admin)
    i.guild.id = guild_id
    i.response.edit_message = mock.AsyncMock()
    i.response.send_message = mock.AsyncMock()
    i.response.send_modal = mock.AsyncMock()
    i.response.defer = mock.AsyncMock()
    i.followup.send = mock.AsyncMock()
    i.edit_original_response = mock.AsyncMock()
    return i


def safe_plan():
    return parse_plan({"roles": [{"name": "A"}]})


def risky_plan():
    index = ServerIndex()
    index.add("ch:1", 1, "text", "old")
    return parse_plan({"delete": ["ch:1"]}, index.kinds), index


def make_view(plan=None, index=None, origin=None):
    cog = cz.Customizer(mock.MagicMock())
    origin = origin or interaction()
    return cz.ConfirmView(cog, origin, plan or safe_plan(), index or ServerIndex()), cog, origin


def patched_apply(report=None):
    return mock.patch.object(cz, "apply_plan", mock.AsyncMock(return_value=report or ApplyReport(roles_created=["A"])))


class ConfirmViewTests(unittest.TestCase):
    def test_ordinary_plan_has_a_green_build_button_and_applies_immediately(self):
        async def go():
            view, cog, origin = make_view()
            self.assertEqual((view.apply_button.label, view.apply_button.style), ("Build it", discord.ButtonStyle.success))
            click = interaction()
            with patched_apply() as apply:
                await view.apply_button.callback(click)
            return view, cog, click, apply
        view, cog, click, apply = run(go())
        apply.assert_awaited_once()
        click.response.send_modal.assert_not_awaited()
        click.response.edit_message.assert_awaited_once()
        final = click.edit_original_response.await_args.kwargs
        self.assertIn("Done", final["embed"].title)
        self.assertEqual(cog.busy, set())                    # released
        self.assertTrue(view.is_finished())

    def test_risky_plan_has_a_red_button_that_asks_for_confirmation_instead_of_applying(self):
        async def go():
            plan, index = risky_plan()
            view, cog, origin = make_view(plan, index)
            self.assertEqual(view.apply_button.style, discord.ButtonStyle.danger)
            self.assertIn("Apply changes", view.apply_button.label)
            click = interaction()
            with patched_apply() as apply:
                await view.apply_button.callback(click)
            return click, apply
        click, apply = run(go())
        apply.assert_not_awaited()
        click.response.send_modal.assert_awaited_once()
        self.assertIsInstance(click.response.send_modal.await_args.args[0], cz.ConfirmModal)

    def test_modal_applies_only_when_confirm_is_typed(self):
        async def go(typed):
            plan, index = risky_plan()
            view, cog, origin = make_view(plan, index)
            modal = cz.ConfirmModal(view)
            modal.answer._value = typed
            submit = interaction()
            with patched_apply() as apply:
                await modal.on_submit(submit)
            return submit, apply, view
        for wrong in ("", "yes", "CONFIRMED", "delete", "CONFIRM it"):
            submit, apply, view = run(go(wrong))
            apply.assert_not_awaited()
            self.assertIn("nothing was changed", submit.response.send_message.await_args.args[0])
            self.assertTrue(submit.response.send_message.await_args.kwargs["ephemeral"])
            self.assertFalse(view.is_finished())              # the admin can try again
        for right in ("CONFIRM", "confirm", "  Confirm  "):
            submit, apply, view = run(go(right))
            apply.assert_awaited_once()
            self.assertTrue(view.is_finished())

    def test_only_the_admin_who_ran_the_command_can_use_the_buttons(self):
        async def go(**kw):
            view, _, _ = make_view(origin=interaction(user_id=7))
            other = interaction(**kw)
            return await view.interaction_check(other), other
        ok, _ = run(go(user_id=7))
        self.assertTrue(ok)
        for kw in ({"user_id": 8}, {"user_id": 7, "admin": False}):
            ok, other = run(go(**kw))
            self.assertFalse(ok)
            self.assertTrue(other.response.send_message.await_args.kwargs["ephemeral"])

    def test_nothing_runs_twice_at_once_in_one_server(self):
        async def go():
            view, cog, _ = make_view()
            cog.busy.add(42)
            click = interaction(guild_id=42)
            with patched_apply() as apply:
                await view.run(click)
            return click, apply, cog
        click, apply, cog = run(go())
        apply.assert_not_awaited()
        self.assertIn("already working", click.response.send_message.await_args.args[0])
        self.assertEqual(cog.busy, {42})                     # untouched

    def test_a_crash_is_reported_and_the_server_is_unlocked(self):
        async def go():
            view, cog, _ = make_view()
            click = interaction()
            with mock.patch.object(cz, "apply_plan", mock.AsyncMock(side_effect=RuntimeError("boom"))):
                await view.run(click)
            return click, cog
        with self.assertLogs("luna.customize", level="ERROR"):
            click, cog = run(go())
        self.assertIn("went wrong", click.edit_original_response.await_args.kwargs["content"])
        self.assertIn("backup", click.edit_original_response.await_args.kwargs["content"])
        self.assertEqual(cog.busy, set())

    def test_the_backup_is_attached_to_the_result(self):
        async def go(path):
            view, _, _ = make_view()
            click = interaction()
            with patched_apply(ApplyReport(deleted=["channel x"], backup_path=path)):
                await view.run(click)
            return click
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "b.json"
            backup.write_text("{}", encoding="utf-8")
            click = run(go(str(backup)))
            attachments = click.edit_original_response.await_args.kwargs["attachments"]
            self.assertEqual([a.filename for a in attachments], ["luna-backup.json"])
            for attachment in attachments:
                attachment.close()      # discord.py does this after sending; Windows won't delete an open file
        click = run(go(None))
        self.assertEqual(click.edit_original_response.await_args.kwargs["attachments"], [])

    def test_the_apply_is_given_the_index_and_an_audit_reason(self):
        async def go():
            index = ServerIndex()
            view, _, _ = make_view(index=index)
            click = interaction()
            with patched_apply() as apply:
                await view.run(click)
            return apply, index
        apply, index = run(go())
        self.assertIs(apply.await_args.kwargs["index"], index)
        self.assertIn("Luna.AI /customize", apply.await_args.kwargs["reason"])

    def test_cancel_changes_nothing(self):
        async def go():
            view, _, _ = make_view()
            click = interaction()
            with patched_apply() as apply:
                await view.cancel.callback(click)
            return view, click, apply
        view, click, apply = run(go())
        apply.assert_not_awaited()
        self.assertIn("Nothing was changed", click.response.edit_message.await_args.kwargs["content"])
        self.assertTrue(view.is_finished())

    def test_an_expired_plan_says_nothing_changed(self):
        async def go():
            view, _, origin = make_view()
            await view.on_timeout()
            return origin
        origin = run(go())
        self.assertIn("nothing was changed", origin.edit_original_response.await_args.kwargs["content"])


class CustomizeCommandTests(unittest.TestCase):
    PROMPT = "make the Mod role red"

    def call(self, *, plan=None, planner_error=None, preview=None, busy=False):
        async def go():
            cog = cz.Customizer(mock.MagicMock())
            if busy:
                cog.busy.add(42)
            i = interaction()
            generate = (mock.AsyncMock(side_effect=planner_error) if planner_error
                        else mock.AsyncMock(return_value=plan))
            with mock.patch.object(cz, "snapshot_guild", return_value=({}, ServerIndex())), \
                    mock.patch.object(cz, "generate_plan", generate), \
                    mock.patch.object(cz, "apply_plan", mock.AsyncMock(return_value=preview or ApplyReport(dry_run=True, roles_created=["A"]))):
                await cog.customize.callback(cog, i, self.PROMPT)
            return i, generate
        return run(go())

    def test_a_planner_error_is_shown_to_the_admin(self):
        i, _ = self.call(planner_error=PlannerError("OpenAI rejected the API key."))
        self.assertIn("OpenAI rejected the API key.", i.followup.send.await_args.args[0])
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])

    def test_a_plan_is_previewed_with_buttons_and_nothing_is_applied_yet(self):
        i, generate = self.call(plan=safe_plan())
        kwargs = i.followup.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], cz.ConfirmView)
        self.assertEqual(kwargs["embed"].title, "🌙 Here's my plan")
        self.assertEqual(kwargs["files"], [])                 # create-only: no attachment needed
        self.assertEqual(generate.await_args.args[0], self.PROMPT)

    def test_a_plan_that_changes_existing_items_attaches_the_full_text(self):
        plan, index = risky_plan()
        preview = ApplyReport(dry_run=True, deleted=["channel old"])
        async def go():
            cog = cz.Customizer(mock.MagicMock())
            i = interaction()
            with mock.patch.object(cz, "snapshot_guild", return_value=({}, index)), \
                    mock.patch.object(cz, "generate_plan", mock.AsyncMock(return_value=plan)), \
                    mock.patch.object(cz, "apply_plan", mock.AsyncMock(return_value=preview)):
                await cog.customize.callback(cog, i, self.PROMPT)
            return i
        i = run(go())
        kwargs = i.followup.send.await_args.kwargs
        self.assertEqual([f.filename for f in kwargs["files"]], ["luna-plan.txt"])
        self.assertIn("WILL BE DELETED", kwargs["files"][0].fp.read().decode("utf-8"))
        self.assertIn("Review carefully", kwargs["embed"].title)
        self.assertTrue(kwargs["view"].risky)

    def test_a_plan_that_matches_the_server_already_gets_no_buttons(self):
        i, _ = self.call(plan=safe_plan(), preview=ApplyReport(dry_run=True))     # nothing_to_do
        kwargs = i.followup.send.await_args.kwargs
        self.assertNotIn("view", kwargs)
        self.assertIn("nothing to do", kwargs["embed"].footer.text)

    def test_a_second_request_while_one_is_running_is_refused_before_calling_the_ai(self):
        i, generate = self.call(plan=safe_plan(), busy=True)
        generate.assert_not_awaited()
        self.assertIn("already working", i.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
