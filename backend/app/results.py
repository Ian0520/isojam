"""Create-only local result bundles for the trusted POSIX worker exercise."""

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, BinaryIO, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.audio import validate_wav

MAX_MANIFEST_BYTES = 16 * 1024
COPY_BLOCK_BYTES = 64 * 1024
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
StemName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class ResultValidationError(ValueError):
    """Missing, conflicting or invalid result evidence; never ready to publish."""


class ResultConflictError(ResultValidationError):
    """An invocation already has different finalized bytes; do not overwrite."""


class ResultIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: UUID
    attempt_id: UUID
    invocation_id: UUID


class ResultProfile(BaseModel):
    """Trusted controller expectation, not an output's self-declared policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Annotated[str, Field(min_length=1, max_length=128)]
    required_stems: Annotated[tuple[StemName, ...], Field(min_length=1, max_length=16)]
    max_stem_bytes: PositiveInt
    max_total_bytes: PositiveInt
    max_duration_seconds: PositiveInt

    @model_validator(mode="after")
    def unique_stems(self):
        if len(set(self.required_stems)) != len(self.required_stems):
            raise ValueError("Expected stems must be unique")
        return self


FAKE_RESULT_PROFILE = ResultProfile(
    id="fake-pcm16-stereo-44100hz-16frames-v1",
    required_stems=(
        "vocals",
        "drums",
        "bass",
        "guitar",
        "piano",
        "other",
        "instrumental",
    ),
    max_stem_bytes=64 * 1024,
    max_total_bytes=7 * 64 * 1024,
    max_duration_seconds=1,
)


class StemArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stem: StemName
    key: Annotated[str, Field(min_length=1, max_length=256)]
    byte_count: PositiveInt
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResultManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    identity: ResultIdentity
    profile_id: Annotated[str, Field(min_length=1, max_length=128)]
    outputs: Annotated[tuple[StemArtifact, ...], Field(min_length=1, max_length=16)]


@dataclass(frozen=True)
class VerifiedArtifact:
    stem: str
    path: Path
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class VerifiedBundle:
    manifest: ResultManifest
    manifest_key: str
    manifest_sha256: str
    outputs: tuple[VerifiedArtifact, ...]


def _mkdir_durable(path: Path) -> None:
    """Persist each newly created directory entry, including the configured root."""
    if not path.exists():
        _mkdir_durable(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def _snapshot(file: BinaryIO) -> tuple[int, int, int, int, int]:
    metadata = os.fstat(file.fileno())
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inspect_wav(file: BinaryIO, profile: ResultProfile) -> tuple[int, str]:
    before = _snapshot(file)
    if not 0 < before[2] <= profile.max_stem_bytes:
        raise ResultValidationError("Result WAV exceeds its byte limit or is empty")
    digest = hashlib.sha256()
    total = 0
    file.seek(0)
    while chunk := file.read(min(COPY_BLOCK_BYTES, profile.max_stem_bytes - total + 1)):
        total += len(chunk)
        if total > profile.max_stem_bytes:
            raise ResultValidationError("Result WAV exceeds its byte limit")
        digest.update(chunk)
    file.seek(0)
    try:
        validate_wav(file, max_duration_seconds=profile.max_duration_seconds)
    except ValueError as error:
        raise ResultValidationError("Result contains invalid WAV audio") from error
    if _snapshot(file) != before or total != before[2]:
        raise ResultValidationError("Result WAV changed during verification")
    return total, digest.hexdigest()


@contextmanager
def _regular_file(directory: int, name: str) -> Iterator[BinaryIO]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ResultValidationError("Result artifact must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            yield file
    finally:
        os.close(descriptor)


class LocalResultStore:
    """Confine bundle paths and preserve finalized bytes through create-only writes.

    Directory/file descriptors refuse symlink traversal below the configured root.
    Files are read-only by convention, not immutable against a hostile OS owner.
    This trusted same-host store requires POSIX operations; remote storage needs
    conditional writes or immutable object versions in its own adapter.
    """

    def __init__(self, root: Path):
        if os.name != "posix":
            raise ValueError(
                "Local result storage requires POSIX filesystem operations"
            )
        if not root.is_absolute():
            raise ValueError("Result storage root must be absolute")
        # The configured storage root is trusted; manifest keys never choose it.
        self.root = root.resolve()

    @staticmethod
    def _components(identity: ResultIdentity) -> tuple[str, ...]:
        return (
            "jobs",
            str(identity.job_id),
            "attempts",
            str(identity.attempt_id),
            str(identity.invocation_id),
        )

    def artifact_key(self, identity: ResultIdentity, stem: str) -> str:
        return "/".join((*self._components(identity), f"{stem}.wav"))

    def manifest_key(self, identity: ResultIdentity) -> str:
        return "/".join((*self._components(identity), "manifest.json"))

    @contextmanager
    def _directory(self, identity: ResultIdentity, *, create: bool) -> Iterator[int]:
        if create:
            _mkdir_durable(self.root)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in self._components(identity):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(directory)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            yield directory
        finally:
            os.close(directory)

    @contextmanager
    def _staged_file(self, directory: int, name: str) -> Iterator[tuple[str, BinaryIO]]:
        temporary = f".{name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w+b") as file:
                yield temporary, file
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.fsync(directory)

    @staticmethod
    def _install(directory: int, temporary: str, name: str) -> bool:
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.fsync(directory)
        return True

    def write_bundle(
        self,
        identity: ResultIdentity,
        workspace: Path,
        *,
        profile: ResultProfile = FAKE_RESULT_PROFILE,
    ) -> VerifiedBundle:
        """Copy a complete private workspace; finalize manifest only after files.

        Identical retries preserve existing inodes/bytes. Conflicting artifacts
        never replace them. Interruption can leave files without a manifest;
        those files are not a ready result. No database operation belongs here.
        """
        try:
            source = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if set(os.listdir(source)) != {
                    f"{stem}.wav" for stem in profile.required_stems
                }:
                    raise ResultValidationError(
                        "Workspace must contain exactly the required stems"
                    )
                outputs = []
                total = 0
                with self._directory(identity, create=True) as directory:
                    for stem in profile.required_stems:
                        name = f"{stem}.wav"
                        with (
                            _regular_file(source, name) as input_file,
                            self._staged_file(directory, name) as (temporary, staged),
                        ):
                            before = _snapshot(input_file)
                            if not 0 < before[2] <= profile.max_stem_bytes:
                                raise ResultValidationError(
                                    "Workspace WAV exceeds its byte limit or is empty"
                                )
                            copied = 0
                            while chunk := input_file.read(
                                min(
                                    COPY_BLOCK_BYTES,
                                    profile.max_stem_bytes - copied + 1,
                                )
                            ):
                                copied += len(chunk)
                                if copied > profile.max_stem_bytes:
                                    raise ResultValidationError(
                                        "Workspace WAV exceeds its byte limit"
                                    )
                                staged.write(chunk)
                            staged.flush()
                            if _snapshot(input_file) != before:
                                raise ResultValidationError(
                                    "Workspace WAV changed while copying"
                                )
                            byte_count, digest = _inspect_wav(staged, profile)
                            total += byte_count
                            if total > profile.max_total_bytes:
                                raise ResultValidationError(
                                    "Result bundle exceeds its total byte limit"
                                )
                            artifact = StemArtifact(
                                stem=stem,
                                key=self.artifact_key(identity, stem),
                                byte_count=byte_count,
                                sha256=digest,
                            )
                            # Flush data and permissions before making its final name visible.
                            os.fchmod(staged.fileno(), 0o400)
                            os.fsync(staged.fileno())
                            if not self._install(directory, temporary, name):
                                with _regular_file(directory, name) as existing:
                                    actual = _inspect_wav(existing, profile)
                                if actual != (byte_count, digest):
                                    raise ResultConflictError(
                                        "Invocation already has different finalized WAV bytes"
                                    )
                            outputs.append(artifact)
                    manifest = ResultManifest(
                        identity=identity, profile_id=profile.id, outputs=tuple(outputs)
                    )
                    raw = json.dumps(
                        manifest.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    if len(raw) > MAX_MANIFEST_BYTES:
                        raise ResultValidationError(
                            "Result manifest exceeds its byte limit"
                        )
                    with self._staged_file(directory, "manifest.json") as (
                        temporary,
                        staged,
                    ):
                        staged.write(raw)
                        staged.flush()
                        os.fchmod(staged.fileno(), 0o400)
                        os.fsync(staged.fileno())
                        if not self._install(directory, temporary, "manifest.json"):
                            with _regular_file(directory, "manifest.json") as existing:
                                previous = existing.read(MAX_MANIFEST_BYTES + 1)
                            if previous != raw:
                                raise ResultConflictError(
                                    "Invocation already has a different finalized manifest"
                                )
            finally:
                os.close(source)
            return self.verify_bundle(identity, profile=profile)
        except OSError as error:
            raise ResultValidationError(
                "Result bundle could not be safely written"
            ) from error

    def verify_bundle(
        self, identity: ResultIdentity, *, profile: ResultProfile = FAKE_RESULT_PROFILE
    ) -> VerifiedBundle:
        """Verify a complete bundle using trusted identity/profile and bounded reads."""
        try:
            with self._directory(identity, create=False) as directory:
                with _regular_file(directory, "manifest.json") as file:
                    before = _snapshot(file)
                    if not 0 < before[2] <= MAX_MANIFEST_BYTES:
                        raise ResultValidationError(
                            "Result manifest exceeds its byte limit or is empty"
                        )
                    raw = file.read(MAX_MANIFEST_BYTES + 1)
                    if _snapshot(file) != before or len(raw) != before[2]:
                        raise ResultValidationError(
                            "Result manifest changed during verification"
                        )
                try:
                    manifest = ResultManifest.model_validate_json(raw)
                except ValidationError as error:
                    raise ResultValidationError("Invalid result manifest") from error
                if manifest.identity != identity or manifest.profile_id != profile.id:
                    raise ResultValidationError(
                        "Result identity/profile does not match the expected invocation"
                    )
                names = [artifact.stem for artifact in manifest.outputs]
                if len(names) != len(set(names)) or set(names) != set(
                    profile.required_stems
                ):
                    raise ResultValidationError(
                        "Manifest must contain exactly the required unique stems"
                    )
                verified = []
                total = 0
                for artifact in manifest.outputs:
                    if artifact.key != self.artifact_key(identity, artifact.stem):
                        raise ResultValidationError(
                            "Result key is outside its expected invocation location"
                        )
                    if artifact.byte_count > profile.max_stem_bytes:
                        raise ResultValidationError(
                            "Declared result exceeds its byte limit"
                        )
                    total += artifact.byte_count
                    if total > profile.max_total_bytes:
                        raise ResultValidationError(
                            "Result bundle exceeds its total byte limit"
                        )
                    with _regular_file(directory, f"{artifact.stem}.wav") as file:
                        actual = _inspect_wav(file, profile)
                    if actual != (artifact.byte_count, artifact.sha256):
                        raise ResultValidationError(
                            "Result WAV size or SHA-256 does not match its manifest"
                        )
                    verified.append(
                        VerifiedArtifact(
                            artifact.stem,
                            self.root / artifact.key,
                            artifact.byte_count,
                            artifact.sha256,
                        )
                    )
            return VerifiedBundle(
                manifest,
                self.manifest_key(identity),
                hashlib.sha256(raw).hexdigest(),
                tuple(verified),
            )
        except OSError as error:
            raise ResultValidationError(
                "Result bundle is missing or contains an unsafe file"
            ) from error
