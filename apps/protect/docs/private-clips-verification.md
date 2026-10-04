# Private clip Linux verification — 2026-10-04

Verification began at `52dfd2e12ef34d9111d04858cc458d4d980e328e`, against owned
baseline `16c75df93ef255d200002d91a7e5d39456428900`. Native tests were published
at `91de1d1c25ef6e0ee2a4d86e882393d0eae78178`; this follow-up completes locked
Python installation, official generation, and the Python repository gates.
The feature remains disabled by default. No live NVR, actual footage,
deployment, merge, gateway configuration, or permissions change was performed.

## Locked environment

Linux x86_64 with preinstalled CPython 3.13.5, uv 0.12.19, Node 24.19.0, and
FFmpeg/ffprobe 7.1.5. Installed locked packages include MCP 2.1.1,
mcp-types 2.1.1, uiprotect 16.10.0, cryptography 50.0.1, Pydantic 2.13.5,
pytest 9.1.1, Ruff 0.16.6, OmegaConf 2.3.1, and ANTLR 4.9.3.

The initial wheel-only sync stopped at ANTLR, which has no wheel. After the
explicitly authorized pure-Python exception, its PyPI source archive was checked
against the lockfile SHA-256
`f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b`.
Its `setup.py` uses setuptools to package Python files; it has no native source,
compiler requirement, or non-registry build requirement. Only this registry
package was allowed to build; all other registry source builds remained denied.
Local workspace packages are editable installs.

The full `uv sync --locked --all-packages --all-extras` completed using the
existing Python 3.13.5 runtime. `uv pip check` reports all 100 installed packages
compatible. No compiler, Rust, Python runtime, global package, or dependency
version update was needed. The unchanged `uv.lock` SHA-256 is
`cda0a3755393415db46df1545f2b7e34221b7a9d98c2e3e46808d17fa0979674`.

## Final results

Counts below describe separate suites and overlap; do not add them as unique tests.

| Check | Result |
| --- | --- |
| Normal focused core/client/native MCP suite | 38 passed, 30 subtests; clean exit |
| Core package | 2598 passed |
| Shared package | 553 passed under a child-reaping test supervisor |
| Protect app | 533 passed |
| Network app | 1315 passed |
| Access app | 235 passed |
| API app | 1545 passed on full run; all 13 environment-related failures passed on rerun, covering all 1558 tests |
| Root catalog/harness suite | 1449 passed, 6 intentionally skipped, 249 subtests passed |
| Relay package | 143 passed, one warning |
| Documentation unittest gate | 79 passed |
| Additional extracted HIIS helper checks | 9 passed |
| Normal Protect package import | Passed with bundled defaults and private clips disabled |
| Real stdio startup/protocol smoke | 12 combinations passed: Network/Protect × lazy/eager/meta_only × automatic/legacy handshake |
| Official manifest generation | Passed for all three products; Protect has 64 tools |
| Generated drift checks | Passed for skill references, API action catalog, and support skills |
| API SDL/OpenAPI and both reference docs | Regenerated; no resulting schema/doc differences |
| Full-workspace Ruff and formatting | Passed; 1107 Python files formatted |
| Complete root `make pre-commit` | Not green as one uninterrupted command; initial run stopped on container process-reaping tests, subsequently resolved under supervision |
| Worker tests/typecheck | Not run: locked npm toolchain is not installed; no Worker code changed |
| Access live protocol smoke / actual gateway/NVR playback | Not run; live credentials and deployment remain outside this verification |

The six root skips are existing community-triage cases explicitly marked as
covered by another rendering test. Access unit tests ran, but the offline
protocol harness intentionally excludes Access startup without a controller.
Automatic protocol negotiation used `2026-07-28`; legacy used `2025-11-25`.
Dummy startup credentials targeted loopback only; no live-controller operation
was performed.

### Failures found and resolved

- Native MCP 2 uses `input_schema`; the old MCP 1.30 test shim was removed.
- A normal focused run initially reported passing tests but segfaulted at Python
  shutdown. The test's `sys.modules` snapshot removed native extensions imported
  afterward. Loading native uiprotect/PyAV before that snapshot fixed clean exit;
  this is test isolation only, not a production monkey patch.
- Normal root pytest could not import the example download client. A small root
  test `conftest.py` adds the examples directory without bypassing package imports.
- Official API catalog generation initially enrolled the two new clip tools into
  REST actions, causing 567 API failures, primarily missing serializer coverage.
  They now use the existing explicit `API_ACTION_EXCLUSIONS` mechanism: private
  process artifacts remain MCP-only. Regression tests verify they cannot resolve
  as API actions, and the generated catalog records their exclusion reasons.
