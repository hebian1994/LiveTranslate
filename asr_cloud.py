import base64
import io
import logging

import numpy as np
import soundfile as sf

from translator import make_openai_client

log = logging.getLogger("LiveTranslate.ASR.Cloud")

SAMPLE_RATE = 16000
DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3-asr-flash"


class CloudASREngine:
    """Speech-to-text via Alibaba Cloud Model Studio (DashScope) ASR API.

    Uses qwen3-asr-flash through the OpenAI-compatible chat/completions
    endpoint with a base64 WAV data URL. Synchronous per-segment calls fit the
    existing pipeline (VAD splits audio into a few-second segments, each sent
    to transcribe()).

    Implements the same interface as ASREngine for drop-in replacement.
    """

    def __init__(
        self,
        api_base=DEFAULT_API_BASE,
        api_key="env:DASHSCOPE_API_KEY",
        model=DEFAULT_MODEL,
        proxy="system",
        timeout=30.0,
    ):
        from translator import resolve_api_key

        if not resolve_api_key(api_key):
            raise ConnectionError(
                f"Cloud ASR: no API key resolved from {api_key!r}. "
                f"Set the DASHSCOPE_API_KEY environment variable or enter "
                f"the key in Settings."
            )
        self._client = make_openai_client(api_base, api_key, proxy, timeout=timeout)
        self._model = model
        self.language = None  # None = auto detect (omit the language param)
        # Verify connectivity cheaply; a bad key/endpoint should fail at load
        # time (with the load dialog visible) instead of on the first segment.
        try:
            self._client.models.list()
        except Exception as e:
            raise ConnectionError(
                f"Cloud ASR cannot reach {api_base}: {e}"
            ) from e
        log.info(f"Cloud ASR ready: {model} @ {api_base}")

    # --- ASRClient-compatible shim ----------------------------------------
    # Same pattern as RemoteASREngine: runs in the main process, presents the
    # ASRClient surface so _switch_asr_engine / _run_asr treat it uniformly.

    @property
    def status(self) -> str:
        return "ready"

    @property
    def pid(self):
        # No worker process; a None pid makes memory-monitor/recycle skip it.
        return None

    def shutdown(self):
        self.unload()

    def terminate(self):
        self.unload()

    def set_input_padding(self, pad_seconds):
        pass

    def set_language(self, language: str):
        old = self.language
        self.language = language if language != "auto" else None
        log.info(f"Cloud ASR language: {old} -> {self.language}")

    def to_device(self, device: str):
        log.info(f"Cloud ASR ignores device change (server-side): {device}")
        return True

    def unload(self):
        try:
            self._client.close()
        except Exception:
            pass
        log.info("Cloud ASR connection closed")

    def _encode_audio(self, audio: np.ndarray) -> str:
        if audio.dtype != np.int16:
            pcm16 = np.clip(audio, -1.0, 1.0)
            pcm16 = (pcm16 * 32767).astype(np.int16)
        else:
            pcm16 = audio
        buf = io.BytesIO()
        sf.write(buf, pcm16, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()

    def transcribe(
        self, audio: np.ndarray, word_timestamps: bool = False, **kwargs
    ) -> dict | None:
        """Send one VAD segment to the cloud ASR API and return the text."""
        if audio.size == 0:
            return None
        data_uri = self._encode_audio(audio)

        asr_options = {"enable_itn": True}
        if self.language:
            # Only set language when known; mixed-language audio must omit it.
            asr_options["language"] = self.language

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_audio", "input_audio": {"data": data_uri}}
                        ],
                    }
                ],
                extra_body={"asr_options": asr_options},
                stream=False,
            )
        except Exception as e:
            # Return None (like other engines on failure) so the pipeline drops
            # this segment but keeps running.
            log.error(f"Cloud ASR request failed: {e}")
            return None

        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return None

        duration = audio.size / SAMPLE_RATE
        usage = resp.usage
        log.debug(
            f"Cloud ASR: {duration:.2f}s -> {text[:60]} "
            f"(tokens: {getattr(usage, 'prompt_tokens', '?')}/"
            f"{getattr(usage, 'completion_tokens', '?')})"
        )
        # The API does not report the detected language; fall back to the
        # configured hint, or "auto" so the translator detects it.
        detected_lang = self.language or "auto"
        return {
            "text": text,
            "language": detected_lang,
            "language_name": detected_lang,
        }
