import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
import openai

from luna import planner
from luna.plan import PLAN_SCHEMA


def run(coro):
    return asyncio.run(coro)


GOOD_JSON = json.dumps({
    "summary": "Study group",
    "roles": [{"name": "Mentor", "color": "#123456", "hoist": True, "mentionable": False, "permissions": []}],
    "categories": [{"name": "Hub", "private_to": [], "channels": [
        {"name": "chat", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []}]}],
})


def completion(content=GOOD_JSON, finish="stop", refusal=None):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


def api_error(cls, status, message, code=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"message": message, "code": code}
    return cls(message, response=httpx.Response(status, request=request), body=body)


def patched_client(create):
    client = mock.MagicMock()
    client.chat.completions.create = create
    return mock.patch.object(planner.openai, "AsyncOpenAI", return_value=client), client


def generate(request="x" * 20, snapshot=None):
    return run(planner.generate_plan(request, snapshot or {}))


class PlannerTests(unittest.TestCase):
    def test_success_sends_strict_json_schema_and_returns_a_plan(self):
        patcher, client = patched_client(mock.AsyncMock(return_value=completion()))
        with patcher, mock.patch.dict("os.environ", {"LUNA_MODEL": ""}):
            plan = generate("a study server for students", {"roles_most_senior_first": []})
        self.assertEqual(plan.roles[0].name, "Mentor")

        kwargs = client.chat.completions.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4.1")
        fmt = kwargs["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertIs(fmt["json_schema"]["schema"], PLAN_SCHEMA)
        self.assertIn("max_completion_tokens", kwargs)      # reasoning models reject max_tokens
        self.assertNotIn("max_tokens", kwargs)
        system, user = kwargs["messages"]
        self.assertEqual(system["role"], "system")
        self.assertIn("a study server for students", user["content"])

    def test_model_can_be_overridden(self):
        patcher, client = patched_client(mock.AsyncMock(return_value=completion()))
        with patcher, mock.patch.dict("os.environ", {"LUNA_MODEL": "gpt-5-mini"}):
            generate()
        self.assertEqual(client.chat.completions.create.await_args.kwargs["model"], "gpt-5-mini")

    def test_missing_key_gives_setup_instructions(self):
        # The SDK raises OpenAIError when it can't find OPENAI_API_KEY.
        with mock.patch.object(planner.openai, "AsyncOpenAI", side_effect=openai.OpenAIError("no key")):
            with self.assertRaises(planner.PlannerError) as ctx:
                generate()
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_real_sdk_without_a_key_is_handled(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "OPENAI_API_KEY"}
        with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(planner.PlannerError) as ctx:
            generate()
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_friendly_messages_for_common_failures(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        cases = [
            (api_error(openai.AuthenticationError, 401, "bad key"), "rejected the API key"),
            (api_error(openai.PermissionDeniedError, 403, "no access"), "isn't allowed"),
            (api_error(openai.NotFoundError, 404, "no model"), "not found"),
            (api_error(openai.RateLimitError, 429, "slow down", code="rate_limit_exceeded"), "rate limiting"),
            (api_error(openai.RateLimitError, 429, "quota", code="insufficient_quota"), "out of credit"),
            (api_error(openai.BadRequestError, 400, "bad schema"), "structured outputs"),
            (api_error(openai.InternalServerError, 500, "oops"), "(500)"),
            (openai.APITimeoutError(request=request), "too long"),
            (openai.APIConnectionError(request=request), "reach OpenAI"),
        ]
        for exc, fragment in cases:
            patcher, _ = patched_client(mock.AsyncMock(side_effect=exc))
            with patcher, self.assertRaises(planner.PlannerError) as ctx:
                generate()
            self.assertIn(fragment, str(ctx.exception), type(exc).__name__)

    def test_refusal_truncation_filter_and_garbage(self):
        for response, fragment in [
            (completion(content=None, refusal="I can't help with that."), "declined"),
            (completion(content="", finish="content_filter"), "declined"),
            (completion(content=GOOD_JSON[:20], finish="length"), "too large"),
            (completion(content=None), "couldn't use"),
            (completion(content="not json"), "couldn't use"),
            (completion(content='{"summary": "", "notes": "", "roles": [], "categories": []}'), "anything to change"),
        ]:
            patcher, _ = patched_client(mock.AsyncMock(return_value=response))
            with patcher, self.assertRaises(planner.PlannerError) as ctx:
                generate()
            self.assertIn(fragment, str(ctx.exception))

    def test_empty_plan_shows_the_models_explanation(self):
        empty = json.dumps({"summary": "", "roles": [], "categories": [],
                            "notes": "I can't delete existing channels or roles."})
        patcher, _ = patched_client(mock.AsyncMock(return_value=completion(content=empty)))
        with patcher, self.assertRaises(planner.PlannerError) as ctx:
            generate("delete everything and start fresh")
        message = str(ctx.exception)
        self.assertIn("anything to change", message)
        self.assertIn("I can't assign roles to members", message)
        self.assertIn("I can't delete existing channels or roles.", message)

    def test_empty_plan_without_notes_still_explains_the_limit(self):
        patcher, _ = patched_client(mock.AsyncMock(return_value=completion(
            content='{"summary": "", "notes": "", "roles": [], "categories": []}')))
        with patcher, self.assertRaises(planner.PlannerError) as ctx:
            generate()
        self.assertIn("Try describing what you'd like", str(ctx.exception))

    def test_an_unexplained_empty_plan_is_retried_once(self):
        empty = completion(content=json.dumps({"summary": "Adds a voice channel.", "notes": "", "roles": [], "categories": []}))
        create = mock.AsyncMock(side_effect=[empty, completion()])
        patcher, _ = patched_client(create)
        with patcher:
            plan = generate()
        self.assertEqual(plan.roles[0].name, "Mentor")
        self.assertEqual(create.await_count, 2)

    def test_if_the_retry_is_empty_too_the_admin_sees_what_the_model_understood(self):
        empty = completion(content=json.dumps({"summary": "Adds a voice channel.", "notes": "", "roles": [], "categories": []}))
        create = mock.AsyncMock(return_value=empty)
        patcher, _ = patched_client(create)
        with patcher, self.assertRaises(planner.PlannerError) as ctx:
            generate()
        self.assertEqual(create.await_count, 2)
        self.assertIn("Adds a voice channel.", str(ctx.exception))
        self.assertIn("couldn't turn it into any actions", str(ctx.exception))

    def test_an_explained_empty_plan_is_not_retried(self):
        explained = completion(content=json.dumps({"summary": "", "notes": "Too vague.", "roles": [], "categories": []}))
        create = mock.AsyncMock(return_value=explained)
        patcher, _ = patched_client(create)
        with patcher, self.assertRaises(planner.PlannerError) as ctx:
            generate()
        self.assertEqual(create.await_count, 1)
        self.assertIn("Too vague.", str(ctx.exception))

    def test_api_errors_are_not_retried(self):
        create = mock.AsyncMock(side_effect=api_error(openai.AuthenticationError, 401, "bad key"))
        patcher, _ = patched_client(create)
        with patcher, self.assertRaises(planner.PlannerError):
            generate()
        self.assertEqual(create.await_count, 1)

    def test_unusable_output_is_logged_with_the_raw_text(self):
        patcher, _ = patched_client(mock.AsyncMock(return_value=completion(content="<<garbage>>")))
        with patcher, self.assertLogs("luna.planner", level="WARNING") as logs, self.assertRaises(planner.PlannerError):
            generate("my request text")
        text = " ".join(logs.output)
        self.assertIn("<<garbage>>", text)
        self.assertIn("my request text", text)

    def test_user_message_wraps_request_and_snapshot(self):
        text = planner.build_user_message("make it cozy", {"roles_most_senior_first": ["Boss"]})
        self.assertIn("<request>\nmake it cozy\n</request>", text)
        self.assertIn("Boss", text)

    def test_prompt_defaults_to_plain_names(self):
        self.assertIn("no emoji", planner.SYSTEM_PROMPT)

    def test_prompt_keeps_powerful_permissions_opt_in(self):
        self.assertIn("Do NOT give anyone administrator", planner.SYSTEM_PROMPT)
        self.assertIn("unless the admin explicitly asked", planner.SYSTEM_PROMPT)

    def test_prompt_is_cautious_about_deletion_and_explains_refs_and_notes(self):
        prompt = planner.SYSTEM_PROMPT
        self.assertIn("Delete only what they clearly asked to remove", prompt)
        self.assertIn("delete nothing and explain", prompt)
        self.assertIn("ONLY by those refs", prompt)
        self.assertIn("Prefer an existing suitable category", prompt)
        self.assertIn("locked_", prompt)
        self.assertIn('"notes"', prompt)

    def test_refs_are_passed_through_so_invented_ones_are_dropped(self):
        data = json.dumps({"summary": "", "notes": "", "roles": [], "categories": [], "edit_roles": [],
                           "edit_categories": [], "edit_channels": [], "role_order": [], "category_order": [],
                           "delete": ["ch:1", "ch:99", "role:1"]})
        patcher, _ = patched_client(mock.AsyncMock(return_value=completion(content=data)))
        with patcher:
            plan = run(planner.generate_plan("delete the old chat", {}, {"ch:1": "text"}))
        self.assertEqual(plan.deletions, ["ch:1"])
        self.assertEqual(sum("can't touch" in w for w in plan.warnings), 2)

    def test_without_refs_a_delete_only_plan_is_empty(self):
        data = json.dumps({"summary": "", "notes": "", "roles": [], "categories": [], "delete": ["ch:1"]})
        patcher, _ = patched_client(mock.AsyncMock(return_value=completion(content=data)))
        with patcher, self.assertRaises(planner.PlannerError):
            generate()

if __name__ == "__main__":
    unittest.main()
