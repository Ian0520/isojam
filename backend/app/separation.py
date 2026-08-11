from pathlib import Path
from tempfile import TemporaryDirectory

def collect_output_paths(manifest) -> dict[str, Path]:
    paths = {}
    for output in manifest.outputs:
        paths[output.output_id] = Path(output.output_path)
    return paths

def separate_audio(
    input_path: Path,
    output_dir: Path,
    session,
) -> dict[str, Path]:
    if input_path.suffix.lower() != ".wav":
        raise ValueError("input must be a WAV file")
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        temp_input_path = temp_dir_path / input_path.name
        temp_input_path.symlink_to(input_path.resolve())
        manifest = session.infer(
        temp_dir_path,
        store_dir=output_dir
        )
    return collect_output_paths(manifest)
