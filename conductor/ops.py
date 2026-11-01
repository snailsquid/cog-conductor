"""Cog lifecycle operations on top of Downloader's internals (Red 3.5.24).

Every public coroutine here mirrors a `[p]cog ...` / `[p]repo ...` code path but without a
command context. Callers must serialise them (the Conductor holds one asyncio.Lock).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from redbot.cogs.downloader import errors as dl_errors

if TYPE_CHECKING:
    from redbot.cogs.downloader.downloader import Downloader
    from redbot.cogs.downloader.installable import InstalledModule
    from redbot.cogs.downloader.repo_manager import Repo
    from redbot.core.bot import Red

REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")


class OpError(Exception):
    """An operation failed; the message is user-facing."""


class LoadError(OpError):
    """A cog was installed but failed to load."""


def short(sha: Optional[str]) -> str:
    return (sha or "?")[:7]


def names_from_url(url: str, cog: Optional[str] = None) -> Tuple[str, str]:
    """`https://github.com/me/cog-water-log` -> (`cog-water-log`, `water_log`)."""
    path = urlparse(url).path.rstrip("/")
    repo_name = path.rsplit("/", 1)[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    if not repo_name or not REPO_NAME_RE.match(repo_name) or repo_name.startswith("."):
        raise OpError(f"Can't derive a repo name from `{url}`.")
    if cog is None:
        cog = repo_name[4:] if repo_name.startswith("cog-") else repo_name
        cog = cog.replace("-", "_").replace(".", "_")
    if not cog.isidentifier():
        raise OpError(f"`{cog}` is not a valid cog package name; pass `cog` explicitly.")
    return repo_name, cog


@dataclass
class CogState:
    name: str
    repo: str
    url: Optional[str]
    branch: Optional[str]
    commit: str
    head: Optional[str]
    pinned: bool
    loaded: bool

    def to_json(self) -> Dict[str, object]:
        return self.__dict__.copy()


class Ops:
    def __init__(self, bot: Red, bad_commits: Dict[str, str]) -> None:
        self.bot = bot
        # cog name -> commit that failed to load; skipped by update until HEAD moves on.
        self.bad_commits = bad_commits

    # ---------------------------------------------------------------- helpers

    async def downloader(self) -> Downloader:
        dl = self.bot.get_cog("Downloader")
        if dl is None:
            res = await self.bot.get_cog("Core")._load(["downloader"])
            if "downloader" not in res["loaded_packages"]:
                raise OpError(f"Could not load Downloader: {res}")
            dl = self.bot.get_cog("Downloader")
        await dl._ready.wait()
        if dl._ready_raised:
            raise OpError("Downloader failed to initialise; check the bot logs.")
        return dl

    async def installed(self, name: str) -> InstalledModule:
        dl = await self.downloader()
        for module in await dl.installed_cogs():
            if module.name == name:
                if module.repo is None:
                    raise OpError(f"`{name}` is installed but its repo is gone.")
                return module
        raise OpError(f"`{name}` is not installed.")

    async def load(self, name: str) -> None:
        """(Re)load a package; raise LoadError with the reason on failure."""
        res = await self.bot.get_cog("Core")._reload([name])
        if name in res["loaded_packages"]:
            return
        reason = res["failed_with_reason_packages"].get(name)
        if reason is None and name in res["failed_packages"]:
            reason = getattr(self.bot, "_last_exception", None) or "unknown error (see logs)"
        if reason is None and name in res["notfound_packages"]:
            reason = "package not found in any cog path"
        raise LoadError(f"`{name}` failed to load:\n{reason or res}")

    async def _install_at(self, repo: Repo, name: str, commit: Optional[str], pinned: bool) -> str:
        """Copy cog `name` from `repo` at `commit` (None = branch HEAD) into the install path."""
        dl = await self.downloader()
        async with repo.checkout(commit, exit_to_rev=repo.branch):
            # Not _filter_incorrect_cogs_by_names: it refuses cogs that are already installed,
            # which is exactly what revert/rollback reinstall.
            cog = next((c for c in repo.available_cogs if c.name == name), None)
            if cog is None:
                raise OpError(f"No cog `{name}` in `{repo.name}` at `{short(repo.commit)}`.")
            cogs, message = dl._filter_incorrect_cogs([cog])
            if not cogs:
                raise OpError(message.strip())
            failed_reqs = await dl._install_requirements(cogs)
            if failed_reqs:
                raise OpError(f"Failed to install requirements: {', '.join(failed_reqs)}")
            installed, failed = await dl._install_cogs(cogs)
            if failed or not installed:
                raise OpError(f"Failed to copy `{name}` into the cog install path.")
        for module in installed:
            module.pinned = pinned
        await dl._save_to_installed(installed)
        return installed[0].commit

    # ---------------------------------------------------------------- operations

    async def status(self) -> List[CogState]:
        dl = await self.downloader()
        out = []
        for m in await dl.installed_cogs():
            out.append(
                CogState(
                    name=m.name,
                    repo=m.repo_name,
                    url=m.repo.clean_url if m.repo else None,
                    branch=m.repo.branch if m.repo else None,
                    commit=m.commit,
                    head=m.repo.commit if m.repo else None,
                    pinned=m.pinned,
                    loaded=m.name in self.bot.extensions,
                )
            )
        return sorted(out, key=lambda s: s.name)

    async def install(self, url: str, cog: Optional[str] = None) -> str:
        repo_name, name = names_from_url(url, cog)
        dl = await self.downloader()
        existing = next((m for m in await dl.installed_cogs() if m.name == name), None)
        if existing is not None:
            if existing.repo_name != repo_name:
                raise OpError(f"A cog named `{name}` is already installed from `{existing.repo_name}`.")
            changed, msg = await self.update(name)
            return f"`{name}` was already installed. {msg}"
        repo = dl._repo_manager.get_repo(repo_name)
        try:
            if repo is None:
                repo = await dl._repo_manager.add_repo(url=url, name=repo_name)
            else:
                await repo.update()
        except dl_errors.AuthenticationError:
            raise OpError(f"Authentication failed or repo not found: {url}")
        except dl_errors.GitException as e:
            raise OpError(f"git failed for {url}: {e}")
        commit = await self._install_at(repo, name, None, pinned=False)
        await self.load(name)
        return f"✅ `{name}` installed at `{short(commit)}` and loaded."

    async def update(self, name: str) -> Tuple[bool, str]:
        """Pull the cog's repo and reinstall + reload if the cog changed."""
        if name == "conductor":
            raise OpError("The conductor doesn't update itself; use `[p]cog update conductor`.")
        dl = await self.downloader()
        module = await self.installed(name)
        if module.pinned:
            return False, f"`{name}` is pinned at `{short(module.commit)}`; skipped."
        repo = module.repo
        old = module.commit
        try:
            await repo.update()
        except dl_errors.GitException as e:
            raise OpError(f"git pull failed for `{repo.name}`: {e}")
        new = repo.commit
        if new == old:
            return False, f"`{name}` already at `{short(old)}`."
        if self.bad_commits.get(name) == new:
            return False, f"`{name}`: HEAD `{short(new)}` failed to load before; waiting for a new push."

        cogs_to_update, _ = await dl._available_updates([module])
        targets = [c for c in cogs_to_update if c.name == name]
        if not targets:
            # Nothing inside the cog folder changed; Downloader already bumped the commit.
            return False, f"`{name}`: no changes in the cog folder (`{short(old)}` → `{short(new)}`)."
        failed_reqs = await dl._install_requirements(targets)
        if failed_reqs:
            raise OpError(f"Failed to install requirements: {', '.join(failed_reqs)}")
        installed, failed = await dl._install_cogs(targets)
        if failed or not installed:
            raise OpError(f"Failed to copy `{name}` into the cog install path.")
        await dl._save_to_installed(installed)

        try:
            await self.load(name)
        except LoadError as e:
            self.bad_commits[name] = new
            try:
                await self._install_at(repo, name, old, pinned=False)
                await self.load(name)
                revert = f"Reverted to `{short(old)}`, which is running again."
            except OpError as revert_err:
                revert = f"Revert to `{short(old)}` also failed:\n{revert_err}"
            raise OpError(f"❌ `{name}` `{short(new)}` failed to load. {revert}\n{e}")
        self.bad_commits.pop(name, None)
        return True, f"✅ `{name}` updated `{short(old)}` → `{short(new)}`"

    async def rollback(self, name: str, rev: str) -> str:
        module = await self.installed(name)
        repo = module.repo
        old = module.commit
        try:
            await repo.update()
            commit = await repo.get_full_sha1(rev)
        except dl_errors.AmbiguousRevision:
            raise OpError(f"`{rev}` is ambiguous in `{repo.name}`; use a longer sha.")
        except dl_errors.UnknownRevision:
            raise OpError(f"No revision `{rev}` in `{repo.name}`.")
        except dl_errors.GitException as e:
            raise OpError(f"git failed for `{repo.name}`: {e}")
        await self._install_at(repo, name, commit, pinned=True)
        await self.load(name)
        return (
            f"⏪ `{name}` rolled back `{short(old)}` → `{short(commit)}` and pinned. "
            f"Unpin to resume auto-updates."
        )

    async def unpin(self, name: str) -> str:
        dl = await self.downloader()
        module = await self.installed(name)
        if module.pinned:
            module.pinned = False
            await dl._save_to_installed([module])
        self.bad_commits.pop(name, None)
        changed, msg = await self.update(name)
        return f"📌 `{name}` unpinned. {msg}"

    async def remove(self, name: str) -> str:
        dl = await self.downloader()
        module = await self.installed(name)
        await self.bot.get_cog("Core")._unload([name])
        path = (await dl.cog_install_path()) / name
        if path.exists():
            await dl._delete_cog(path)
        await dl._remove_from_installed([module])
        repo_name = module.repo_name
        if not any(m.repo_name == repo_name for m in await dl.installed_cogs()):
            try:
                await dl._repo_manager.delete_repo(repo_name)
            except dl_errors.MissingGitRepo:
                pass
        self.bad_commits.pop(name, None)
        return f"🗑️ `{name}` unloaded, uninstalled, and repo `{repo_name}` removed."

    async def cogs_for_push(self, repo_name: str, full_name: str, ref: str) -> List[str]:
        """Installed, unpinned cogs that a push to `full_name`@`ref` should update."""
        dl = await self.downloader()
        names = []
        suffix = "/" + full_name.lower()
        for m in await dl.installed_cogs():
            repo = m.repo
            if repo is None or m.repo_name != repo_name:
                continue
            url = repo.clean_url.lower().rstrip("/")
            if url.endswith(".git"):
                url = url[:-4]
            if not url.endswith(suffix):
                continue
            if repo.branch and ref != f"refs/heads/{repo.branch}":
                continue
            names.append(m.name)
        return names
