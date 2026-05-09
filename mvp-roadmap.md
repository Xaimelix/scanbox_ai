# ScanBox AI — MVP Roadmap

## 1. Roadmap Goal
The goal of the MVP roadmap is to define the smallest practical version of ScanBox AI that can deliver a useful 3D scanning result with a clean production path.

The MVP should prove that the system can:
- capture objects reliably,
- reconstruct them with a usable 3D pipeline,
- export a usable asset,
- and keep the architecture open for later scaling.

---

## 2. MVP Strategy
The MVP should be intentionally narrow.

Recommended scope:
- RGB camera capture,
- optional LiDAR / depth input,
- rotating table or equivalent controlled capture setup,
- 3D Gaussian Splatting as the default reconstruction path,
- Nerfstudio as the orchestration and experimentation layer,
- Cloud Processing Layer for heavier jobs when needed,
- export to GLB and web preview first.

What should **not** be in the MVP:
- full LLM-driven autonomy,
- advanced mesh editing tools,
- dynamic scene reconstruction,
- large-scale multi-object batch automation,
- complex AR product flows beyond basic export.

---

## 3. Roadmap Phases

### Phase 1 — Capture Prototype
Objective: prove that the system can reliably collect usable source data.

Deliverables:
- camera capture pipeline,
- controlled lighting setup,
- turntable control or equivalent object rotation,
- basic capture metadata storage,
- calibration workflow for camera and optional LiDAR.

Success criteria:
- stable image sets across multiple runs,
- no major synchronization issues,
- predictable object coverage,
- repeatable capture conditions.

### Phase 2 — Reconstruction Baseline
Objective: produce the first working 3D reconstruction output.

Deliverables:
- dataset packaging step,
- Nerfstudio-based reconstruction pipeline,
- 3DGS / gsplat default backend,
- preview generation,
- basic quality checks.

Success criteria:
- the system produces a visible 3D result from captured data,
- the output is stable enough for review,
- the pipeline can be rerun without manual repair each time.

### Phase 3 — Export and Delivery
Objective: turn reconstruction into a usable asset.

Deliverables:
- GLB export,
- web preview assets,
- optional USDZ path for later AR use,
- result packaging and storage layout.

Success criteria:
- exported assets are readable by downstream tools,
- preview delivery is fast and simple,
- asset metadata stays attached to the output.

### Phase 4 — LiDAR Enhancement
Objective: improve reconstruction robustness and metric consistency.

Deliverables:
- LiDAR / depth alignment,
- point cloud preprocessing,
- geometry prior integration,
- depth supervision for reconstruction,
- mesh / scale cleanup.

Success criteria:
- better scale stability,
- stronger reconstruction on low-texture objects,
- fewer geometric artifacts,
- clearer alignment between RGB and depth data.

### Phase 5 — Cloud Processing Layer
Objective: move heavy jobs out of the local machine when needed.

Deliverables:
- GPU job submission flow,
- remote reconstruction execution,
- job queue or task tracking,
- result retrieval and packaging.

Success criteria:
- larger scenes can be processed without local hardware blocking the pipeline,
- the same project can run locally or remotely,
- job status is observable and resumable.

### Phase 6 — Operational Hardening
Objective: make the MVP reliable enough for repeated use.

Deliverables:
- clearer error handling,
- calibration validation,
- capture quality checks,
- consistent file naming and output layout,
- documentation cleanup.

Success criteria:
- fewer manual fixes,
- easier debugging,
- repeatable results across runs,
- smoother handoff to users or future contributors.

---

## 4. Priority Order
Recommended order of implementation:

1. Capture prototype
2. Reconstruction baseline
3. Export and delivery
4. LiDAR enhancement
5. Cloud Processing Layer
6. Operational hardening

This order keeps the project useful early while preserving the path to a more advanced system later.

---

## 5. MVP Deliverables
The first useful version of ScanBox AI should include:
- a documented capture workflow,
- a working reconstruction path,
- a basic export path,
- a clean architecture description,
- and a small set of research notes that justify the technical choices.

In practical terms, the MVP is successful when a user can scan an object, get a reconstruction, and export a usable asset without needing to understand the internals of the pipeline.

---

## 6. Risks and Tradeoffs
The main risks are:
- poor capture quality,
- calibration issues,
- over-scoping the reconstruction backend,
- combining too many experimental features too early,
- and underestimating the work required to make export outputs practical.

The roadmap should stay focused on one strong path: build a reliable scan-to-asset pipeline first, then extend it.

---

## 7. Next Steps After the MVP
Once the MVP is stable, the next extensions should be:
- improved UI / operator workflow,
- smarter scan presets,
- more automated calibration,
- richer AR export support,
- advanced NeRF mode for difficult scenes,
- and more production-grade cloud scaling.

---

## 8. Summary
The ScanBox AI MVP should prove that the system can move from capture to usable asset with a small but coherent stack.

The best practical path is:
- RGB capture first,
- 3DGS reconstruction second,
- export third,
- LiDAR refinement and cloud scale after the core pipeline is stable.

This keeps the project grounded, shippable, and open for growth.
