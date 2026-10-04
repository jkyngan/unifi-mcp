"""Reconstruct an original clip through an existing authorized MCP call adapter.

Import download_clip and supply async call_tool(name, arguments). This module
does not create credentials, connect to a new service, or print media payloads.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

_HEX = re.compile(r"[0-9a-f]{64}")


async def _settle_write(write):
    """Do not close the file while a worker survives repeated cancellation."""
    cancelled = False
    while True:
        try:
            written = await asyncio.shield(write)
            break
        except asyncio.CancelledError:
            if write.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return written


def unwrap_tool_data(result):
    """Handle direct/execute results, completed jobs, and a text JSON bridge."""
    for _ in range(8):
        if not isinstance(result, (dict, list, tuple)):
            if getattr(result, "isError", False) or getattr(result, "is_error", False):
                raise ValueError("Clip tool failed")
            structured = getattr(result, "structured_content", None)
            if structured is None:
                structured = getattr(result, "structuredContent", None)
            result = structured if structured is not None else getattr(result, "content", None)
        elif isinstance(result, tuple) and len(result) == 2:
            result = result[1] if result[1] is not None else result[0]
        elif isinstance(result, list):
            if len(result) != 1:
                raise ValueError("Unexpected clip response")
            block = result[0]
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if not isinstance(text, str) or len(text) > 256000:
                raise ValueError("Unexpected clip response")
            result = json.loads(text)
        else:
            if result.get("isError") or result.get("success") is False:
                raise ValueError("Clip tool failed")
            if result.get("success") is True and isinstance(result.get("data"), dict):
                return result["data"]
            if result.get("status") == "done":
                result = result.get("result")
            elif isinstance(result.get("structuredContent"), dict):
                result = result["structuredContent"]
            elif "content" in result:
                result = result["content"]
            else:
                raise ValueError("Unexpected clip response")
    raise ValueError("Clip response nesting limit exceeded")


async def download_clip(call_tool, export_result, camera_id: str, destination: Path, *, chunk_bytes: int = 32768):
    """Fetch bounded chunks, validate identity/offsets/hashes and publish atomically.

    destination is selected by local trusted code, never by the tool response.
    Existing files are never overwritten. Any failure removes the partial file.
    """
    meta = unwrap_tool_data(export_result)
    size, artifact_id, expected = meta.get("size_bytes"), meta.get("artifact_id"), meta.get("sha256")
    if (
        type(size) is not int
        or not 0 < size <= 128 * 1024 * 1024
        or not isinstance(artifact_id, str)
        or not _HEX.fullmatch(artifact_id)
        or not isinstance(expected, str)
        or not _HEX.fullmatch(expected)
        or meta.get("content_type") != "video/mp4"
    ):
        raise ValueError("Invalid clip metadata")
    if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 65536:
        raise ValueError("Invalid chunk size")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Clip destination already exists")
    fd, temporary = tempfile.mkstemp(prefix=".clip-download-", suffix=".part", dir=destination.parent)
    partial = Path(temporary)
    digest = hashlib.sha256()
    offset = 0
    try:
        with os.fdopen(fd, "wb") as file:
            while offset < size:
                requested = min(chunk_bytes, size - offset)
                raw = await call_tool(
                    "protect_read_clip_chunk",
                    {
                        "camera_id": camera_id,
                        "artifact_id": artifact_id,
                        "offset": offset,
                        "max_bytes": requested,
                    },
                )
                chunk = unwrap_tool_data(raw)
                if (
                    chunk.get("artifact_id") != artifact_id
                    or chunk.get("size_bytes") != size
                    or chunk.get("sha256") != expected
                    or chunk.get("offset") != offset
                    or chunk.get("content_type") != "video/mp4"
                ):
                    raise ValueError("Clip identity or offset changed")
                encoded = chunk.get("data_base64")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((requested + 2) // 3):
                    raise ValueError("Invalid encoded chunk size")
                payload = base64.b64decode(encoded, validate=True)
                if (
                    len(payload) != requested
                    or chunk.get("length") != len(payload)
                    or chunk.get("next_offset") != offset + len(payload)
                    or chunk.get("eof") is not (offset + len(payload) == size)
                    or hashlib.sha256(payload).hexdigest() != chunk.get("chunk_sha256")
                ):
                    raise ValueError("Clip chunk integrity check failed")
                # A bounded write is offloaded; await completion before closing on abort.
                write = asyncio.create_task(asyncio.to_thread(file.write, payload))
                written = await _settle_write(write)
                if written != len(payload):
                    raise OSError("Short clip write")
                digest.update(payload)
                offset += len(payload)
            if digest.hexdigest() != expected:
                raise ValueError("Clip file checksum mismatch")
            file.flush()
            os.fsync(file.fileno())
        # Hard-link publication is atomic and fails if destination already exists.
        # Both files are on the same filesystem; no replace/overwrite operation.
        os.link(partial, destination)
        return {"path": str(destination), "size_bytes": size, "sha256": expected}
    finally:
        partial.unlink(missing_ok=True)
