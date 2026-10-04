"""Independently decode original and reconstructed fixtures with existing Blender."""
import hashlib
import json
import sys
from pathlib import Path
import aud
import bpy

root = Path(__file__).resolve().parent
output = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
probes = output / "decode-probes"
probes.mkdir(exist_ok=True)
scene = bpy.context.scene
scene.render.resolution_x = 128
scene.render.resolution_y = 72
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.render.image_settings.media_type = "IMAGE"
scene.render.image_settings.file_format = "PNG"
scene.render.use_sequencer = True
editor = scene.sequence_editor_create()
results = []
for fixture, has_audio in (("tone.mp4", True), ("silent.mp4", False)):
    pair = []
    for label, path in (("original", root / fixture), ("reconstructed", output / ("reconstructed-" + fixture))):
        movie = editor.strips.new_movie("Synthetic movie", str(path), channel=1, frame_start=1)
        images = []
        for frame in (1, 12):
            scene.frame_set(frame)
            png = probes / f"{label}-{fixture}-{frame}.png"
            scene.render.filepath = str(png)
            bpy.ops.render.render(write_still=True)
            images.append(hashlib.sha256(png.read_bytes()).hexdigest())
        editor.strips.remove(movie)
        audio = None
        if has_audio:
            sound = aud.Sound(str(path))
            samples = sound.data()
            assert samples.size > 0 and float(abs(samples).max()) > 0.001
            audio = {"specs": sound.specs, "samples": int(samples.size),
                     "pcm_sha256": hashlib.sha256(samples.tobytes()).hexdigest()}
        pair.append({"label": label, "file": str(path), "frame_sha256": images, "audio": audio})
    assert pair[0]["frame_sha256"] == pair[1]["frame_sha256"]
    assert pair[0]["frame_sha256"][0] != pair[0]["frame_sha256"][1]
    assert pair[0]["audio"] == pair[1]["audio"]
    results.append({"fixture": fixture, "video_decodes": True, "frames_match": True,
                    "audio_matches": has_audio, "silent_source": not has_audio, "details": pair})
(output / "decode-results.json").write_text(json.dumps(results, indent=2) + "\n")
print("SYNTHETIC_VIDEO_AND_AUDIO_DECODE_VERIFIED")
