# Model Compatibility Spike

## Goal

Determine whether BS-RoFormer-SW is a viable source-separation model for IsoJam.

## Model

* **Model:** BS-RoFormer-SW
* **Checkpoint:** `BS-Rofo-SW-Fixed.ckpt`
* **Inference package:** `bs-roformer-infer 0.1.5`
* **PyTorch:** `2.13.0`
* **Device:** CUDA

## Environment

* **Operating environment:** WSL 2 Ubuntu
* **Python:** `3.12.3`
* **GPU:** NVIDIA GeForce RTX 4060 Laptop GPU
* **VRAM:** 8 GB

## Test 1 — 夜訪吸血鬼

### Test Material

* **Artist:** 五月天 (Mayday)
* **Track:** 夜訪吸血鬼
* **Segment:** 00:00–00:58
* **Duration:** 58 seconds

### Known Instrumentation

The tested segment contains:

* Vocals
* Bass
* Drums
* Piano
* Two guitar parts

The presence of multiple instruments and two separate guitar parts makes this segment useful for evaluating both general stem separation and guitar separation.

### Performance

* **Processing time:** approximately 15.3 seconds
* **Result:** completed successfully
* **CUDA out-of-memory errors:** none

### Separation Result

The model successfully separated the major instrument categories in the test segment.

The guitar stem was subjectively high quality, and the other instruments were also separated well enough for the intended music-practice use case.

Both guitar parts were grouped into the single guitar stem.

### Observed Limitation

BS-RoFormer-SW separates by instrument category and produces only one guitar stem. It therefore does not separate the two individual guitar parts from this recording.

## Test 2 — Paranoid Android

### Test Material

* **Artist:** Radiohead
* **Track:** Paranoid Android
* **Segment:** Full song
* **Duration:** 6:13

### Known Instrumentation

The song contains:

* Vocals
* Bass
* Drums
* Synth
* Keyboard
* Acoustic guitar
* Two electric guitar parts

The dense arrangement and multiple overlapping guitar parts make this a useful test of separation quality over a complete song.

### Performance

* **Processing time:** approximately 71.2 seconds
* **Result:** completed successfully
* **CUDA out-of-memory errors:** none

### Separation Result

The model produced subjectively good separation results across the full song.

Despite the relatively dense arrangement and presence of multiple guitar parts, the generated stems were sufficiently well separated for the intended music-practice use case.

As in Test 1, guitar parts belonging to the same instrument category were grouped into the model's single guitar stem rather than separated into individual guitar tracks.

## Test 3 — 溫柔 [還你自由版]

### Test Material

* **Artist:** 五月天 (Mayday)
* **Track:** 溫柔 [還你自由版]
* **Segment:** Full song
* **Duration:** 7:06

### Known Instrumentation

The song contains:

* Vocals
* Bass
* Drums
* Piano
* Two electric guitar parts

A particularly dense section contains vocals, piano, bass, drums, and two distorted electric guitars playing simultaneously. This section provides a stress case for evaluating separation quality under heavy instrumental overlap.

### Performance

* **Processing time:** approximately 100.8 seconds
* **Result:** completed successfully
* **CUDA out-of-memory errors:** none

### Separation Result

The model produced subjectively good separation results across the full song.

No obvious stem leakage was observed, including during the dense section containing vocals, piano, bass, drums, and two distorted electric guitars simultaneously.

The separated instruments remained sufficiently clear for the intended music-practice use case.

As in the previous tests, multiple guitar parts were grouped into the model's single guitar stem.

### Observed Quality Limitation

Across all three test tracks, separation quality decreased somewhat during heavily layered sections where many instruments played simultaneously.

The degradation was noticeable in the audio quality of the separated stems, but the outputs remained subjectively good and usable for the intended practice use case.

## Conclusion

BS-RoFormer-SW is a viable source-separation model for the initial version of IsoJam.

Across three test cases with different arrangements and durations, the model:

* Successfully completed CUDA inference without out-of-memory failures
* Produced useful separation of the major instrument categories
* Handled full-length tracks of more than six and seven minutes
* Produced good results even in dense, multi-instrument sections
* Produced guitar stems suitable for the intended practice workflow

The primary observed limitation is reduced stem quality during heavily layered sections. In addition, the model separates by instrument category, so multiple guitar parts are combined into a single guitar stem rather than separated individually.

These limitations are acceptable for the initial version of IsoJam.

BS-RoFormer-SW will therefore be used as the current source-separation model. The choice can be revisited later if practical testing reveals additional limitations or if future features require more granular source separation.
