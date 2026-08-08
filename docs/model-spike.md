# Model Compatibility Spike

## BS-RoFormer-SW

### Environment

- GPU: NVIDIA GeForce RTX 4060 Laptop GPU, 8 GB VRAM
- Model: BS-RoFormer-SW
- Device: CUDA

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

### Command

```bash
bs-roformer-infer \
    --input_folder ~/projects/isojam/uploads \
    --store_dir ~/projects/isojam/outputs/bs-roformer \
    --device cuda
```

### Performance

* **Processing time:** approximately 15.3 seconds
* **Result:** completed successfully
* **CUDA out-of-memory errors:** none

### Separation Result

The model successfully separated the major instrument categories in the test segment.

The guitar stem was subjectively high quality, and the other instruments were also separated correctly enough for the intended music-practice use case.

Both guitar parts were grouped into the single guitar stem.

### Observed Limitation

BS-RoFormer-SW separates by instrument category and produces only one guitar stem. It therefore does not separate the two individual guitar parts from this recording.


### Conclusion

