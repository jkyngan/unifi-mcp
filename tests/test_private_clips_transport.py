"""Native locked MCP 2.x dispatcher and UniFi response boundary; synthetic NVR only."""

import asyncio
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_private_clips as core_tests
from download_clip import download_clip, unwrap_tool_data
from test_private_clips import END, START

AVAILABLE = importlib.util.find_spec("mcp") is not None and importlib.metadata.version("mcp").startswith("2.")


@unittest.skipUnless(AVAILABLE, "Native MCP 2.x runtime required; no v1 compatibility shim")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await core_tests.ClipTests.asyncSetUp(self)
        # Restore native uiprotect enums; only the controller/camera is synthetic.
        self.sdk.stop()
        from unifi_core.jobs import JobStore
        from unifi_mcp_shared.meta_tools import register_meta_tools
        from unifi_mcp_shared.permissioned_tool import setup_permissioned_tool
        from unifi_mcp_shared.response_serialization import normalize_call_tool_result

        self.normalize = normalize_call_tool_result
        from unifi_mcp_shared.protocol import create_mcp_tool_adapter
        from unifi_mcp_shared.server import UniFiMCPServer

        self.server = UniFiMCPServer(
            "synthetic-private-clips",
            tools_manifest_path=Path(__file__).parents[1] / "apps/protect/src/unifi_protect_mcp/tools_manifest.json",
        )
        self.server._original_tool = create_mcp_tool_adapter(self.server.tool)
        self.registry = {}

        def register(**kwargs):
            self.registry[kwargs["name"]] = kwargs

        setup_permissioned_tool(
            server=self.server,
            category_map={"recording": "recordings"},
            server_prefix="protect",
            register_tool_fn=register,
            diagnostics_enabled_fn=lambda: False,
            wrap_tool_fn=lambda function, name: function,
            logger=logging.getLogger("fixture"),
        )
        runtime = types.ModuleType("unifi_protect_mcp.runtime")
        runtime.server, runtime.recording_manager = self.server, self.manager
        self.runtime = patch.dict(sys.modules, {"unifi_protect_mcp.runtime": runtime})
        self.runtime.start()
        name = "unifi_protect_mcp.tools.recordings"
        if name in sys.modules:
            self.tools = importlib.reload(sys.modules[name])
        else:
            self.tools = importlib.import_module(name)
        self.jobs = JobStore()

        async def start(handler, args):
            return {"jobId": await self.jobs.start(handler(**args))}

        register_meta_tools(
            server=self.server,
            tool_decorator=self.server._original_tool,
            tool_index_handler=lambda args=None: {},
            start_async_tool=start,
            get_job_status=self.jobs.status,
            register_tool=register,
            prefix="protect",
            server_label="Synthetic Protect",
        )

    async def asyncTearDown(self):
        self.runtime.stop()
        await core_tests.ClipTests.asyncTearDown(self)

    async def create(self):
        result = await self.server.call_tool(
            "protect_export_clip_artifact",
            {"camera_id": "cam-test", "start": START.isoformat(), "end": END.isoformat()},
        )
        return self.normalize(result)

    async def test_actual_sdk_direct_and_execute_reconstruct_original(self):
        for mode in ("direct", "execute"):
            exported = await self.create()

            async def call(name, args):
                if mode == "execute":
                    result = await self.server.call_tool("protect_execute", {"tool": name, "arguments": args})
                else:
                    result = await self.server.call_tool(name, args)
                return self.normalize(result)

            out = Path(self.temp.name) / (mode + ".mp4")
            await download_clip(call, exported, "cam-test", out, chunk_bytes=1024)
            self.assertEqual(out.read_bytes(), self.camera.payload)

    async def test_actual_sdk_then_hiis_text_normalization_preserves_bytes(self):
        exported = await self.create()

        async def call(name, args):
            value = self.normalize(await self.server.call_tool(name, args))
            # Mirrors requestCake. A separate Node test executes the real HIIS helper.
            return {"content": [{"type": "text", "text": json.dumps(value)}]}

        out = Path(self.temp.name) / "hiis.mp4"
        await download_clip(call, exported, "cam-test", out, chunk_bytes=1024)
        self.assertEqual(out.read_bytes(), self.camera.payload)

    async def test_batch_creates_artifact_but_never_retains_chunk_bytes(self):
        queued = self.normalize(
            await self.server.call_tool(
                "protect_batch",
                {
                    "operations": [
                        {
                            "tool": "protect_export_clip_artifact",
                            "arguments": {"camera_id": "cam-test", "start": START.isoformat(), "end": END.isoformat()},
                        }
                    ]
                },
            )
        )
        job_id = queued["jobs"][0]["jobId"]
        async with asyncio.timeout(15):
            while True:
                status = await self.jobs.status(job_id)
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(status["status"], "done")
        meta = unwrap_tool_data(status)
        self.assertNotIn("data_base64", meta)
        rejected = self.normalize(
            await self.server.call_tool(
                "protect_batch",
                {
                    "operations": [
                        {
                            "tool": "protect_read_clip_chunk",
                            "arguments": {"camera_id": "cam-test", "artifact_id": meta["artifact_id"]},
                        }
                    ]
                },
            )
        )
        self.assertFalse(rejected["jobs"])
        self.assertIn("direct or execute", rejected["errors"][0]["error"])
        nested = self.normalize(
            await self.server.call_tool(
                "protect_batch",
                {
                    "operations": [
                        {
                            "tool": "protect_execute",
                            "arguments": {
                                "tool": "protect_read_clip_chunk",
                                "arguments": {"camera_id": "cam-test", "artifact_id": meta["artifact_id"]},
                            },
                        }
                    ]
                },
            )
        )
        async with asyncio.timeout(15):
            while True:
                status = await self.jobs.status(nested["jobs"][0]["jobId"])
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.01)
        self.assertFalse(status["result"]["success"])
        self.assertNotIn("data_base64", json.dumps(status))

    async def test_pydantic_schema_constraints_and_safe_error(self):
        schema = await self.server.list_tools()
        tool = next(t for t in schema if t.name == "protect_read_clip_chunk")
        self.assertEqual(tool.input_schema["properties"]["max_bytes"]["maximum"], 65536)
        result = await self.tools.protect_read_clip_chunk("cam-test", "../secret", max_bytes=1)
        self.assertFalse(result["success"])
        self.assertNotIn("../secret", result["error"])
        for kwargs in ({"offset": True}, {"max_bytes": 65537}, {"offset": -1}):
            with self.assertRaises(Exception):
                await self.server.call_tool(
                    "protect_read_clip_chunk", {"camera_id": "cam-test", "artifact_id": "a" * 64, **kwargs}
                )

    async def test_disabled_feature_before_controller_call(self):
        # Existing policy gates intentionally do not gate read actions. The new
        # feature's explicit default-off switch is independent of those gates.
        self.manager._artifacts = None
        result = await self.create()
        self.assertFalse(result["success"])
        self.assertEqual(self.camera.calls, [])

    async def test_tool_does_not_leak_controller_exception_text(self):
        async def fail(**kwargs):
            raise RuntimeError("fixture-secret-not-for-output")

        self.camera.get_video = fail
        with self.assertLogs("unifi_protect_mcp.tools.recordings", level="WARNING") as logs:
            result = await self.create()
        self.assertFalse(result["success"])
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("fixture-secret", "".join(logs.output))

    async def test_private_manifest_matches_real_registration(self):
        path = Path(__file__).parents[1] / "apps/protect/src/unifi_protect_mcp/tools_manifest.json"
        manifest = json.loads(path.read_text())
        for name in ("protect_export_clip_artifact", "protect_read_clip_chunk"):
            entry = next(tool for tool in manifest["tools"] if tool["name"] == name)
            self.assertEqual(entry["schema"]["input"], self.registry[name]["input_schema"])
            self.assertEqual(entry["schema"]["output"], self.registry[name]["output_schema"])
            self.assertEqual(entry["description"], self.registry[name]["description"])
            self.assertEqual(manifest["module_map"][name], "unifi_protect_mcp.tools.recordings")
        self.assertEqual(manifest["count"], len(manifest["tools"]))

    async def test_native_sdk_stream_and_decoded_video_audio_through_dispatch(self):
        """Native Camera/get_camera_video/streaming; only HTTP response is synthetic."""
        import hashlib
        import shutil
        import subprocess
        from unittest.mock import AsyncMock, MagicMock

        from uiprotect.data import Camera
        from unifi_core.protect.streaming_client import StreamingProtectApiClient

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("Existing FFmpeg/ffprobe required for independent codec verification")
        client = StreamingProtectApiClient("synthetic.invalid", 443, "fixture", "fixture")
        camera = Camera.model_construct(id="cam-test", name="Synthetic fixture", channels=[object()])
        camera._api = client
        client._bootstrap = types.SimpleNamespace(cameras={camera.id: camera}, auth_user=self.user)
        self.cm.client = client

        def decoded(path, audio=False):
            args = ["ffmpeg", "-v", "error", "-i", str(path)]
            args += (
                ["-map", "0:a:0", "-f", "s16le", "-"]
                if audio
                else ["-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            )
            return subprocess.run(args, capture_output=True, check=True, timeout=30).stdout

        try:
            for fixture in ("tone.mp4", "silent.mp4"):
                original = core_tests.FIXTURES / fixture
                payload = original.read_bytes()

                async def chunks(size):
                    for offset in range(0, len(payload), min(size, 509)):
                        yield payload[offset : offset + min(size, 509)]

                response = types.SimpleNamespace(
                    status=200,
                    content_length=len(payload),
                    content=types.SimpleNamespace(iter_chunked=chunks),
                    close=MagicMock(),
                )
                client.request = AsyncMock(return_value=response)
                for mode in ("direct", "execute", "batch"):
                    args = {"camera_id": camera.id, "start": START.isoformat(), "end": END.isoformat()}
                    if mode == "batch":
                        queued = self.normalize(
                            await self.server.call_tool(
                                "protect_batch",
                                {"operations": [{"tool": "protect_export_clip_artifact", "arguments": args}]},
                            )
                        )
                        async with asyncio.timeout(15):
                            while (status := await self.jobs.status(queued["jobs"][0]["jobId"]))["status"] == "running":
                                await asyncio.sleep(0.01)
                        self.assertEqual(status["status"], "done")
                        exported = status
                    elif mode == "execute":
                        exported = self.normalize(
                            await self.server.call_tool(
                                "protect_execute", {"tool": "protect_export_clip_artifact", "arguments": args}
                            )
                        )
                    else:
                        exported = await self.create()

                    async def call(name, arguments):
                        result = (
                            await self.server.call_tool("protect_execute", {"tool": name, "arguments": arguments})
                            if mode == "execute"
                            else await self.server.call_tool(name, arguments)
                        )
                        return self.normalize(result)

                    destination = Path(self.temp.name) / f"{mode}-{fixture}"
                    await download_clip(call, exported, camera.id, destination, chunk_bytes=1024)
                    self.assertEqual(destination.read_bytes(), payload)
                    self.assertEqual(
                        hashlib.sha256(destination.read_bytes()).hexdigest(), unwrap_tool_data(exported)["sha256"]
                    )
                    probe = json.loads(
                        subprocess.run(
                            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(destination)],
                            capture_output=True,
                            check=True,
                            text=True,
                            timeout=30,
                        ).stdout
                    )
                    tracks = [stream["codec_type"] for stream in probe["streams"]]
                    self.assertEqual(tracks.count("video"), 1)
                    self.assertEqual(tracks.count("audio"), int(fixture == "tone.mp4"))
                    video = decoded(destination)
                    self.assertTrue(video)
                    frame_bytes = 128 * 72 * 3
                    self.assertNotEqual(video[:frame_bytes], video[-frame_bytes:])
                    self.assertEqual(video, decoded(original))
                    if fixture == "tone.mp4":
                        audio = decoded(destination, audio=True)
                        self.assertTrue(any(audio))
                        self.assertEqual(audio, decoded(original, audio=True))
                    self.assertNotIn("fps", client.request.call_args.kwargs["params"])
                    self.assertEqual(client.request.call_args.args, ("get", "/proxy/protect/api/video/export"))
                    self.assertTrue(response.close.called)
        finally:
            await client.close_session()

    async def test_actual_hiis_helpers_after_native_mcp_dispatch(self):
        """Optional authorized local HIIS source; no server boot or network."""
        import os
        import shutil
        import subprocess

        source = os.getenv("HIIS_MCP_SOURCE")
        if not source or not shutil.which("node"):
            self.skipTest("Set HIIS_MCP_SOURCE to an authorized checkout; existing Node TypeScript stripping required")
        helper = core_tests.FIXTURES / "hiis_bridge.mjs"
        for mode in ("direct", "execute"):
            exported = await self.create()

            async def call(name, arguments):
                result = (
                    await self.server.call_tool("protect_execute", {"tool": name, "arguments": arguments})
                    if mode == "execute"
                    else await self.server.call_tool(name, arguments)
                )
                value = self.normalize(result)
                for wrapper in ("requestCake", "asImageContent", "asFileContent"):
                    process = subprocess.run(
                        ["node", "--disable-warning=ExperimentalWarning", str(helper)],
                        input=json.dumps({"mode": wrapper, "value": value}),
                        text=True,
                        capture_output=True,
                        check=True,
                        timeout=15,
                    )
                    value = json.loads(process.stdout)
                return value

            destination = Path(self.temp.name) / ("actual-hiis-" + mode + ".mp4")
            await download_clip(call, exported, "cam-test", destination, chunk_bytes=8192)
            self.assertEqual(destination.read_bytes(), self.camera.payload)

    async def test_native_uiprotect_stream_closes_on_failure_and_cancellation(self):
        from unittest.mock import MagicMock

        from unifi_core.protect.clip_artifacts import ClipError
        from unifi_core.protect.streaming_client import StreamingProtectApiClient

        client = StreamingProtectApiClient("synthetic.invalid", 443, "fixture", "fixture")
        try:
            for error in (None, ClipError("limit"), asyncio.CancelledError()):
                delivered = []

                async def chunks(size):
                    yield b"synthetic"

                response = types.SimpleNamespace(
                    content_length=9,
                    content=types.SimpleNamespace(iter_chunked=chunks),
                    close=MagicMock(),
                )

                async def callback(total, chunk):
                    delivered.append((total, chunk))
                    if chunk is not None and error is not None:
                        raise error

                if error is None:
                    await client._stream_response(response, 65536, callback)
                else:
                    with self.assertRaises(type(error)):
                        await client._stream_response(response, 65536, callback)
                self.assertEqual(delivered, [(9, None), (9, b"synthetic")])
                response.close.assert_called_once()
        finally:
            await client.close_session()
