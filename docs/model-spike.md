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

## Test 3 — TBD

To be completed with a deliberately challenging test track.

## Conclusion

To be completed after the compatibility tests.
