"""Server-side voice transcription with faster-whisper ([decisions.md 2026-07-23]).

The :class:`Transcriber` loads the bundled ``base`` CTranslate2 model once from a local directory
and reuses it for every clip, so no network download ever happens in the container.
``faster_whisper`` is imported lazily inside the method — mirroring how the Claude Agent SDK is kept
out of the hermetic test build — so this module imports (and the route wiring is exercised) without
pulling the heavy ctranslate2/onnxruntime/av wheels. The route injects a :data:`TranscribeAudio`
seam, which tests replace with a fake, so the real model is never loaded under ``bazel test``.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any, Callable

# The injected seam the /audio route calls: raw clip bytes + declared content type -> transcript.
TranscribeAudio = Callable[[bytes, str], str]


class Transcriber:
    """Transcribe audio clips with a locally bundled faster-whisper ``base`` model.

    The model is loaded on first use and cached; construction is cheap and does not import
    ``faster_whisper`` or touch the model files, so wiring one in ``main.py`` never delays startup
    and never requires the model to be present until a clip is actually transcribed.
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_dir = str(model_dir)
        self._device = device
        self._compute_type = compute_type
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _load_model(self) -> Any:
        """Load and cache the WhisperModel from the local directory (offline, never a download)."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(
                        self._model_dir,
                        device=self._device,
                        compute_type=self._compute_type,
                        local_files_only=True,
                    )
        return self._model

    def transcribe(self, audio: bytes, content_type: str) -> str:
        """Decode the clip and return its concatenated transcript text (may be empty).

        ``content_type`` is accepted for interface parity with the seam; faster-whisper/PyAV sniff
        the container from the bytes, so no explicit format hint is needed.
        """
        model = self._load_model()
        segments, _info = model.transcribe(io.BytesIO(audio))
        return "".join(segment.text for segment in segments).strip()
