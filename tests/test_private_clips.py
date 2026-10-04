"""Synthetic private-clip safety and file reconstruction tests (stdlib unittest)."""
import asyncio
import base64
import hashlib
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from download_clip import download_clip, unwrap_tool_data
from unifi_core.protect.clip_artifacts import ClipArtifactStore, ClipError, ClipLimits, inspect_mp4
from unifi_core.protect.managers.recording_manager import RecordingManager

FIXTURES = Path(__file__).parent / "fixtures/clips"
START = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=1)
SCOPE = ("test-controller", 443, "fixture-site", "fixture-user")


class FakeCamera:
    id = "cam-test"
    name = "Synthetic fixture"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get_video(self, **kwargs):
        self.calls.append(kwargs)
        callback = kwargs.get("iterator_callback")
        if callback is None:
            return self.payload  # legacy metadata export
        await callback(len(self.payload), None)
        for pos in range(0, len(self.payload), 509):
            await callback(len(self.payload), self.payload[pos:pos + 509])
        return None  # actual uiprotect iterator/output_file success convention


class ClipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "store"
        self.limits = ClipLimits(max_clip_bytes=100000, max_total_bytes=400000)
        self.store = ClipArtifactStore(self.directory, self.limits)
        self.camera = FakeCamera((FIXTURES / "tone.mp4").read_bytes())
        self.allow = True
        self.user = types.SimpleNamespace(id="fixture-user", can=lambda *args: self.allow)
        self.cm = types.SimpleNamespace(host="test-controller", port=443, site="fixture-site",
            client=types.SimpleNamespace(bootstrap=types.SimpleNamespace(
                cameras={"cam-test": self.camera}, auth_user=self.user)))
        self.manager = RecordingManager(self.cm, self.store)
        data = types.ModuleType("uiprotect.data")
        data.ModelType = types.SimpleNamespace(CAMERA="camera")
        data.PermissionNode = types.SimpleNamespace(READ_MEDIA="readmedia")
        self.sdk = patch.dict(sys.modules, {"uiprotect.data": data})
        self.sdk.start()

    async def asyncTearDown(self):
        self.sdk.stop()
        await self.store.close()
        self.temp.cleanup()

    async def export(self):
        return await self.manager.export_clip_artifact("cam-test", START, END)

    async def read(self, meta, **kwargs):
        return await self.manager.read_clip_chunk("cam-test", meta["artifact_id"], **kwargs)

    def media_files(self):
        return [p for p in self.directory.glob("*") if p.suffix in {".part", ".mp4"}]

    async def test_tone_and_silent_reconstruct_exact_original_files(self):
        for name, audio in (("tone.mp4", True), ("silent.mp4", False)):
            self.camera.payload = (FIXTURES / name).read_bytes()
            meta = await self.export()
            self.assertEqual(meta["has_audio"], audio)
            self.assertEqual(meta["size_bytes"], len(self.camera.payload))
            self.assertNotIn("fps", self.camera.calls[-1])
            self.assertIn("iterator_callback", self.camera.calls[-1])
            calls = []
            async def call_tool(name, arguments):
                self.assertEqual(name, "protect_read_clip_chunk")
                calls.append(arguments["offset"])
                chunk = await self.manager.read_clip_chunk(**arguments)
                return {"success": True, "data": chunk}
            out = Path(self.temp.name) / name
            result = await download_clip(call_tool, {"success": True, "data": meta}, "cam-test", out, chunk_bytes=311)
            self.assertEqual(out.read_bytes(), self.camera.payload)
            self.assertEqual(result["sha256"], hashlib.sha256(self.camera.payload).hexdigest())
            self.assertGreater(len(calls), 1)
            self.assertEqual(inspect_mp4(out)["has_audio"], audio)

    async def test_legacy_metadata_export_unchanged_and_feature_disabled(self):
        legacy = RecordingManager(self.cm)
        result = await legacy.export_clip("cam-test", START, END)
        self.assertTrue(result["exported"])
        self.assertEqual(result["size_bytes"], len(self.camera.payload))
        with self.assertRaises(ClipError):
            await legacy.export_clip_artifact("cam-test", START, END)

    async def test_denied_export_makes_no_artifact(self):
        self.allow = False
        with self.assertRaises(ClipError):
            await self.export()
        self.assertEqual(self.camera.calls, [])
        self.assertEqual(self.media_files(), [])

    async def test_permission_rechecked_on_every_chunk(self):
        meta = await self.export()
        await self.read(meta, max_bytes=1)
        self.allow = False
        with self.assertRaises(ClipError):
            await self.read(meta, offset=1)

    async def test_cross_site_principal_and_camera_denied(self):
        meta = await self.export()
        for scope in (("other", 443, "fixture-site", "fixture-user"), ("test-controller", 443, "other", "fixture-user"),
                      ("test-controller", 443, "fixture-site", "other")):
            with self.assertRaises(ClipError):
                await self.store.read(artifact_id=meta["artifact_id"], scope=scope, camera_id="cam-test",
                                      offset=0, max_bytes=1)
        with self.assertRaises(ClipError):
            await self.store.read(artifact_id=meta["artifact_id"], scope=SCOPE, camera_id="other",
                                  offset=0, max_bytes=1)
        self.user.id = "changed-principal"
        with self.assertRaises(ClipError):
            await self.read(meta)

    async def test_duration_channel_and_timezone_bounds(self):
        for start, end, channel in ((START, START, 0), (END, START, 0),
            (START, START + timedelta(seconds=31), 0), (START.replace(tzinfo=None), END, 0),
            (START, END, -1), (START, END, 3), (START, END, True)):
            with self.subTest(start=start, end=end, channel=channel), self.assertRaises(ClipError):
                await self.manager.export_clip_artifact("cam-test", start, end, channel)
        self.assertEqual(self.camera.calls, [])

    async def test_id_and_range_bounds(self):
        meta = await self.export()
        for bad in ("../outside.mp4", "0" * 63, "0" * 65, "A" * 64, "0" * 64):
            with self.assertRaises(ClipError):
                await self.manager.read_clip_chunk("cam-test", bad)
        for kwargs in ({"offset": -1}, {"offset": True}, {"offset": 0.5},
                       {"offset": meta["size_bytes"]}, {"max_bytes": 0}, {"max_bytes": 65537},
                       {"max_bytes": True}, {"max_bytes": 1.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ClipError):
                await self.read(meta, **kwargs)
        chunk = await self.read(meta, offset=meta["size_bytes"] - 1, max_bytes=65536)
        self.assertEqual(chunk["length"], 1)
        self.assertTrue(chunk["eof"])

    async def test_empty_partial_and_corrupt_streams_removed(self):
        valid = self.camera.payload
        for payload in (b"", valid[:-1], b"not-mp4"):
            async def stream(callback):
                await callback(len(payload), payload)
            with self.assertRaises(ClipError):
                await self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream)
            self.assertEqual(self.media_files(), [])
        async def short(callback):
            await callback(len(valid) + 1, valid)
        with self.assertRaises(ClipError):
            await self.store.create(scope=SCOPE, camera_id="cam-test", stream=short)
        self.assertEqual(self.media_files(), [])

    async def test_stream_exception_and_nonstreaming_result_removed(self):
        async def fail(callback):
            await callback(0, b"partial")
            raise OSError("synthetic transport failure")
        with self.assertRaises(OSError):
            await self.store.create(scope=SCOPE, camera_id="cam-test", stream=fail)
        async def buffered(callback):
            return self.camera.payload
        with self.assertRaises(ClipError):
            await self.store.create(scope=SCOPE, camera_id="cam-test", stream=buffered)
        self.assertEqual(self.media_files(), [])

    async def test_disk_failure_removes_partial_and_releases_quota(self):
        with patch("unifi_core.protect.clip_artifacts._write", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                await self.export()
        self.assertEqual(self.media_files(), [])
        self.assertFalse(self.store._active)
        await self.export()

    async def test_advertised_and_actual_byte_limits(self):
        for announce in (True, False):
            async def stream(callback):
                if announce:
                    await callback(100001, None)
                else:
                    await callback(0, b"x" * 60000)
                    await callback(0, b"y" * 50000)
            with self.assertRaises(ClipError):
                await self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream)
            self.assertEqual(self.media_files(), [])

    async def test_cancellation_removes_partial_and_releases_reservation(self):
        entered = asyncio.Event()
        async def stream(callback):
            await callback(0, b"partial")
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.media_files(), [])
        self.assertFalse(self.store._active)
        await self.export()

    async def test_timeout_removes_partial(self):
        self.store.limits = replace(self.limits, export_timeout_seconds=1)
        async def stream(callback):
            await callback(0, b"partial")
            await asyncio.Event().wait()
        with self.assertRaises(TimeoutError):
            await self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream)
        self.assertEqual(self.media_files(), [])

    async def test_repeated_cancellation_drains_write_before_cleanup(self):
        from unifi_core.protect.clip_artifacts import _write
        entered, release = threading.Event(), threading.Event()
        def blocked_write(fd, payload):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test write was not released")
            return _write(fd, payload)
        with patch("unifi_core.protect.clip_artifacts._write", blocked_write):
            task = asyncio.create_task(self.export())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.media_files(), [])
        self.assertFalse(self.store._active)
        await self.export()

    async def test_client_write_drains_repeated_cancellation(self):
        from download_clip import _settle_write
        entered, release = threading.Event(), threading.Event()
        def blocked_write():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test write was not released")
            return 5
        worker = asyncio.create_task(asyncio.to_thread(blocked_write))
        task = asyncio.create_task(_settle_write(worker))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(worker.result(), 5)

    async def test_concurrency_and_reservation_quota(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(callback):
            entered.set()
            await release.wait()
            await callback(len(self.camera.payload), self.camera.payload)
        task = asyncio.create_task(self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream))
        await entered.wait()
        with self.assertRaisesRegex(ClipError, "busy"):
            await self.export()
        self.store.limits = replace(self.limits, max_concurrent=2, max_total_bytes=100000)
        with self.assertRaisesRegex(ClipError, "storage quota"):
            await self.export()
        release.set()
        await task

    async def test_artifact_count_and_expiry_cleanup(self):
        now = [0.0]
        self.store._clock = lambda: now[0]
        self.store.limits = replace(self.limits, max_artifacts=1)
        meta = await self.export()
        with self.assertRaisesRegex(ClipError, "count quota"):
            await self.export()
        now[0] = 600
        with self.assertRaises(ClipError):
            await self.read(meta)
        self.assertEqual(self.media_files(), [])
        await self.export()

    async def test_periodic_expiry_without_another_tool_call(self):
        self.store.limits = replace(self.limits, ttl_seconds=1)
        await self.export()
        await asyncio.sleep(2.2)
        self.assertEqual(self.media_files(), [])

    async def test_exclusive_store_lock_and_restart_cleanup(self):
        meta = await self.export()
        other = ClipArtifactStore(self.directory, self.limits)
        with self.assertRaises(ClipError):
            await other.start()
        self.assertEqual(len(self.media_files()), 1)
        await self.store.close()
        orphan = self.directory / ("1" * 64 + ".part")
        orphan.write_bytes(b"old partial")
        unrelated = self.directory / "keep.txt"
        unrelated.write_text("unrelated")
        await other.start()
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())
        with self.assertRaises(ClipError):
            await other.read(artifact_id=meta["artifact_id"], scope=SCOPE, camera_id="cam-test",
                             offset=0, max_bytes=1)
        await other.close()

    async def test_changed_file_size_rejected(self):
        meta = await self.export()
        self.store._entries[meta["artifact_id"]].path.write_bytes(b"truncated")
        with self.assertRaises(ClipError):
            await self.read(meta)

    async def test_shutdown_cancels_export_and_rejects_reads(self):
        entered = asyncio.Event()
        async def stream(callback):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.store.create(scope=SCOPE, camera_id="cam-test", stream=stream))
        await entered.wait()
        await self.store.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.media_files(), [])
        with self.assertRaises(ClipError):
            await self.export()

    async def test_client_rejects_corruption_and_cleans_partial(self):
        meta = await self.export()
        async def corrupt(name, args):
            data = await self.manager.read_clip_chunk(**args)
            data["data_base64"] = base64.b64encode(b"x" * data["length"]).decode()
            return {"success": True, "data": data}
        out = Path(self.temp.name) / "bad.mp4"
        with self.assertRaises(ValueError):
            await download_clip(corrupt, {"success": True, "data": meta}, "cam-test", out)
        self.assertFalse(out.exists())
        self.assertEqual(list(out.parent.glob(".clip-download-*")), [])

    async def test_client_rejects_bad_identity_size_offset_hash_and_no_overwrite(self):
        meta = await self.export()
        for key, value in (("artifact_id", "a" * 64), ("size_bytes", 1), ("offset", 1),
                           ("next_offset", 0), ("length", 999999), ("eof", False), ("sha256", "b" * 64),
                           ("data_base64", "x" * 100000)):
            async def corrupt(name, args):
                data = await self.manager.read_clip_chunk(**args)
                data[key] = value
                return {"success": True, "data": data}
            out = Path(self.temp.name) / "bad.mp4"
            with self.subTest(key=key), self.assertRaises(ValueError):
                await download_clip(corrupt, {"success": True, "data": meta}, "cam-test", out)
            self.assertFalse(out.exists())
        out.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            await download_clip(corrupt, {"success": True, "data": meta}, "cam-test", out)
        self.assertEqual(out.read_bytes(), b"keep")

    async def test_erp_text_and_completed_job_wrapping_preserve_chunks(self):
        import json
        meta = await self.export()
        async def call(name, args):
            data = await self.manager.read_clip_chunk(**args)
            nested = {"status": "done", "result": {"success": True, "data": data}}
            return {"content": [{"type": "text", "text": json.dumps(nested)}]}
        out = Path(self.temp.name) / "wrapped.mp4"
        await download_clip(call, {"success": True, "data": meta}, "cam-test", out, chunk_bytes=777)
        self.assertEqual(out.read_bytes(), self.camera.payload)


