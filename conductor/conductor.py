from __future__ import annotations

import asyncio
import logging
import os
import traceback
from pathlib import Path
from typing import Awaitable, List, Optional, Set

from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

from .api import Api
from .devwatch import DevWatcher
from .ops import OpError, Ops, short

log = logging.getLogger("red.conductor")


class Conductor(commands.Cog):
    """Keeps git-hosted cogs installed, updated and rolled back.

    Prod: HTTP API + GitHub webhook + fallback poll (CONDUCTOR_MODE=prod, the default).
    Dev: hot-reloads loaded cogs from CONDUCTOR_WATCH folders (CONDUCTOR_MODE=dev).
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xC0DD0C70, force_registration=True)
        self.config.register_global(bad_commits={})
        self.lock = asyncio.Lock()
        self.ops = Ops(bot, bad_commits={})
        self.mode = os.environ.get("CONDUCTOR_MODE", "prod").lower()
        self.api: Optional[Api] = None
        self.tasks: List[asyncio.Task] = []
        self.pending_push: Set[str] = set()

    # ---------------------------------------------------------------- lifecycle

    async def cog_load(self) -> None:
        self.ops.bad_commits = await self.config.bad_commits()
        if self.mode == "dev":
            roots = [Path(p) for p in os.environ.get("CONDUCTOR_WATCH", "/cogs").split(":") if p]
            self._spawn(DevWatcher(self, roots).run(), "dev watcher")
            return

        token = os.environ.get("CONDUCTOR_API_TOKEN", "")
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if len(token) < 32 or len(secret) < 16:
            log.error("CONDUCTOR_API_TOKEN / GITHUB_WEBHOOK_SECRET missing or too short; API disabled")
        else:
            self.api = Api(self, token, secret)
            host = os.environ.get("CONDUCTOR_HOST", "0.0.0.0")
            port = int(os.environ.get("CONDUCTOR_PORT", "8080"))
            await self.api.start(host, port)
        poll = int(os.environ.get("CONDUCTOR_POLL_SECONDS", "900"))
        if poll > 0:
            self._spawn(self._poll_loop(poll), "update poll")

    async def cog_unload(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.api is not None:
            await self.api.stop()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        return

    def _spawn(self, coro: Awaitable[None], label: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"conductor {label}")

        def done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.error("Conductor task %r crashed", label, exc_info=t.exception())

        task.add_done_callback(done)
        self.tasks = [t for t in self.tasks if not t.done()] + [task]
        return task

    # ---------------------------------------------------------------- reporting

    async def notify(self, message: str) -> None:
        for page in pagify(message, delims=["\n```", "\n", " "], shorten_by=12, page_length=1900):
            await self.bot.send_to_owners(page)

    async def run_op(self, label: str, coro: Awaitable[str]) -> str:
        """Run one operation under the lock and DM the owner the outcome."""
        async with self.lock:
            try:
                message = await coro
            except OpError as e:
                await self.notify(f"❌ {label} failed:\n{e}")
                raise
            except Exception as e:
                log.exception("Conductor op %r crashed", label)
                tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                await self.notify(f"💥 {label} crashed:\n{box(tb[-1800:], 'py')}")
                raise
            finally:
                await self.config.bad_commits.set(self.ops.bad_commits)
        await self.notify(message)
        return message

    # ---------------------------------------------------------------- background updates

    def schedule_push_update(self, names: List[str]) -> None:
        fresh = [n for n in names if n not in self.pending_push]
        self.pending_push.update(fresh)
        for name in fresh:
            self._spawn(self._push_update(name), f"push update {name}")

    async def _push_update(self, name: str) -> None:
        try:
            async with self.lock:
                # Discard so a push arriving while we pull is queued again, not dropped.
                self.pending_push.discard(name)
                try:
                    changed, message = await self.ops.update(name)
                except OpError as e:
                    changed, message = True, str(e)
                except Exception as e:
                    log.exception("Push update of %s crashed", name)
                    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    changed, message = True, f"💥 update {name} crashed:\n{box(tb[-1800:], 'py')}"
                finally:
                    await self.config.bad_commits.set(self.ops.bad_commits)
            if changed:
                await self.notify(message)
            else:
                log.info("Push for %s: %s", name, message)
        finally:
            self.pending_push.discard(name)

    async def _poll_loop(self, interval: int) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            await asyncio.sleep(interval)
            try:
                states = await self.ops.status()
            except Exception:
                log.exception("Update poll could not list cogs")
                continue
            for state in states:
                if state.pinned or state.name == "conductor":
                    continue
                await self._push_update(state.name)

    # ---------------------------------------------------------------- commands

    @commands.group(name="conductor")
    @commands.is_owner()
    async def conductor(self, ctx: commands.Context) -> None:
        """Manage git-installed cogs."""

    @conductor.command(name="status")
    async def conductor_status(self, ctx: commands.Context) -> None:
        """Show installed cogs, their commits and pinned/loaded state."""
        lines = [f"mode: {self.mode}" + (" | API up" if self.api and self.api.runner else "")]
        for s in await self.ops.status():
            flags = []
            if s.pinned:
                flags.append("pinned")
            if not s.loaded:
                flags.append("NOT LOADED")
            if s.head and s.head != s.commit and not s.pinned:
                flags.append(f"head {short(s.head)}")
            if self.ops.bad_commits.get(s.name):
                flags.append(f"bad {short(self.ops.bad_commits[s.name])}")
            lines.append(f"{s.name:<20} {short(s.commit)}  {s.repo}  {' '.join(flags)}")
        if len(lines) == 1:
            lines.append("(no cogs installed by Downloader)")
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page))

    async def _cmd(self, ctx: commands.Context, label: str, coro: Awaitable[str]) -> None:
        async with ctx.typing():
            try:
                await self.run_op(label, coro)
            except Exception:
                pass  # outcome was DMed
        await ctx.tick()

    @conductor.command(name="install")
    async def conductor_install(self, ctx: commands.Context, repo_url: str, cog: str = None) -> None:
        """Add a repo, install its cog and load it."""
        await self._cmd(ctx, f"install {repo_url}", self.ops.install(repo_url, cog))

    @conductor.command(name="update")
    async def conductor_update(self, ctx: commands.Context, cog: str) -> None:
        """Pull and reload a cog now."""

        async def op() -> str:
            return (await self.ops.update(cog))[1]

        await self._cmd(ctx, f"update {cog}", op())

    @conductor.command(name="rollback")
    async def conductor_rollback(self, ctx: commands.Context, cog: str, commit: str) -> None:
        """Install a cog at an earlier commit and pin it there."""
        await self._cmd(ctx, f"rollback {cog}", self.ops.rollback(cog, commit))

    @conductor.command(name="unpin")
    async def conductor_unpin(self, ctx: commands.Context, cog: str) -> None:
        """Unpin a rolled-back cog and update it to the latest commit."""
        await self._cmd(ctx, f"unpin {cog}", self.ops.unpin(cog))

    @conductor.command(name="remove")
    async def conductor_remove(self, ctx: commands.Context, cog: str) -> None:
        """Unload, uninstall and remove the repo of a cog."""
        await self._cmd(ctx, f"remove {cog}", self.ops.remove(cog))
