"""Decode fixtures and MCP outputs with already-locked PyAV/FFmpeg libraries."""

import json
from pathlib import Path

import av


def decoded(path):
    with av.open(str(path)) as container:
        has_audio = bool(container.streams.audio)
        video = bytearray()
        for frame in container.decode(video=0):
            rgb = frame.reformat(format="rgb24")
            plane = rgb.planes[0]
            data = bytes(plane)
            # Omit alignment padding; compare actual RGB pixels only.
            for row in range(rgb.height):
                offset = row * plane.line_size
                video.extend(data[offset : offset + rgb.width * 3])
    audio = bytearray()
    if has_audio:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        with av.open(str(path)) as container:
            for frame in container.decode(audio=0):
                for pcm in resampler.resample(frame):
                    audio.extend(bytes(pcm.planes[0])[: pcm.samples * 2])
            for pcm in resampler.resample(None):
                audio.extend(bytes(pcm.planes[0])[: pcm.samples * 2])
    return bytes(video), bytes(audio), has_audio


results = []
paths = sorted(Path("/out").glob("*.mp4"))
assert len(paths) == 6
for path in paths:
    original = Path("/verification/tests/fixtures/clips") / path.name.split("-", 1)[1]
    actual = decoded(path)
    assert actual == decoded(original), f"Decoded mismatch: {path.name}"
    video, audio, has_audio = actual
    assert video and has_audio == ("tone" in path.name)
    assert any(audio) if has_audio else not audio
    results.append(
        {
            "file": path.name,
            "decoded_video_bytes": len(video),
            "decoded_audio_bytes": len(audio),
            "decoded_equal": True,
            "audio_present": has_audio,
        }
    )
report = {
    "decoder": "locked PyAV backed by FFmpeg",
    "av_version": av.__version__,
    "library_versions": av.library_versions,
    "results": results,
}
Path("/out/decoded.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
