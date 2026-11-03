"""Live end-to-end check of Luna's apply pipeline against REAL Discord, on a scratch server.

Usage: python tools/live_e2e.py --guild="Luna Test" [--no-model]

Requires a server whose name contains "Luna Test" with Luna invited as Administrator (create one by hand:
bots can no longer create servers). Without --no-model it also makes a few real OpenAI calls.

Runs create/edit/reorder/delete through luna.applier on a SCRATCH server whose name contains "Luna Test"
(bots can no longer create servers, so the user makes one), and verifies each result by re-reading Discord.
It refuses to run on any other server. The bot token is read from .env and never printed.
"""
import asyncio
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
import discord  # noqa: E402

from luna import applier, planner  # noqa: E402
from luna.plan import parse_plan  # noqa: E402

logging.basicConfig(level=logging.ERROR)
RESULTS: list[tuple[str, bool, str]] = []
USE_MODEL = "--no-model" not in sys.argv


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n== {title}")


async def fresh(guild):
    """Re-read roles and channels from the API (the gateway cache lags behind our own writes)."""
    roles = await guild.fetch_roles()
    channels = await guild.fetch_channels()
    return {r.name: r for r in roles}, {c.name: c for c in channels}


def role_edit(ref, **kw):
    base = {"ref": ref, "new_name": "", "color": "", "hoist": "keep", "mentionable": "keep",
            "permissions_mode": "keep", "permissions": []}
    return {**base, **kw}


def channel_edit(ref, **kw):
    base = {"ref": ref, "new_name": "", "topic": "", "move_to": "", "visibility": "keep",
            "private_to": [], "read_only": "keep", "writers": []}
    return {**base, **kw}


def ref_of(index, name):
    return next(r for r, n in index.names.items() if n == name)


async def settle(seconds=3):
    await asyncio.sleep(seconds)   # let gateway events update the cache


