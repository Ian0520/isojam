from pathlib import Path

import numpy as np
import soundfile as sf


class InvalidAudioError(ValueError):
    pass


class AudioTooLongError(ValueError):
    pass


def validate_wav(path: Path, *, max_duration_seconds: int) -> None:
    if max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be positive")
    try:
        with sf.SoundFile(path) as audio:
            if audio.format not in {"WAV", "WAVEX"} or audio.frames <= 0:
                raise InvalidAudioError("Invalid or unsupported WAV audio")
            if audio.channels not in {1, 2}:
                raise InvalidAudioError("WAV audio must be mono or stereo")
            if not 8000 <= audio.samplerate <= 96000:
                raise InvalidAudioError(
                    "WAV sample rate must be between 8000 and 96000 Hz"
                )
            if audio.frames > max_duration_seconds * audio.samplerate:
                raise AudioTooLongError("Audio exceeds the maximum allowed duration")
            decoded_frames = 0
            for block in audio.blocks(blocksize=65536, dtype="float32", always_2d=True):
                if not np.isfinite(block).all():
                    raise InvalidAudioError("WAV audio contains non-finite samples")
                decoded_frames += len(block)
            if decoded_frames != audio.frames:
                raise InvalidAudioError("Incomplete WAV audio")
    except sf.SoundFileError as error:
        raise InvalidAudioError("Invalid or unsupported WAV audio") from error
