import app.separation as separation
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest

class FakeSession:
    def infer(self, input_folder, *, store_dir):
        files = list(Path(input_folder).iterdir())
        self.input_folder = input_folder
        self.store_dir = store_dir
        self.input_filenames = [path.name for path in files]
        self.input_is_symlink = files[0].is_symlink()

        fake_guitar_output = SimpleNamespace(output_id="guitar",
                                             output_path=store_dir / "test_guitar.wav")
        manifest = SimpleNamespace(outputs=[fake_guitar_output])

        return manifest

def test_collect_output_paths():
    fake_bass_output = SimpleNamespace(output_id="bass",
                                       output_path="data/output/test_bass.wav")
    fake_guitar_output = SimpleNamespace(output_id="guitar",
                                         output_path="data/output/test_guitar.wav")
    fake_manifest = SimpleNamespace(outputs=[fake_bass_output, fake_guitar_output])

    paths = separation.collect_output_paths(fake_manifest)

    assert paths["bass"] == Path("data/output/test_bass.wav")
    assert paths["guitar"] == Path("data/output/test_guitar.wav")

def test_separate_audio(tmp_path):
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio")
    output_dir = tmp_path / "output"

    session = FakeSession()

    returned = separation.separate_audio(input_path, output_dir, session)

    assert session.input_filenames == ["test.wav"]
    assert session.store_dir == output_dir
    assert returned["guitar"] == output_dir / "test_guitar.wav"
    assert session.input_is_symlink

def test_separate_audio_rejects_non_wav(tmp_path):
    input_path = tmp_path / "test.mp3"
    output_dir = tmp_path / "output"
    session = FakeSession()
    with pytest.raises(ValueError, match="input must be a WAV file"):
        separation.separate_audio(input_path, output_dir, session)
    assert not output_dir.exists()