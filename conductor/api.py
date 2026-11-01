"""aiohttp server: bearer-token JSON API + GitHub push webhook."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from aiohttp import web

from .ops import OpError

if TYPE_CHECKING:
    from .conductor import Conductor

log = logging.getLogger("red.conductor.api")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
PUBLIC_PATHS = {"/github", "/health"}


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class Api:
    def __init__(self, conductor: Conductor, token: str, webhook_secret: str) -> None:
        self.conductor = conductor
        self.token = token.encode()
        self.webhook_secret = webhook_secret.encode()
        self.runner: web.AppRunner | None = None

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth], client_max_size=4 * 1024 * 1024)
        app.add_routes(
            [
                web.get("/health", self.health),
                web.get("/cogs", self.list_cogs),
                web.post("/cogs", self.install),
                web.post("/cogs/{name}/update", self.update),
                web.post("/cogs/{name}/rollback", self.rollback),
                web.post("/cogs/{name}/unpin", self.unpin),
                web.delete("/cogs/{name}", self.remove),
                web.post("/github", self.github),
            ]
        )
        return app

    async def start(self, host: str, port: int) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port, reuse_address=True)
        await site.start()
        log.info("Conductor API listening on %s:%s", host, port)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # ---------------------------------------------------------------- middleware

    @web.middleware
    async def auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path not in PUBLIC_PATHS:
            header = request.headers.get("Authorization", "")
            scheme, _, supplied = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                supplied.strip().encode(), self.token
            ):
                return _error(401, "unauthorized")
        return await handler(request)

    async def _run(self, label: str, coro: Awaitable[str]) -> web.Response:
        """Run one operation through the conductor (lock + owner DM) and map errors."""
        try:
            message = await self.conductor.run_op(label, coro)
        except OpError as e:
            return _error(400, str(e))
        except Exception as e:  # already DMed with traceback by run_op
            return _error(500, f"{type(e).__name__}: {e}")
        return web.json_response({"ok": True, "message": message})

    # ---------------------------------------------------------------- routes

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def list_cogs(self, request: web.Request) -> web.Response:
        states = await self.conductor.ops.status()
        return web.json_response({"ok": True, "cogs": [s.to_json() for s in states]})

    async def install(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            repo = body["repo"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return _error(400, 'expected JSON body {"repo": "<url>"}')
        cog = body.get("cog")
        return await self._run(f"install {repo}", self.conductor.ops.install(repo, cog))

    async def update(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]

        async def op() -> str:
            changed, message = await self.conductor.ops.update(name)
            return message

        return await self._run(f"update {name}", op())

    async def rollback(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            commit = (await request.json())["commit"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return _error(400, 'expected JSON body {"commit": "<sha>"}')
        return await self._run(f"rollback {name}", self.conductor.ops.rollback(name, commit))

    async def unpin(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return await self._run(f"unpin {name}", self.conductor.ops.unpin(name))

    async def remove(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return await self._run(f"remove {name}", self.conductor.ops.remove(name))

    async def github(self, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            log.warning("Rejected webhook with bad signature from %s", request.remote)
            return _error(401, "bad signature")

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return web.json_response({"ok": True, "message": "pong"})
        if event != "push":
            return web.json_response({"ok": True, "message": f"ignored event {event!r}"})
        try:
            payload = json.loads(body)
            repo_name = payload["repository"]["name"]
            full_name = payload["repository"]["full_name"]
            ref = payload["ref"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return _error(400, "malformed push payload")
        if payload.get("deleted"):
            return web.json_response({"ok": True, "message": "ignored branch deletion"})

        names = await self.conductor.ops.cogs_for_push(repo_name, full_name, ref)
        if not names:
            return web.json_response({"ok": True, "message": "no matching installed cogs"})
        # GitHub times out after 10 s; apply in the background and report by DM.
        self.conductor.schedule_push_update(names)
        return web.json_response({"ok": True, "message": f"updating {names}"}, status=202)
