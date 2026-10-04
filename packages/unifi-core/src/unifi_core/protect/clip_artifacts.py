"""Bounded, ephemeral MP4 artifacts served only through authenticated MCP tools.

This module has no MCP or controller dependency. Files are private, never paths
supplied by a caller. A process lock allows safe restart cleanup of this store.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Awaitable, Callable

MAX_CHUNK_BYTES = 65536
_ID = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"[0-9a-f]{64}\.(?:part|mp4)")
Callback = Callable[[int, bytes | None], Awaitable[None]]
Stream = Callable[[Callback], Awaitable[object]]


class ClipError(ValueError):
    """Safe, fixed operational message suitable for a tool error."""


@dataclass(frozen=True)
class ClipLimits:
    max_duration_seconds: int = 30
    max_clip_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    ttl_seconds: int = 600
    max_concurrent: int = 1
    max_artifacts: int = 32
    export_timeout_seconds: int = 120

    def __post_init__(self):
        ceilings = {
            "max_duration_seconds": 120, "max_clip_bytes": 128 * 1024 * 1024,
            "max_total_bytes": 1024 * 1024 * 1024, "ttl_seconds": 3600,
            "max_concurrent": 4, "max_artifacts": 128, "export_timeout_seconds": 300,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"Invalid clip limit: {name}")
        if self.max_total_bytes < self.max_clip_bytes:
            raise ValueError("Clip total quota must accommodate one maximum-size clip")


async def _settle(task):
    """Drain work even after repeated cancellation, then propagate cancellation."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _io(func, *args):
    """Complete an in-flight file operation before cancellation can close its fd."""
    return await _settle(asyncio.create_task(asyncio.to_thread(func, *args)))


def _regular_open(path: Path, flags: int) -> int:
    # O_NOFOLLOW where available; the parent is owned/private and locked.
    if path.is_symlink():
        raise ClipError("Unsafe artifact file")
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ClipError("Unsafe artifact file")
    return fd


def _write(fd: int, payload: bytes):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Short artifact write")
        view = view[written:]


def inspect_mp4(path: Path) -> dict:
    """Validate bounded ISO-BMFF box framing and identify video/audio tracks.

    This is structural validation, NOT a codec decoder or a promise of browser
    codec support. Original bytes are untouched; clients should decode/probe.
    """
    size = path.stat().st_size
    found: set[bytes] = set()
    handlers: list[bytes] = []
    boxes = 0
    with path.open("rb") as file:
        def walk(begin: int, end: int, depth: int):
            nonlocal boxes
            pos = begin
            while pos < end:
                boxes += 1
                if boxes > 10000 or end - pos < 8:
                    raise ClipError("Incomplete or invalid MP4")
                file.seek(pos)
                head = file.read(8)
                length, kind = int.from_bytes(head[:4], "big"), head[4:]
                header = 8
                if length == 1:
                    if end - pos < 16:
                        raise ClipError("Incomplete or invalid MP4")
                    length = int.from_bytes(file.read(8), "big")
                    header = 16
                elif length == 0:
                    length = end - pos
                if length < header or pos + length > end:
                    raise ClipError("Incomplete or invalid MP4")
                if depth == 0:
                    found.add(kind)
                    if kind == b"mdat" and length == header:
                        raise ClipError("Empty MP4 media data")
                if kind in (b"moov", b"trak", b"mdia") and depth < 3:
                    walk(pos + header, pos + length, depth + 1)
                elif kind == b"hdlr" and depth == 3:
                    if length < header + 12:
                        raise ClipError("Invalid MP4 track")
                    file.seek(pos + header + 8)
                    handlers.append(file.read(4))
                pos += length
        walk(0, size, 0)
    if not {b"ftyp", b"moov", b"mdat"} <= found or b"vide" not in handlers:
        raise ClipError("No complete MP4 video recording available")
    return {"container_validated": True, "has_audio": b"soun" in handlers,
            "video_tracks": handlers.count(b"vide"), "audio_tracks": handlers.count(b"soun")}


