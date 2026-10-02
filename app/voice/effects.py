"""Post-processing of synthesized speech: an ffmpeg `-af` filter chain
(SIRISAI_TTS_EFFECT) run over every clip, whichever TTS backend made it --
so chat, the HUD, satellites and speak_in_current_room all sound the same.

Clips always leave as constant-bitrate MP3 with no per-file length header
(ffmpeg doesn't write the Xing header to a pipe). The HUD joins sentence
clips into one to play them without gaps, and Safari stops a joined file
at the length the first clip's header claims -- so even with no effect
configured, clips pass through here with a no-op filter when ffmpeg is
available (app/voice/factory.py).
"""

import asyncio
import logging

from app.voice.base import TextToSpeechClient

logger = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_SECONDS = 15


class EffectTTSClient(TextToSpeechClient):
    """Wraps another TTS client and filters its MP3 output through ffmpeg.

    The effect is cosmetic, so any ffmpeg failure (binary missing, a typo
    in the filter chain) falls back to the unprocessed audio rather than
    losing speech altogether."""

    def __init__(self, inner: TextToSpeechClient, audio_filter: str, ffmpeg: str = "ffmpeg") -> None:
        self.inner = inner
        self.audio_filter = audio_filter
        self.ffmpeg = ffmpeg

    async def synthesize(self, text: str) -> bytes:
        audio = await self.inner.synthesize(text)
        try:
            return await apply_audio_filter(audio, self.audio_filter, ffmpeg=self.ffmpeg)
        except Exception:
            logger.warning("TTS effect failed; using the unprocessed audio", exc_info=True)
            return audio

    async def aclose(self) -> None:
        await self.inner.aclose()


async def apply_audio_filter(mp3: bytes, audio_filter: str, *, ffmpeg: str = "ffmpeg") -> bytes:
    """Run `mp3` through `ffmpeg -af audio_filter`, returning MP3."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "mp3", "-i", "pipe:0",
        "-af", audio_filter,
        "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(mp3), timeout=_FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg exited {process.returncode}: {err.decode(errors='replace').strip()}")
    return out
