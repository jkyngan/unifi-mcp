"""Actual installed MCP 1.30/Pydantic tool dispatch, with synthetic NVR.
The deployment uses MCP 2.1.1; this suite does not claim that version was tested.
"""
import asyncio
import importlib
import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from download_clip import download_clip, unwrap_tool_data
import test_private_clips as core_tests
from test_private_clips import START, END

AVAILABLE = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("pydantic") is not None


@unittest.skipUnless(AVAILABLE, "Existing MCP/Pydantic runtime required")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await core_tests.ClipTests.asyncSetUp(self)
        from mcp.server.fastmcp import FastMCP, Context
        # Only the SDK v2 import spelling differs for these handlers. No server
        # auth/config/bootstrap is run or substituted in production code.
        shim = types.ModuleType("mcp.server.mcpserver")
        shim.Context = Context
        self.alias = patch.dict(sys.modules, {"mcp.server.mcpserver": shim})
        self.alias.start()
        from unifi_mcp_shared.permissioned_tool import setup_permissioned_tool
        from unifi_mcp_shared.meta_tools import register_meta_tools
        from unifi_mcp_shared.response_serialization import normalize_call_tool_result
        from unifi_core.jobs import JobStore

        self.normalize = normalize_call_tool_result
        self.server = FastMCP("synthetic-private-clips")
        self.server._original_tool = self.server.tool
        self.registry = {}
        def register(**kwargs):
            self.registry[kwargs["name"]] = kwargs
        setup_permissioned_tool(server=self.server, category_map={"recording": "recordings"},
            server_prefix="protect", register_tool_fn=register, diagnostics_enabled_fn=lambda: False,
            wrap_tool_fn=lambda function, name: function, logger=logging.getLogger("fixture"))
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
        native = self.server
        class Adapter:
            async def call_tool(self, name, arguments, context=None):
                return await native.call_tool(name, arguments)
        register_meta_tools(server=Adapter(), tool_decorator=self.server._original_tool,
            tool_index_handler=lambda args=None: {}, start_async_tool=start, get_job_status=self.jobs.status,
            register_tool=register, prefix="protect", server_label="Synthetic Protect")

    async def asyncTearDown(self):
        self.runtime.stop()
        self.alias.stop()
        await core_tests.ClipTests.asyncTearDown(self)

    async def create(self):
        result = await self.server.call_tool("protect_export_clip_artifact",
            {"camera_id": "cam-test", "start": START.isoformat(), "end": END.isoformat()})
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
        queued = self.normalize(await self.server.call_tool("protect_batch", {"operations": [{
            "tool": "protect_export_clip_artifact",
            "arguments": {"camera_id": "cam-test", "start": START.isoformat(), "end": END.isoformat()},
        }]}))
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
        rejected = self.normalize(await self.server.call_tool("protect_batch", {"operations": [{
            "tool": "protect_read_clip_chunk",
            "arguments": {"camera_id": "cam-test", "artifact_id": meta["artifact_id"]},
        }]}))
        self.assertFalse(rejected["jobs"])
        self.assertIn("direct or execute", rejected["errors"][0]["error"])
        nested = self.normalize(await self.server.call_tool("protect_batch", {"operations": [{
            "tool": "protect_execute", "arguments": {"tool": "protect_read_clip_chunk",
                "arguments": {"camera_id": "cam-test", "artifact_id": meta["artifact_id"]}},
        }]}))
        async with asyncio.timeout(15):
            while True:
                status = await self.jobs.status(nested["jobs"][0]["jobId"])
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.01)
        self.assertFalse(status["result"]["success"])
        self.assertNotIn("data_base64", json.dumps(status))

    async def test_pydantic_schema_constraints_and_safe_error(self):
        schema = (await self.server.list_tools())
        tool = next(t for t in schema if t.name == "protect_read_clip_chunk")
        self.assertEqual(tool.inputSchema["properties"]["max_bytes"]["maximum"], 65536)
        result = await self.tools.protect_read_clip_chunk("cam-test", "../secret", max_bytes=1)
        self.assertFalse(result["success"])
        self.assertNotIn("../secret", result["error"])
        for kwargs in ({"offset": True}, {"max_bytes": 65537}, {"offset": -1}):
            with self.assertRaises(Exception):
                await self.server.call_tool("protect_read_clip_chunk",
                    {"camera_id": "cam-test", "artifact_id": "a" * 64, **kwargs})

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