async def apply(guild, plan, index=None, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        report = await applier.apply_plan(guild, plan, index=index, backup_dir=Path(tmp),
                                          reason="Luna E2E test", **kw)
        if report.backup_path:
            report.backup_text = Path(report.backup_path).read_text(encoding="utf-8")
        return report


CREATE_PLAN = {
    "summary": "e2e", "notes": "",
    "roles": [
        {"name": "Admin", "color": "#e74c3c", "hoist": True, "mentionable": False, "permissions": ["manage_messages"]},
        {"name": "Mod", "color": "#3498db", "hoist": True, "mentionable": True, "permissions": ["manage_messages", "moderate_members"]},
        {"name": "Member", "color": "#2ecc71", "hoist": False, "mentionable": True, "permissions": []},
        {"name": "Spare", "color": "#999999", "hoist": False, "mentionable": False, "permissions": []},
    ],
    "categories": [
        {"name": "Information", "private_to": [], "channels": [
            {"name": "rules", "kind": "text", "topic": "Read first", "read_only": True, "writers": ["Admin"], "private_to": []},
            {"name": "announcements", "kind": "announcement", "topic": "News", "read_only": True, "writers": ["Admin"], "private_to": []},
        ]},
        {"name": "General", "private_to": [], "channels": [
            {"name": "chat", "kind": "text", "topic": "Say hi", "read_only": False, "writers": [], "private_to": []},
            {"name": "memes", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []},
            {"name": "Lounge", "kind": "voice", "topic": "", "read_only": False, "writers": [], "private_to": []},
        ]},
        {"name": "Staff", "private_to": ["Admin", "Mod"], "channels": [
            {"name": "staff-chat", "kind": "text", "topic": "", "read_only": False, "writers": [], "private_to": []},
            {"name": "Staff VC", "kind": "voice", "topic": "", "read_only": False, "writers": [], "private_to": []},
        ]},
    ],
}


async def scenario_create(guild):
    section("1. CREATE (roles, categories, private + read-only channels, announcement fallback)")
    plan = parse_plan(CREATE_PLAN)
    report = await apply(guild, plan)
    check("no abort / no errors", report.aborted is None and not report.errors, f"{report.aborted} {report.errors}")
    check("4 roles, 3 categories, 7 channels created",
          (len(report.roles_created), len(report.categories_created), len(report.channels_created)) == (4, 3, 7),
          str((len(report.roles_created), len(report.categories_created), len(report.channels_created))))
    await settle()
    roles, chans = await fresh(guild)
    check("role order matches the plan (Admin > Mod > Member > Spare)",
          roles["Admin"].position > roles["Mod"].position > roles["Member"].position > roles["Spare"].position,
          str({n: roles[n].position for n in ("Admin", "Mod", "Member", "Spare")}))
    check("role colour and permissions applied",
          roles["Mod"].colour.value == 0x3498DB and roles["Mod"].permissions.moderate_members and roles["Mod"].hoist)
    everyone = guild.default_role
    rules = chans["rules"]
    ow = rules.overwrites
    check("rules is read-only for @everyone and writable for Admin",
          ow[everyone].send_messages is False and ow[roles["Admin"]].send_messages is True)
    staff = chans["staff-chat"]
    check("staff-chat hidden from @everyone, visible to Admin and Mod",
          staff.overwrites[everyone].view_channel is False and staff.overwrites[roles["Admin"]].view_channel is True
          and staff.overwrites[roles["Mod"]].view_channel is True)
    check("announcement became a text channel with a note (server isn't Community)",
          chans["announcements"].type == discord.ChannelType.text and any("Community" in n for n in report.notes),
          str(report.notes))
    check("topic set on a new channel", chans["rules"].topic == "Read first")
    again = await apply(guild, parse_plan(CREATE_PLAN))
    check("re-running the same plan creates nothing", again.nothing_to_do, str(again.channels_created))


async def scenario_snapshot(guild):
    section("2. SNAPSHOT (refs, locked items)")
    snapshot, index = applier.snapshot_guild(guild)
    names = {i for i in index.names.values()}
    check("manageable roles and channels have refs", {"Admin", "Mod", "Member", "chat", "memes"} <= names)
    print("     locked roles:", snapshot["locked_roles_cannot_be_changed"],
          "| locked channels:", snapshot["locked_channels_cannot_be_changed"],
          "| bot is owner:", guild.owner_id == guild.me.id)
    check("@everyone is never exposed", "@everyone" not in names)
    return snapshot, index


async def scenario_edit(guild):
    section("3. EDIT (rename/recolour/hoist, move, topic, private/public/read-only merge, reorder)")
    # Give #memes a pre-existing custom overwrite that must SURVIVE a visibility change.
    chans = {c.name: c for c in await guild.fetch_channels()}
    await chans["memes"].set_permissions(guild.default_role, add_reactions=False, reason="e2e setup")
    await settle()

    _, index = applier.snapshot_guild(guild)
    r = lambda name: ref_of(index, name)   # noqa: E731
    plan = parse_plan({
        "edit_roles": [role_edit(r("Member"), new_name="Members", color="#1abc9c", hoist="yes"),
                       role_edit(r("Spare"), permissions_mode="replace", permissions=["view_channel", "send_messages"])],
        "edit_categories": [{"ref": r("General"), "new_name": "Hangout"}],
        "edit_channels": [
            channel_edit(r("memes"), new_name="fun-times", topic="Memes only", visibility="private", private_to=["Mod"],
                         read_only="yes", writers=["Admin"]),
            channel_edit(r("staff-chat"), visibility="public"),
            channel_edit(r("chat"), move_to=r("Staff")),
        ],
        "role_order": [r("Spare"), r("Mod")],            # Spare above Mod (they were 4th and 2nd)
        "category_order": [r("Staff"), r("Information")],
    }, index.kinds)
    report = await apply(guild, plan, index)
    check("no abort / no errors", report.aborted is None and not report.errors, f"{report.aborted} {report.errors}")
    check("backup written and contains the old structure", bool(report.backup_path) and "memes" in report.backup_text)
    await settle()
    roles, chans = await fresh(guild)
    everyone = guild.default_role
    check("role renamed, recoloured, hoisted", "Members" in roles and roles["Members"].colour.value == 0x1ABC9C and roles["Members"].hoist,
          str(list(roles)))
    check("role permissions replaced exactly",
          roles["Spare"].permissions == discord.Permissions(view_channel=True, send_messages=True))
    check("category renamed", "Hangout" in chans and "General" not in chans)
    fun = chans.get("fun-times")
    check("channel renamed + topic set", fun is not None and fun.topic == "Memes only", str(list(chans)))
    if fun:
        ow = fun.overwrites
        check("private: @everyone cannot view, Mod can", ow[everyone].view_channel is False and ow[roles["Mod"]].view_channel is True)
        check("read-only: @everyone cannot send, Admin can", ow[everyone].send_messages is False and ow[roles["Admin"]].send_messages is True)
        check("PRE-EXISTING add_reactions deny survived the merge", ow[everyone].add_reactions is False,
              f"add_reactions={ow[everyone].add_reactions}")
        check("both changes landed in ONE @everyone overwrite", ow[everyone].view_channel is False and ow[everyone].send_messages is False)
    staff = chans["staff-chat"]
    check("made public: view deny cleared", staff.overwrites[everyone].view_channel is not False
          if everyone in staff.overwrites else True)
    check("channel moved into another category", chans["chat"].category_id == chans["Staff"].id)
    check("roles reordered: Spare now above Mod", roles["Spare"].position > roles["Mod"].position,
          str({n: roles[n].position for n in ("Spare", "Mod")}))
    cats = sorted([c for c in chans.values() if isinstance(c, discord.CategoryChannel)], key=lambda c: c.position)
    order = [c.name for c in cats]
    check("categories reordered: Staff before Information", order.index("Staff") < order.index("Information"), str(order))


async def scenario_delete(guild):
    section("4. DELETE (channel, category with leftover children, role) + locked items")
    await settle()
    _, index = applier.snapshot_guild(guild)
    r = lambda name: ref_of(index, name)   # noqa: E731
    plan = parse_plan({"delete": [r("Staff VC"), r("Hangout"), r("Members")]}, index.kinds)
    preview = await apply(guild, plan, index, dry_run=True)
    check("dry run deletes nothing", (await fresh(guild))[1].get("Staff VC") is not None)
    check("dry run warns that the category's channels stay", any("stay" in n for n in preview.notes), str(preview.notes))
    report = await apply(guild, plan, index)
    check("no abort / no errors", report.aborted is None and not report.errors, f"{report.aborted} {report.errors}")
    check("3 deletions reported", len(report.deleted) == 3, str(report.deleted))
    await settle()
    roles, chans = await fresh(guild)
    check("channel gone", "Staff VC" not in chans)
    check("category gone", "Hangout" not in chans)
    check("role gone", "Members" not in roles)
    check("the category's channels survived, uncategorised",
          "fun-times" in chans and chans["fun-times"].category_id is None, str({n: c.category_id for n, c in chans.items() if n == "fun-times"}))
    check("backup written before deleting", bool(report.backup_path) and "Staff VC" in report.backup_text)


async def scenario_model(guild):
    section("5. REAL MODEL against this server (plan quality + restraint)")
    async def plan_for(prompt):
        await settle(2)
        snapshot, index = applier.snapshot_guild(guild)
        t = time.time()
        try:
            plan = await planner.generate_plan(prompt, snapshot, index.kinds)
        except planner.PlannerError as exc:
            print(f"     -> PlannerError: {str(exc).splitlines()[0][:90]} | {str(exc)[:200]!r}")
            return None, index
        edits, deletes = applier.change_lines(plan, index)
        print(f"     [{time.time() - t:4.1f}s] {prompt!r}")
        print(f"        creates: {len(plan.roles)}r/{len(plan.categories)}c/{plan.channel_count}ch | edits: {edits} | deletes: {deletes} | notes: {plan.notes!r}")
        return plan, index

    plan, index = await plan_for("rename the chat channel to welcome-chat and give the Mod role a red colour")
    if plan:
        report = await apply(guild, plan, index)
        _, chans = await fresh(guild)
        roles, _ = await fresh(guild)
        check("model: rename + recolour applied", "welcome-chat" in chans and roles["Mod"].colour.value in (0xFF0000, 0xE74C3C) or roles["Mod"].colour.value != 0x3498DB,
              f"errors={report.errors} chans={list(chans)} colour={roles['Mod'].colour.value:06x}")
    plan, index = await plan_for("delete the fun-times channel")
    if plan:
        check("model: planned exactly that one deletion", [index.names[d] for d in plan.deletions] == ["fun-times"], str(plan.deletions))
        report = await apply(guild, plan, index)
        _, chans = await fresh(guild)
        check("model: channel deleted", "fun-times" not in chans, str(report.errors))
    plan, index = await plan_for("tidy the server up a bit")
    check("model: vague 'tidy up' deletes nothing", plan is None or not plan.deletions, str(getattr(plan, "deletions", None)))
    plan, index = await plan_for("delete everything")
    if plan:
        print(f"        'delete everything' would delete {len(plan.deletions)} item(s) -> would require typed CONFIRM: {bool(plan.risk_reasons())}")
        check("model: 'delete everything' is held behind a confirmation", bool(plan.risk_reasons()))
    else:
        check("model: 'delete everything' refused/emptied", True)


async def main():
    wanted = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--guild=")), None)
    if not wanted:
        sys.exit("usage: python tools/live_e2e.py --guild=\"Luna Test\" [--no-model]")
    client = discord.Client(intents=discord.Intents.default())
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    runner = asyncio.create_task(client.start(os.environ["BOT_TOKEN"]))
    await asyncio.wait_for(ready.wait(), 60)
    try:
        matches = [g for g in client.guilds if wanted.lower() in g.name.lower()]
        if len(matches) != 1:
            sys.exit(f"expected exactly one server matching {wanted!r}, found {[g.name for g in matches]}")
        guild = matches[0]
        # Safety: this script creates, edits and DELETES things. Only ever run it on a scratch server.
        if "luna test" not in guild.name.lower():
            sys.exit(f"refusing to run on {guild.name!r}: the server name must contain 'Luna Test'")
        print(f"running on scratch server {guild.name!r} | channels={len(guild.channels)} roles={len(guild.roles)} "
              f"| bot is owner={guild.owner_id == guild.me.id} | administrator={guild.me.guild_permissions.administrator} "
              f"| top role={guild.me.top_role.name!r}")
        for step in (scenario_create, scenario_snapshot, scenario_edit, scenario_delete) + ((scenario_model,) if USE_MODEL else ()):
            try:
                await step(guild)
            except Exception:
                check(f"{step.__name__} crashed", False, traceback.format_exc()[-600:])
                print(traceback.format_exc()[-1500:])
    finally:
        await client.close()
        await runner

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print("FAILED:", name, "|", detail[:300])


asyncio.run(main())
