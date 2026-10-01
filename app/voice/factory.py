"""Picks the configured STT/TTS backends. Mirrors app/llm/factory.py."""

from app.core.config import Settings
from app.voice.base import NullSpeechToTextClient, NullTextToSpeechClient, SpeechToTextClient, TextToSpeechClient
from app.voice.effects import EffectTTSClient
from app.voice.openai_compatible import OpenAICompatibleSTTClient, OpenAICompatibleTTSClient


def build_stt_client(settings: Settings) -> SpeechToTextClient:
    if settings.stt_provider == "openai_compatible":
        return OpenAICompatibleSTTClient(base_url=settings.stt_base_url, model=settings.stt_model)
    return NullSpeechToTextClient()


def build_tts_client(settings: Settings) -> TextToSpeechClient:
    if settings.tts_provider == "openai_compatible":
        client: TextToSpeechClient = OpenAICompatibleTTSClient(
            base_url=settings.tts_base_url, model=settings.tts_model, voice=settings.tts_voice, speed=settings.tts_speed
        )
        if settings.tts_effect:
            client = EffectTTSClient(client, settings.tts_effect)
        return client
    return NullTextToSpeechClient()
