# IsoJam

IsoJam is a music-practice web application that uses audio source separation to generate isolated instrument tracks and backing tracks from uploaded songs.

## Status

Early development.

The initial source-separation feasibility spike has been completed, with BS-RoFormer-SW selected as the current model for the first version.

The backend now supports an end-to-end WAV processing flow:

- upload an audio file
- create a processing job
- run BS-RoFormer source separation in the background
- retrieve job status and generated output metadata
- download generated stems through the API

Current limitations include in-memory job/upload metadata, WAV-only processing, and local filesystem storage.

## Documentation

- [Project Scope](docs/project-scope.md)
- [Model Compatibility Spike](docs/model-spike.md)