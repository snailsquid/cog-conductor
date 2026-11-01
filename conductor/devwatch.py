"""Dev mode: poll mounted cog folders and hot-reload loaded cogs when their files change."""
from __future__ import annotations

import asyncio
import logging
import os
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Set, Tuple

from .ops import OpError

if TYPE_CHECKING:
    from .conductor import Conductor

log = logging.getLogger("red.conductor.dev")

POLL_SECONDS = 0.5
WATCHED_SUFFIXES = {".py", ".json"}


def _packages(root: Path) -> Iterator[Path]:
    """Package dirs at `root/<pkg>` or `root/<repo>/<pkg>` (the cog-template layout)."""
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "__init__.py").is_file():
            yield child
            continue
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_dir() and (grandchild / "__init__.py").is_file():
                yield grandchild


def _fingerprint(pkg: Path) -> Tuple[Tuple[str, int, int], ...]:
    files = []
    for f in pkg.rglob("*"):
        if "__pycache__" in f.parts or f.suffix not in WATCHED_SUFFIXES or not f.is_file():
            continue
        st = f.stat()
        files.append((str(f.relative_to(pkg)), st.st_mtime_ns, st.st_size))
    return tuple(sorted(files))


def _syntax_errors(pkg: Path) -> List[str]:
    errors = []
    for f in sorted(pkg.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        try:
            compile(f.read_bytes(), str(f), "exec")
        except SyntaxError as e:
            errors.append("".join(traceback.format_exception_only(type(e), e)).strip())
    return errors


class DevWatcher:
    def __init__(self, conductor: Conductor, roots: List[Path]) -> None:
        self.conductor = conductor
        self.bot = conductor.bot
        self.roots = roots
        self.fingerprints: Dict[str, tuple] = {}
        self.added_paths: Set[Path] = set()
        # Packages we already tried to auto-load once (so a broken one isn't retried every tick).
        self.autoload_tried: Set[str] = set()
        self.autoload = os.environ.get("CONDUCTOR_AUTOLOAD", "1") != "0"

    async def run(self) -> None:
        await self.bot.wait_until_red_ready()
        log.info("Dev watcher started on %s", ", ".join(map(str, self.roots)))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Dev watcher tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _ensure_path(self, path: Path) -> None:
        if path in self.added_paths:
            return
        cog_mgr = self.bot._cog_mgr
        if path.resolve() not in [p.resolve() for p in await cog_mgr.user_defined_paths()]:
            await cog_mgr.add_path(path)
            log.info("Added cog path %s", path)
        self.added_paths.add(path)

    async def tick(self) -> None:
        changed: List[Path] = []
        for root in self.roots:
            for pkg in _packages(root):
                if pkg.name == "conductor":
                    continue  # never hot-reload ourselves
                await self._ensure_path(pkg.parent)
                fp = await asyncio.to_thread(_fingerprint, pkg)
                previous = self.fingerprints.get(pkg.name)
                self.fingerprints[pkg.name] = fp
                if previous is not None and previous != fp:
                    changed.append(pkg)
                elif (
                    self.autoload
                    and pkg.name not in self.bot.extensions
                    and pkg.name not in self.autoload_tried
                ):
                    self.autoload_tried.add(pkg.name)
                    await self.reload(pkg, autoload=True)
        if not changed:
            return
        # Let editors finish writing (save = truncate + write, or several files at once).
        await asyncio.sleep(0.3)
        for pkg in changed:
            self.fingerprints[pkg.name] = await asyncio.to_thread(_fingerprint, pkg)
            await self.reload(pkg)

    async def reload(self, pkg: Path, autoload: bool = False) -> None:
        name = pkg.name
        if name not in self.bot.extensions and not autoload:
            if self.autoload:
                # Was broken on first sight; a change is the cue to try again.
                autoload = True
            else:
                log.debug("%s changed but is not loaded; skipping", name)
                return
        errors = await asyncio.to_thread(_syntax_errors, pkg)
        if errors:
            await self.conductor.notify(
                f"❌ `{name}` has a syntax error; the old version keeps running.\n"
                + "\n".join(f"```py\n{e[:900]}\n```" for e in errors[:3])
            )
            return
        try:
            async with self.conductor.lock:
                await self.conductor.ops.load(name)
        except OpError as e:
            await self.conductor.notify(f"❌ dev {'load' if autoload else 'reload'} of `{name}` failed.\n{e}")
            return
        log.info("%s %s", "Loaded" if autoload else "Reloaded", name)
