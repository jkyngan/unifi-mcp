"""Recording tools for UniFi Protect MCP server.

Provides tools for querying recording status, listing recording availability,
and exporting video clips from cameras.
"""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, Optional

from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from unifi_core.exceptions import UniFiNotFoundError
from unifi_core.protect.clip_artifacts import ClipError
from unifi_core.protect.models._actions import (
    DeleteRecordingInput,
    ExportClipArtifactInput,
    ExportClipInput,
    ReadClipChunkInput,
)
from unifi_core.protect.models.recordings import (
    from_controller as recording_from_controller,
)
from unifi_core.protect.models.recordings import (
    status_list_from_controller,
)
from unifi_protect_mcp.runtime import recording_manager, server

logger = logging.getLogger(__name__)


# Private clip tools intentionally use bounded JSON chunks in the standard data
# envelope: direct, execute and batch paths can all retain the bytes.
@server.tool(
    name="protect_export_clip_artifact",
    description=(
        "Export a short original MP4 into private temporary storage, preserving source audio. "
        "Disabled unless the operator enables private clips. Returns an expiring artifact ID, "
        "size and SHA-256; use protect_read_clip_chunk to obtain actual bytes. "
        "This operation does not return a public URL or imply playback."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    permission_category="recording",
    permission_action="read",
    input_schema=ExportClipArtifactInput.model_json_schema(),
)
async def protect_export_clip_artifact(
    camera_id: Annotated[str, Field(description="Camera ID from protect_list_cameras; scoped to this Protect server.")],
    start: Annotated[str, Field(description="Start timestamp with explicit timezone, e.g. 2020-01-01T12:00:00+00:00.")],
    end: Annotated[str, Field(description="End timestamp with timezone; default operator clip limit is 30 seconds.")],
    channel_index: Annotated[
        int, Field(strict=True, ge=0, le=2, description="Original video channel: 0 high, 1 medium, 2 low.")
    ] = 0,
) -> Dict[str, Any]:
    """Create an ephemeral original-file artifact without changing NVR recordings."""
    try:
        values = ExportClipArtifactInput(camera_id=camera_id, start=start, end=end, channel_index=channel_index)
        start_dt, end_dt = datetime.fromisoformat(values.start), datetime.fromisoformat(values.end)
        result = await recording_manager.export_clip_artifact(values.camera_id, start_dt, end_dt, values.channel_index)
        return {"success": True, "data": result}
    except ClipError as exc:
        return {"success": False, "error": f"Failed to export private clip: {exc}"}
    except (ValidationError, ValueError, TypeError):
        return {
            "success": False,
            "error": "Failed to export private clip: invalid camera, channel or timezone timestamp",
        }
    except Exception as exc:
        # Export exceptions may contain controller URLs/credentials. Do not log
        # payloads, paths, exception text or tracebacks at this media boundary.
        logger.warning("Private clip export failed (%s)", type(exc).__name__)
        return {
            "success": False,
            "error": "Failed to export private clip; verify access, recording availability and limits",
        }


@server.tool(
    name="protect_read_clip_chunk",
    description=(
        "Read up to 65536 original MP4 bytes from an unexpired private clip on this Protect server. "
        "Returns bounded base64 in data with offsets and SHA-256 checksums. Reassemble using code, "
        "verify the complete file checksum, and expose the resulting MP4; never print chunks into chat. "
        "Camera media permission is checked on every call."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    permission_category="recording",
    permission_action="read",
    input_schema=ReadClipChunkInput.model_json_schema(),
)
async def protect_read_clip_chunk(
    camera_id: Annotated[str, Field(description="Same camera ID used to create the clip, on the same Protect server.")],
    artifact_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$", description="Opaque artifact ID from export; never a path.")
    ],
    offset: Annotated[
        int, Field(strict=True, ge=0, description="Zero-based byte offset; start with 0, then use next_offset.")
    ] = 0,
    max_bytes: Annotated[
        int, Field(strict=True, ge=1, le=65536, description="Maximum decoded bytes per chunk (1..65536).")
    ] = 32768,
) -> Dict[str, Any]:
    """Return actual bounded bytes using the existing authenticated MCP transport."""
    try:
        values = ReadClipChunkInput(camera_id=camera_id, artifact_id=artifact_id, offset=offset, max_bytes=max_bytes)
        result = await recording_manager.read_clip_chunk(**values.model_dump())
        return {"success": True, "data": result}
    except (ClipError, UniFiNotFoundError):
        return {"success": False, "error": "Failed to read private clip: unavailable, expired, denied or invalid range"}
    except (ValidationError, ValueError, TypeError):
        return {"success": False, "error": "Failed to read private clip: invalid ID, offset or chunk size"}
    except Exception as exc:
        logger.warning("Private clip read failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Failed to read private clip"}


# ---------------------------------------------------------------------------
# Helper: parse ISO datetime string
# ---------------------------------------------------------------------------


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-format datetime string, returning None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


@server.tool(
    name="protect_get_recording_status",
    description=(
        "Returns the current recording state for one or all cameras. Shows "
        "recording mode (always, never, detections), whether actively recording, "
        "and available recording time range from NVR stats. Pass a camera_id to "
        "check a single camera, or omit for all cameras."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def protect_get_recording_status(
    camera_id: Annotated[
        Optional[str],
        Field(
            description="Camera UUID (from protect_list_cameras) to check. Omit to get recording status for all cameras."
        ),
    ] = None,
) -> Dict[str, Any]:
    """Get recording status for one or all cameras."""
    logger.info("protect_get_recording_status called (camera_id=%s)", camera_id)
    try:
        result = await recording_manager.get_recording_status(camera_id=camera_id)
        shaped = status_list_from_controller(result).model_dump(exclude_none=True)
        return {"success": True, "data": shaped}
    except (UniFiNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Error getting recording status: %s", e, exc_info=True)
        return {"success": False, "error": f"Failed to get recording status: {e}"}


@server.tool(
    name="protect_list_recordings",
    description=(
        "Returns recording availability information for a camera within a time "
        "range. Shows the recording window and whether footage exists. UniFi "
        "Protect stores recordings as continuous streams, not discrete segments. "
        "Defaults to the last 24 hours if no time range is specified. "
        "Times should be ISO-8601 format (e.g., '2026-03-16T00:00:00Z')."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def protect_list_recordings(
    camera_id: Annotated[str, Field(description="Camera UUID (from protect_list_cameras) to query recordings for.")],
    start: Annotated[
        Optional[str],
        Field(
            description="Start of the time range as ISO 8601 timestamp (e.g., 2026-03-16T00:00:00Z). Defaults to 24 hours ago."
        ),
    ] = None,
    end: Annotated[
        Optional[str],
        Field(description="End of the time range as ISO 8601 timestamp (e.g., 2026-03-17T00:00:00Z). Defaults to now."),
    ] = None,
) -> Dict[str, Any]:
    """List recording availability for a camera."""
    logger.info("protect_list_recordings called (camera=%s, start=%s, end=%s)", camera_id, start, end)
    try:
        result = await recording_manager.list_recordings(
            camera_id=camera_id,
            start=_parse_datetime(start),
            end=_parse_datetime(end),
        )
        shaped = recording_from_controller(result).model_dump(exclude_none=True)
        return {"success": True, "data": shaped}
    except (UniFiNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Error listing recordings for camera %s: %s", camera_id, e, exc_info=True)
        return {"success": False, "error": f"Failed to list recordings: {e}"}


@server.tool(
    name="protect_export_clip",
    description=(
        "Exports a video clip from a camera for a specified time range. Returns "
        "metadata about the export (size, duration) but not the video data itself "
        "(too large for MCP responses). Maximum duration is 2 hours. "
        "For timelapse exports, pass fps (4=60x, 8=120x, 20=300x, 40=600x). "
        "Times must be ISO-8601 format (e.g., '2026-03-16T12:00:00Z')."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def protect_export_clip(
    camera_id: Annotated[str, Field(description="Camera UUID (from protect_list_cameras) to export footage from.")],
    start: Annotated[str, Field(description="Clip start time as ISO 8601 timestamp (e.g., 2026-03-16T12:00:00Z).")],
    end: Annotated[
        str,
        Field(
            description="Clip end time as ISO 8601 timestamp (e.g., 2026-03-16T12:30:00Z). Maximum 2 hours after start."
        ),
    ],
    channel_index: Annotated[
        int,
        Field(description="Video channel index: 0 = high quality (default), 1 = medium, 2 = low."),
    ] = 0,
    fps: Annotated[
        Optional[int],
        Field(
            description="Frames per second for timelapse export. Common values: 4 (60x speed), 8 (120x), 20 (300x), 40 (600x). Omit for normal speed."
        ),
    ] = None,
) -> Dict[str, Any]:
    """Export a video clip from a camera."""
    logger.info("protect_export_clip called (camera=%s, start=%s, end=%s, fps=%s)", camera_id, start, end, fps)
    try:
        try:
            ExportClipInput(camera_id=camera_id, start=start, end=end, channel_index=channel_index, fps=fps)
        except ValidationError as e:
            return {"success": False, "error": f"Invalid input: {e.errors()[0]['msg']}"}
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if start_dt is None:
            return {
                "success": False,
                "error": "Invalid start time. Use ISO-8601 format (e.g., '2026-03-16T12:00:00Z').",
            }
        if end_dt is None:
            return {"success": False, "error": "Invalid end time. Use ISO-8601 format (e.g., '2026-03-16T12:30:00Z')."}

        result = await recording_manager.export_clip(
            camera_id=camera_id,
            start=start_dt,
            end=end_dt,
            channel_index=channel_index,
            fps=fps,
        )
        return {"success": True, "data": result}
    except (UniFiNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Error exporting clip for camera %s: %s", camera_id, e, exc_info=True)
        return {"success": False, "error": f"Failed to export clip: {e}"}


@server.tool(
    name="protect_delete_recording",
    description=(
        "Attempts to delete recordings for a camera in a time range. "
        "Note: Individual recording deletion is NOT supported by the uiprotect API. "
        "Recording retention is managed automatically by the NVR. This tool returns "
        "information about the limitation and alternative approaches."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    permission_category="recording",
    permission_action="delete",
)
async def protect_delete_recording(
    camera_id: Annotated[str, Field(description="Camera UUID (from protect_list_cameras) to delete recordings for.")],
    start: Annotated[
        str, Field(description="Start of the deletion range as ISO 8601 timestamp (e.g., 2026-03-16T00:00:00Z).")
    ],
    end: Annotated[
        str, Field(description="End of the deletion range as ISO 8601 timestamp (e.g., 2026-03-16T12:00:00Z).")
    ],
    confirm: Annotated[
        bool,
        Field(
            description="When true, attempts the deletion. When false (default), returns a preview. Note: deletion is not supported by the API regardless."
        ),
    ] = False,
) -> Dict[str, Any]:
    """Delete recording for a camera (not supported by API)."""
    logger.info(
        "protect_delete_recording called (camera=%s, start=%s, end=%s, confirm=%s)",
        camera_id,
        start,
        end,
        confirm,
    )
    try:
        try:
            DeleteRecordingInput(camera_id=camera_id, start=start, end=end)
        except ValidationError as e:
            return {"success": False, "error": f"Invalid input: {e.errors()[0]['msg']}"}
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if start_dt is None:
            return {"success": False, "error": "Invalid start time. Use ISO-8601 format."}
        if end_dt is None:
            return {"success": False, "error": "Invalid end time. Use ISO-8601 format."}

        result = await recording_manager.delete_recording(
            camera_id=camera_id,
            start=start_dt,
            end=end_dt,
        )
        # Since deletion is not supported, always return the info response
        return {"success": False, "error": result["message"]}
    except (UniFiNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Error with delete recording for camera %s: %s", camera_id, e, exc_info=True)
        return {"success": False, "error": f"Failed to process recording deletion: {e}"}


logger.info(
    "Recording tools registered: protect_get_recording_status, protect_list_recordings, "
    "protect_export_clip, protect_delete_recording"
)
