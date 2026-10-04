# Private clip Linux verification — 2026-10-04

Source under test starts at `52dfd2e12ef34d9111d04858cc458d4d980e328e`,
against owned baseline `16c75df93ef255d200002d91a7e5d39456428900`.
This follow-up changes tests and documentation only. The feature remains disabled
by default. No controller connection, actual footage, deployment, merge, gateway
configuration, or permissions change was performed.

## Environment and dependency status

Linux x86_64, preinstalled CPython 3.13.5, uv 0.12.19, Node 24.19.0,
FFmpeg/ffprobe 7.1.5. The isolated environment contains the repository-locked
MCP 2.1.1, mcp-types 2.1.1, uiprotect 16.10.0, cryptography 50.0.1,
Pydantic 2.13.5, pytest 9.1.1, Ruff 0.16.6, and OmegaConf 2.3.1.

The full wheel-only locked sync stopped because `antlr4-python3-runtime==4.9.3`
has no binary distribution. A second locked sync explicitly excluded that one
package and installed the other 99 packages, including local editable workspace
packages. All registry dependencies were prohibited from source builds. No
compiler, Rust toolchain, Python runtime, or global package was installed.

**This is an incomplete locked environment.** `uv pip check` reports exactly one
missing dependency: OmegaConf requires ANTLR 4.9.x. Allowing the exact
lockfile-hashed ANTLR pure-Python source to be packaged into an isolated wheel
requires an explicit exception to the no-source-build constraint. No such build
was performed. The lockfile was neither updated nor relaxed; its SHA-256 remains
`cda0a3755393415db46df1545f2b7e34221b7a9d98c2e3e46808d17fa0979674`.

## Results

| Check | Result | Scope |
| --- | --- | --- |
| Core/client focused pytest | 28 passed, 30 subtests passed | Normal core imports; synthetic controller and permission test doubles |
| Standalone focused suite | 38 passed, no skips | Native MCP 2.1.1 and uiprotect 16.10.0; package initializers bypassed by the explicitly isolated runner |
| Separate actual HIIS helper checks | 9 passed | Extracted helper bodies only; mocked HTTP, no server/config import |
| Full-workspace Ruff | Passed | `ruff check .` |
| Full-workspace formatting | Passed | 1105 files already formatted |
| Normal Protect import/startup | Blocked | `ModuleNotFoundError: No module named 'antlr4'`; server never started |
| Official Protect manifest generator | Blocked | Same ANTLR import failure; generated manifest was not edited |
| Normal combined focused pytest, initial attempt | 27 passed, 9 failed | Transport setups failed on missing ANTLR before dispatch; two later tests added and isolated coverage expanded |
| Root `make pre-commit` | Blocked | Formatting passed; generation stopped at first app manifest on missing ANTLR; later stages not run |
| Complete app/core/shared/catalog/protocol/worker gates | Not run to completion | Full environment is not installed |
| Live gateway/NVR/KKM playback | Not run | Synthetic-only verification; production remains gated |

Reproduction with the isolated environment's Python:

```sh
PYTHONPATH=examples/python:tests .venv/bin/python -m pytest tests/test_private_clips.py -q
HIIS_MCP_SOURCE=/path/to/authorized/hiis/mcp-server/index.ts \
  .venv/bin/python scripts/test_private_clips_standalone.py
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The optional HIIS test requires existing Node support for
`node:module.stripTypeScriptTypes`. Without that authorized source or FFmpeg, the
corresponding tests explicitly skip; the reported Linux run had both available.
Use `UV_NO_SYNC=1`, `UV_LOCKED=1`, `UV_NO_BUILD=1`, and
`UV_PYTHON_DOWNLOADS=never` when invoking repository Make targets in this partial
environment, so they cannot silently rebuild dependencies or download a runtime.

## What the new native coverage proves

The MCP 1.30 alias and adapter were removed. Tests use `UniFiMCPServer`, including
strict argument dispatch, response serialization, the protocol tool adapter,
permission decorators, and the real execute/batch implementations. The test
runtime injects a synthetic manager instead of booting a controller connection.
MCP 2's native schema attribute is `input_schema`.

For both the H.264 128×72, 12 fps video/AAC 48 kHz mono tone fixture and the
silent video fixture, direct calls, execute calls, and batch export followed by
direct reads reconstruct exactly the original MP4 bytes and SHA-256. Native
uiprotect `Camera.get_video`, `get_camera_video`, and streaming callbacks run;
only the HTTP request/response is mocked. Assertions check normal-speed request
parameters, SDK response closure, nonidentical first/last video frames, decoded
video equality, nonzero audio and decoded PCM equality, and absence of invented
audio in the silent clip. This is not live HTTP/NVR integration or playback in a
consumer UI.

Separate native SDK tests verify closure after success, sink failure, and
cancellation. Linux tests verify directory 0700 and artifact 0600, reject a
world-readable store and directory/file symlinks, and preserve the unrelated
symlink target. Existing quota, expiry, cancellation, scope, integrity, and batch
cache exclusion checks remain covered.

The optional HIIS bridge passes actual native MCP direct/execute results through
`requestCake`, `asImageContent`, and `asFileContent` extracted from an authorized
local checkout, then reconstructs the MP4. Inputs are bounded to 256000 characters;
base64 stays in programmatic pipes. No private HIIS source is copied into this
repository. Local helper compatibility does not establish live gateway parity,
identity propagation, access control, or client attachment delivery.

## Remaining deployment requirements

1. Resolve the ANTLR installation exception, complete the unchanged locked sync,
   verify dependency consistency, and rerun normal imports and startup without
   live credentials.
2. Run the official manifest generator, review generated differences and remove
   the old generation note through regeneration, then complete repository gates.
3. Establish immutable image-to-source mapping for the intended KKM canary and
   review authenticated gateway identity and camera access control. The current
   model binds controller/site/NVR user/camera; separate human-owner isolation is
   not proven when credentials are shared.
4. Obtain explicit deployment/restart approval before enabling one canary, then
   deliver a synthetic file through the actual connector and verify its hash,
   video, audio, and consuming-client readability.
5. Only after those gates, request an explicitly authorized camera and short
   October 4 time window for sales-review footage. Synthetic verification does
   not establish production playback or explain the sales slowdown.