@dataclass(frozen=True)
class _Artifact:
    path: Path
    scope: tuple
    camera_id: str
    size: int
    sha256: str
    deadline: float
    expires_at: float
    media: dict


class ClipArtifactStore:
    """One private directory per server process/container; never share between sites."""

    def __init__(self, directory: Path, limits: ClipLimits, *, clock=time.monotonic, wall_clock=time.time):
        if not directory.is_absolute():
            raise ValueError("Clip directory must be absolute")
        self.directory = directory
        self.limits = limits
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = asyncio.Lock()
        self._entries: dict[str, _Artifact] = {}
        self._active: set[asyncio.Task] = set()
        self._lock_fd: int | None = None
        self._janitor: asyncio.Task | None = None
        self._closed = False

    def _open_store(self) -> int:
        if self.directory.is_symlink():
            raise ClipError("Clip directory must not be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ClipError("Clip directory must be owner-only (0700)")
        fd = _regular_open(self.directory / ".lock", os.O_CREAT | os.O_RDWR)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Only our flat opaque filenames; never recursive cleanup or symlinks.
            for path in self.directory.iterdir():
                if _FILE.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                    path.unlink()
            return fd
        except BaseException:
            os.close(fd)
            raise ClipError("Clip store is unavailable or already in use") from None

    async def start(self):
        async with self._lock:
            if self._closed:
                raise ClipError("Clip store is closed")
            if self._lock_fd is None:
                # Assign within the worker, so cancellation cannot leak a lock fd.
                def open_store():
                    self._lock_fd = self._open_store()
                try:
                    await _io(open_store)
                finally:
                    if self._lock_fd is not None:
                        self._janitor = asyncio.create_task(self._reap())

    async def _reap(self):
        while True:
            await asyncio.sleep(min(30, self.limits.ttl_seconds))
            try:
                await self.cleanup()
            except OSError:
                # Failed deletion continues consuming quota; never silently reuse space.
                continue

    async def _prune_locked(self):
        for key, entry in list(self._entries.items()):
            if self._clock() >= entry.deadline:
                await _io(entry.path.unlink, True)
                del self._entries[key]

    async def cleanup(self):
        async with self._lock:
            await self._prune_locked()

    async def create(self, *, scope: tuple, camera_id: str, stream: Stream) -> dict:
        await self.start()
        task = asyncio.current_task()
        async with self._lock:
            await self._prune_locked()
            if self._closed:
                raise ClipError("Clip store is closed")
            if len(self._active) >= self.limits.max_concurrent:
                raise ClipError("Clip export is busy; retry after the active export")
            if len(self._entries) + len(self._active) >= self.limits.max_artifacts:
                raise ClipError("Clip artifact count quota reached; wait for expiry")
            used = sum(entry.size for entry in self._entries.values())
            reserved = (len(self._active) + 1) * self.limits.max_clip_bytes
            if used + reserved > self.limits.max_total_bytes:
                raise ClipError("Clip storage quota reached; wait for expiry")
            self._active.add(task)
        artifact_id = token_hex(32)
        partial = self.directory / (artifact_id + ".part")
        ready = self.directory / (artifact_id + ".mp4")
        fd = None
        size, advertised = 0, 0
        digest = hashlib.sha256()
        published = False
        try:
            def open_partial():
                nonlocal fd
                fd = _regular_open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            await _io(open_partial)

            async def receive(total: int, chunk: bytes | None):
                nonlocal size, advertised
                if total:
                    if type(total) is not int or total < 0 or total > self.limits.max_clip_bytes:
                        raise ClipError("Clip exceeds the byte limit")
                    if advertised and advertised != total:
                        raise ClipError("Clip length changed during transfer")
                    advertised = total
                if chunk is None:
                    return
                if not isinstance(chunk, bytes) or len(chunk) > MAX_CHUNK_BYTES:
                    raise ClipError("Invalid or oversized upstream chunk")
                if size + len(chunk) > self.limits.max_clip_bytes:
                    raise ClipError("Clip exceeds the byte limit")
                await _io(_write, fd, chunk)
                digest.update(chunk)
                size += len(chunk)

            async with asyncio.timeout(self.limits.export_timeout_seconds):
                result = await stream(receive)
            # Iterator/output-file APIs return None on SUCCESS. Only bytes received
            # and successful stream completion establish an export, not its return value.
            if result is not None:
                raise ClipError("Controller did not use the streaming export contract")
            closing, fd = fd, None
            await _io(os.close, closing)
            if not size or (advertised and size != advertised):
                raise ClipError("No recording or incomplete clip transfer")
            media = await _io(inspect_mp4, partial)
            async with self._lock:
                if self._closed:
                    raise ClipError("Clip store is closed")
                await _io(os.replace, partial, ready)
                entry = _Artifact(
                    ready, scope, camera_id, size, digest.hexdigest(),
                    self._clock() + self.limits.ttl_seconds,
                    self._wall_clock() + self.limits.ttl_seconds, media,
                )
                self._entries[artifact_id] = entry
                published = True
                return self._metadata(artifact_id, entry)
        finally:
            async def finish():
                if fd is not None:
                    await _io(os.close, fd)
                if not published:
                    await _io(partial.unlink, True)
                    await _io(ready.unlink, True)
                async with self._lock:
                    self._active.discard(task)
            await _settle(asyncio.create_task(finish()))

    @staticmethod
    def _metadata(artifact_id: str, entry: _Artifact) -> dict:
        return {"artifact_id": artifact_id, "size_bytes": entry.size, "sha256": entry.sha256,
                "expires_at_unix": entry.expires_at, "content_type": "video/mp4",
                "max_chunk_bytes": MAX_CHUNK_BYTES, **entry.media}

    async def read(self, *, artifact_id: str, scope: tuple, camera_id: str,
                   offset: int, max_bytes: int) -> dict:
        if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
            raise ClipError("Clip unavailable")
        if type(offset) is not int or offset < 0:
            raise ClipError("Offset must be a non-negative integer")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CHUNK_BYTES:
            raise ClipError("Chunk size must be between 1 and 65536 bytes")
        async with self._lock:
            await self._prune_locked()
            entry = self._entries.get(artifact_id)
            if self._closed or entry is None or entry.scope != scope or entry.camera_id != camera_id:
                raise ClipError("Clip unavailable")
            if offset >= entry.size:
                raise ClipError("Offset must be smaller than the clip size")
            def read_chunk():
                fd = _regular_open(entry.path, os.O_RDONLY)
                try:
                    if os.fstat(fd).st_size != entry.size:
                        raise ClipError("Clip file changed")
                    os.lseek(fd, offset, os.SEEK_SET)
                    data = os.read(fd, min(max_bytes, entry.size - offset))
                    if len(data) != min(max_bytes, entry.size - offset):
                        raise ClipError("Incomplete clip read")
                    return data
                finally:
                    os.close(fd)
            payload = await _io(read_chunk)
            # Expiry is checked again after IO; no lease extends the TTL.
            if self._clock() >= entry.deadline:
                raise ClipError("Clip unavailable")
            return {**self._metadata(artifact_id, entry), "offset": offset,
                    "length": len(payload), "next_offset": offset + len(payload),
                    "eof": offset + len(payload) == entry.size,
                    "chunk_sha256": hashlib.sha256(payload).hexdigest(),
                    "data_base64": base64.b64encode(payload).decode("ascii")}

    async def close(self):
        async with self._lock:
            self._closed = True
            active = list(self._active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._janitor:
            self._janitor.cancel()
            await asyncio.gather(self._janitor, return_exceptions=True)
        async with self._lock:
            for entry in self._entries.values():
                await _io(entry.path.unlink, True)
            self._entries.clear()
            if self._lock_fd is not None:
                await _io(os.close, self._lock_fd)
                self._lock_fd = None