class ValidationTests(unittest.TestCase):
    def test_invalid_limits(self):
        for field, value in (("max_duration_seconds", 0), ("max_concurrent", 5),
            ("max_clip_bytes", -1), ("ttl_seconds", True), ("ttl_seconds", 3601),
            ("max_artifacts", 0), ("max_total_bytes", 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ClipLimits(**{field: value})

    def test_client_rejects_error_and_deep_wrapping(self):
        with self.assertRaises(ValueError):
            unwrap_tool_data({"success": False, "error": "sensitive details should not be propagated"})
        with self.assertRaises(ValueError):
            unwrap_tool_data({"content": []})


class StreamingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_stream_response_closes_on_success_failure_and_cancellation(self):
        import importlib.util
        from unittest.mock import MagicMock
        sdk = types.ModuleType("uiprotect")
        class BaseClient:
            async def _stream_response(self, response, chunk_size, iterator_callback, progress_callback):
                await iterator_callback(1, b"x")
        sdk.ProtectApiClient = BaseClient
        path = Path(__file__).parents[1] / "packages/unifi-core/src/unifi_core/protect/streaming_client.py"
        with patch.dict(sys.modules, {"uiprotect": sdk}):
            spec = importlib.util.spec_from_file_location("fixture_streaming_client", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        for error in (None, ClipError("limit"), asyncio.CancelledError()):
            response = types.SimpleNamespace(close=MagicMock())
            async def callback(total, chunk):
                if error is not None:
                    raise error
            if error is None:
                await module.StreamingProtectApiClient()._stream_response(response, 65536, callback)
            else:
                with self.assertRaises(type(error)):
                    await module.StreamingProtectApiClient()._stream_response(response, 65536, callback)
            response.close.assert_called_once()
