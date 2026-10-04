import asyncio
import hashlib
import importlib.metadata
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock

from download_clip import download_clip, unwrap_tool_data
from test_private_clips import END, FIXTURES, START
from test_private_clips_transport import TransportTests
from uiprotect.data import Camera
from unifi_core.protect.streaming_client import StreamingProtectApiClient


async def main():
    fixture = TransportTests()
    await fixture.asyncSetUp()
    client = StreamingProtectApiClient("synthetic.invalid", 443, "fixture", "fixture")
    camera = Camera.model_construct(
        id="cam-test", name="Synthetic fixture", channels=[object()]
    )
    camera._api = client
    client._bootstrap = types.SimpleNamespace(
        cameras={camera.id: camera}, auth_user=fixture.user
    )
    fixture.cm.client = client
    results = []
    try:
        for name in ("tone.mp4", "silent.mp4"):
            payload = (FIXTURES / name).read_bytes()

            async def chunks(size, payload=payload):
                for offset in range(0, len(payload), 509):
                    yield payload[offset : offset + 509]

            response = types.SimpleNamespace(
                status=200,
                content_length=len(payload),
                content=types.SimpleNamespace(iter_chunked=chunks),
                close=lambda: None,
            )
            client.request = AsyncMock(return_value=response)
            for mode in ("direct", "execute", "batch"):
                args = {
                    "camera_id": camera.id,
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                }
                if mode == "batch":
                    queued = fixture.normalize(
                        await fixture.server.call_tool(
                            "protect_batch",
                            {
                                "operations": [
                                    {
                                        "tool": "protect_export_clip_artifact",
                                        "arguments": args,
                                    }
                                ]
                            },
                        )
                    )
                    async with asyncio.timeout(15):
                        while (
                            status := await fixture.jobs.status(
                                queued["jobs"][0]["jobId"]
                            )
                        )["status"] == "running":
                            await asyncio.sleep(0.01)
                    assert status["status"] == "done"
                    exported = status
                elif mode == "execute":
                    exported = fixture.normalize(
                        await fixture.server.call_tool(
                            "protect_execute",
                            {"tool": "protect_export_clip_artifact", "arguments": args},
                        )
                    )
                else:
                    exported = await fixture.create()

                async def call(tool, arguments, mode=mode):
                    result = (
                        await fixture.server.call_tool(
                            "protect_execute", {"tool": tool, "arguments": arguments}
                        )
                        if mode == "execute"
                        else await fixture.server.call_tool(tool, arguments)
                    )
                    return fixture.normalize(result)

                path = Path("/out") / f"{mode}-{name}"
                await download_clip(call, exported, camera.id, path, chunk_bytes=1024)
                assert path.read_bytes() == payload
                checksum = hashlib.sha256(payload).hexdigest()
                assert checksum == unwrap_tool_data(exported)["sha256"]
                results.append(
                    {
                        "file": path.name,
                        "size": len(payload),
                        "sha256": checksum,
                        "has_audio": name == "tone.mp4",
                    }
                )
        output = {
            "versions": {
                name: importlib.metadata.version(name)
                for name in [
                    "mcp",
                    "uiprotect",
                    "cryptography",
                    "unifi-core",
                    "unifi-mcp-shared",
                    "unifi-protect-mcp",
                ]
            },
            "results": results,
        }
        Path("/out/roundtrip.json").write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps(output))
    finally:
        await client.close_session()
        await fixture.asyncTearDown()


asyncio.run(main())