- Five documentation count assertions exposed stale 62-tool text. Source docs and
  generated references now consistently describe 64 Protect tools.
- Three unchanged shared credential-helper lifecycle tests initially failed
  because this container's PID 1 retained zombie descendants. The same unmodified
  tests all passed under a separate Linux `PR_SET_CHILD_SUBREAPER` supervisor that
  reaps orphaned children, like a normal init. No production lifecycle code changed.
- Thirteen API packaging/CLI/migration tests failed because uv's default cache
  directory is read-only. All passed after setting `UV_CACHE_DIR` to a writable
  workspace directory. No API production change was needed for those failures.

## Reproduction

After the complete locked sync, use the isolated environment directly and keep
runtime downloads disabled. A writable uv cache is necessary in this sandbox.

```sh
export UV_CACHE_DIR=/path/to/writable/workspace/uv-cache
export UV_PYTHON_DOWNLOADS=never
export UV_NO_SYNC=1
export UV_LOCKED=1
export PATH="$PWD/.venv/bin:$PATH"
export HIIS_MCP_SOURCE=/path/to/authorized/hiis/mcp-server/index.ts
python -m pytest tests/test_private_clips.py tests/test_private_clips_transport.py -q
make manifest
make check-generated
python scripts/smoke_mcp_metadata.py --server all --registration-mode all --client-mode all
ruff check .
ruff format --check .
```

Run normal package pytest suites separately as in the repository Makefile. For
shared process-lifecycle tests, use an environment whose init reaps orphaned
children, or a separate child-reaping supervisor. The optional HIIS test needs
existing Node `stripTypeScriptTypes`; audiovisual checks need existing FFmpeg.
The reported focused run had both and did not skip either test.

Worker npm dependencies are absent. Completing that separate repository gate
would require the locked npm packages, including TypeScript 5.9.3, Vitest 4.1.11,
Wrangler 4.129.0, esbuild 0.28.1, and workerd 1.20260903.1. They were not installed
under the no-new-compiler/runtime/toolchain restriction. No claim of a completely
green root/Worker gate is made.

## Synthetic audiovisual and transport coverage

Tests use the production `UniFiMCPServer`, strict argument dispatch, response
serialization, protocol adapter, permission decorators, and execute/batch
implementations. Only the test manager/controller is injected; normal tests no
longer bypass package initializers. The standalone runner remains available but
is no longer the sole basis for native SDK verification.

For both H.264 128×72, 12 fps video with AAC 48 kHz mono tone and silent video,
direct calls, execute calls, and batch export followed by direct reads reconstruct
exactly the original MP4 bytes and SHA-256. Native uiprotect `Camera.get_video`,
`get_camera_video`, and streaming callbacks execute with the HTTP response mocked.
Assertions check normal-speed parameters, response closure, changing video frames,
independently decoded video equality, nonzero audio/PCM equality, and no invented
audio in the silent fixture. This does not prove live HTTP/NVR integration or
playback in a consuming UI.

Native SDK lifecycle tests cover success, sink failure, and cancellation. Linux
checks verify 0700 directories, 0600 artifacts, rejection of unsafe directory
permissions and directory/file symlinks, and preservation of an unrelated target.
Quota, expiry, scope, authorization rechecks, client integrity, and batch cache
exclusion remain covered.

The optional HIIS bridge passes native MCP direct/execute results through actual
`requestCake`, `asImageContent`, and `asFileContent` helper bodies extracted from
an authorized local checkout, then reconstructs the file. Inputs are bounded to
256000 characters; base64 remains inside programmatic pipes. No private HIIS
source, server/config import, network operation, or secret is included. Local
helper compatibility does not establish live gateway source parity or ACLs.

## Remaining rollout requirements

1. Complete the separate Worker/full-repository gate if required for release,
   using an approved locked npm toolchain environment.
2. Establish immutable image-to-source mapping for the intended KKM canary and
   verify authenticated gateway identity and camera access control. This feature
   binds controller/site/NVR user/camera; separate human-owner isolation remains
   unproven when controller credentials are shared.
3. Obtain explicit deployment/restart approval before enabling one canary, then
   deliver a synthetic file through the actual connector and verify its hash,
   video, audio, and consuming-client readability.
4. Only afterward retrieve an explicitly authorized camera and short October 4
   time window for sales-review footage. Synthetic results do not establish
   production playback or explain the sales slowdown.
