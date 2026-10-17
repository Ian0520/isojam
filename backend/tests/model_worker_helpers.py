"""CPU-only model sessions for real worker subprocess transport tests."""

import os
import time
from types import SimpleNamespace

import soundfile as sf

from app.model_processing import MODEL_STEMS


class CpuModel:
    def __init__(self, mode="success"):
        self.mode = mode
        print("model Python diagnostic", flush=True)
        os.write(1, b"model native stdout diagnostic\n")

    def infer(self, input_folder, *, store_dir):
        if self.mode == "failure":
            raise RuntimeError("synthetic model failure")
        if self.mode == "stall":
            time.sleep(60)
        input_path = next(input_folder.iterdir())
        audio, rate = sf.read(input_path)
        outputs = []
        for stem in MODEL_STEMS:
            path = store_dir / f"{input_path.stem}_{stem}.wav"
            sf.write(path, audio, rate, subtype="FLOAT")
            outputs.append(SimpleNamespace(output_id=stem, output_path=path))
        return SimpleNamespace(outputs=outputs)

    def close(self):
        if self.mode == "close-failure":
            raise RuntimeError("synthetic close failure after bundle")


def child_main(mode):
    from app.model_worker import main

    return main(model_session_factory=lambda: CpuModel(mode))
