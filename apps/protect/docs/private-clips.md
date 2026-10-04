# Private original clip retrieval

This opt-in feature retains short original MP4 exports and serves bounded chunks
through the existing MCP tool transport. It adds no public URL, HTTP media route,
credentials, external storage, or transcoding. `protect_export_clip` remains the
existing metadata-only API.

## Operator configuration

Enable only after reviewing the existing gateway's client access scope and
validating the actual consuming connector with a synthetic clip.

| Variable | Default | Meaning |
| --- | --- | --- |
| `UNIFI_PROTECT_CLIPS_ENABLED` | false | Explicit opt-in |
| `UNIFI_PROTECT_CLIPS_DIRECTORY` | system temp / unifi-protect-clips | Dedicated private directory |
| `UNIFI_PROTECT_CLIPS_MAX_DURATION_SECONDS` | 30 | Maximum requested clip duration |
| `UNIFI_PROTECT_CLIPS_MAX_CLIP_BYTES` | 67108864 | Maximum file bytes, enforced during streaming |
| `UNIFI_PROTECT_CLIPS_MAX_TOTAL_BYTES` | 268435456 | File bytes plus in-flight reservations |
| `UNIFI_PROTECT_CLIPS_TTL_SECONDS` | 600 | Lifetime from successful export completion |
| `UNIFI_PROTECT_CLIPS_MAX_CONCURRENT` | 1 | Simultaneous exports; excess requests rejected |
| `UNIFI_PROTECT_CLIPS_MAX_ARTIFACTS` | 32 | Ready plus in-flight artifacts |
| `UNIFI_PROTECT_CLIPS_EXPORT_TIMEOUT_SECONDS` | 120 | Network export deadline |

Each process/container needs its own directory. Linux directories must be owned by
the process user with 0700 permissions; files use 0600. A process lock prevents
two servers sharing a store. At startup, only recognized opaque artifact filenames
are removed, including interrupted exports from a previous process. No recursive
deletion is used. Normal shutdown cancels exports and removes completed artifacts.
Process crash loses artifact IDs; restart clears the orphaned files.

Expiry immediately denies new chunk reads; periodic physical deletion follows
within at most 30 seconds. In-flight maximum-size reservations and a count limit
bound both byte and metadata growth. A cleanup failure does not release quota.
Review aggregate capacity across all Protect instances before enabling them.

## Authentication and scope

Every call travels through the existing authenticated MCP endpoint. No HTTP
download endpoint or query-string token is introduced.

The manager checks the current SDK bootstrap authenticated user's camera
`READ_MEDIA` permission before export and each read, matching Camera.get_video's
permission model. IDs are bound to controller host/port, site, NVR user ID and
camera ID and are not portable to another Protect instance. IDs are random,
not filesystem paths. The existing read policy gates are not an end-user ACL;
they intentionally allow reads.

**This preserves the existing controller-principal scope.** If multiple downstream
users share one NVR credential, they retain that shared scope. This implementation
does not invent per-user identities absent from the gateway, and does not claim
instant revocation beyond the SDK's current bootstrap authorization state. A
gateway with finer per-user camera rules must propagate/enforce those rules before
this feature is enabled. Do not trust caller-supplied owner, role or site claims.

## Client workflow

1. Call `protect_export_clip_artifact(camera_id, start, end, channel_index=0)`.
   Timestamps need explicit timezones. The result includes artifact ID, exact size,
   complete-file SHA-256, expiry, track counts, and a structural-container check.
2. Call `protect_read_clip_chunk(camera_id, artifact_id, offset=0, max_bytes=32768)`.
   Maximum decoded chunk size is 65536 bytes. Use `next_offset` until `eof`.
3. Reconstruct programmatically, checking every offset, size and chunk SHA-256,
   then the complete-file SHA-256. Never print base64 chunks into conversation.
   `examples/python/download_clip.py` accepts the client's existing authorized
   asynchronous `call_tool(name, arguments)` adapter and writes atomically to a
   locally selected destination, refusing to overwrite an existing file.

The bytes remain normal-speed original MP4: no fps parameter, no resizing, no
transcoding, and all source audio tracks retained. Silent footage remains silent.
ISO-BMFF framing/track checks reject common empty/truncated/corrupt responses;
they are not a codec decoder. Independently decode/probe the delivered file and
report unsupported codecs, rather than claiming playback from metadata.

Direct calls and `protect_execute` retain chunks inside the standard data envelope.
That shape also survives a text-JSON bridge. Native
resource forwarding is not required. A URI, local server path, or a successful
export response alone is not delivery.

Artifact creation may use `protect_batch`. Chunk reads are explicitly rejected
in batch, and nested execute results containing chunks are also blocked from the
job cache. Otherwise completed jobs could retain footage after artifact expiry
and without repeating camera authorization. Existing unrelated batch tools are
unchanged.

The uiprotect iterator callback receives `(total_bytes, chunk_or_none)` and returns
None on successful streaming. File validation, not that None return value, decides
export success. A narrow SDK subclass closes stream responses on cancellation or
sink failure; recheck the `_stream_response` hook against future SDK versions.

## Validation and release gates

The repository includes tiny generated video+tone and silent MP4 fixtures and a
Blender generation script. Unit tests exercise quotas, limits, cancellation,
partial/no recording, authorization, scope, expiry, restart, integrity and client
cleanup. The standalone runner avoids controller bootstrap and software installs.

```text
python scripts/test_private_clips_standalone.py
```

Transport tests require native MCP 2.x and use the production UniFi dispatcher,
response serialization, execute and batch handlers. They no longer use an MCP
1.30 import shim. With existing FFmpeg/ffprobe, they also exercise native
uiprotect Camera/export streaming against a mocked HTTP response and compare
independently decoded video and PCM audio. Set `HIIS_MCP_SOURCE` to an authorized
local `mcp-server/index.ts` to test the actual three HIIS helper functions with
existing Node TypeScript stripping, without importing server/config code.

The standalone runner still bypasses package initializers and uses a synthetic
controller. It does not replace normal imports, startup, or the full project gates.
See [Linux verification results](private-clips-verification.md) for exact coverage
and the remaining locked-environment blocker.
Before rollout, run the official Protect manifest generator and required project
checks in the approved locked environment. The isolated manifest additions are
marked with a generation note until full regeneration is completed.

Canary one approved instance after explicit deployment/restart approval. Verify the actual
connector yields a client-readable file with matching hash, decoded video and
original audio using synthetic material first. Any subsequent real-footage test
must use an explicitly authorized camera and short time window.

Rollback: disable private clips, restore the previous pinned image/config with
approval, cancel/drain exports, and clear only the dedicated artifact directory.
Never delete NVR recordings. Confirm legacy metadata tools and camera scope.
