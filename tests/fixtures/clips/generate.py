"""Generate tiny non-personal MP4 fixtures using an existing Blender installation.
Run: blender --background --factory-startup --python tests/fixtures/clips/generate.py
No footage or network is used. Files are deliberately small but decodable.
"""

import math
import struct
import wave
from pathlib import Path

import bpy

root = Path(__file__).resolve().parent
root.mkdir(parents=True, exist_ok=True)
wav = root / "synthetic-tone.wav"
with wave.open(str(wav), "wb") as file:
    file.setnchannels(1)
    file.setsampwidth(2)
    file.setframerate(16000)
    file.writeframes(
        b"".join(struct.pack("<h", int(3000 * math.sin(i * 2 * math.pi * 440 / 16000))) for i in range(16000))
    )
scene = bpy.context.scene
scene.render.resolution_x = 128
scene.render.resolution_y = 72
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.frame_start = 1
scene.frame_end = 12
scene.render.image_settings.media_type = "VIDEO"
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
scene.render.ffmpeg.audio_codec = "AAC"
scene.render.ffmpeg.audio_channels = "MONO"
scene.render.ffmpeg.audio_mixrate = 48000
editor = scene.sequence_editor_create()
color = editor.strips.new_effect("Synthetic color", "COLOR", channel=1, frame_start=1, length=12)
color.color = (0.1, 0.3, 0.8)
color.keyframe_insert(data_path="color", frame=1)
color.color = (0.8, 0.3, 0.1)
color.keyframe_insert(data_path="color", frame=12)
sound = editor.strips.new_sound("Synthetic tone", str(wav), channel=2, frame_start=1)
scene.render.filepath = str(root / "tone.mp4")
bpy.ops.render.render(animation=True)
editor.strips.remove(sound)
scene.render.ffmpeg.audio_codec = "NONE"
scene.render.filepath = str(root / "silent.mp4")
bpy.ops.render.render(animation=True)
wav.unlink()
print("SYNTHETIC_FIXTURES_COMPLETE")
