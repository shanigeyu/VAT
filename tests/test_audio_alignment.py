import tempfile
import unittest
import wave
from pathlib import Path

from production import FPS, audio_part, ffmpeg, tool


class AudioAlignmentTests(unittest.TestCase):
    def test_normalized_32khz_voice_fills_exact_timeline_length(self):
        try:
            tool("ffmpeg")
        except ValueError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "narration.wav"
            output = Path(folder) / "track.wav"
            seconds = 4.41
            frames = 111
            ffmpeg("-f", "lavfi", "-i", f"sine=frequency=350:sample_rate=32000:duration={seconds}",
                   "-c:a", "pcm_s16le", source)
            audio_part(source, output, frames, 100, limit=seconds, norm=True)

            with wave.open(str(output), "rb") as wav:
                self.assertEqual(wav.getframerate(), 48000)
                self.assertEqual(wav.getnchannels(), 2)
                self.assertEqual(wav.getnframes(), frames * (48000 // FPS))
