# ScanBox AI — NeRF Research Notes
## Technical overview

### 1. Overview
NeRF (Neural Radiance Fields) is a family of methods for representing a 3D scene as a continuous function that maps a 3D position and viewing direction to emitted color and density. In practice, the method is used for photorealistic view synthesis, novel-view rendering, and scene reconstruction from multi-view images.

For ScanBox AI, NeRF is important not as a standalone product, but as the reconstruction core inside a larger scanning pipeline:
- capture images / video from multiple views,
- estimate camera poses,
- train a scene representation,
- render previews or exports,
- optionally convert to mesh / splats for downstream use.

The main practical trade-off is:
- **NeRF**: strong visual quality, flexible, slower to train/render.
- **3D Gaussian Splatting**: faster training and real-time-ish rendering, often better for product preview.
- **Hybrid pipeline**: use splats for fast UX, NeRF for quality-sensitive scenes.

---

### 2. Frameworks and implementations

#### 2.1 Nerfstudio
Nerfstudio is the most useful “operator layer” for ScanBox AI because it standardizes the workflow around data parsing, pose estimation, training, evaluation, and export.

Key strengths:
- modular training pipeline,
- multiple method backends,
- active ecosystem for reconstruction experiments,
- good documentation and CLI tooling,
- supports NeRF-style methods and 3DGS variants.

Useful when you need:
- repeatable experiments,
- fast switching between methods,
- a clean interface for automated jobs,
- future extensibility.

#### 2.2 instant-ngp
instant-ngp (NVIDIA) is the canonical high-performance baseline for NeRF research and interactive demos.

Key strengths:
- extremely fast training,
- efficient hash-grid encoding,
- strong reference implementation for real-time experimentation,
- good for proving feasibility on limited datasets.

Limitations:
- narrower research/CLI surface than Nerfstudio,
- less suitable as a long-term product orchestration layer,
- some workflows are more “demo-first” than pipeline-first.

Best fit for ScanBox AI:
- benchmarking,
- internal quality/speed comparisons,
- validating hardware and scene constraints.

#### 2.3 gsplat / 3D Gaussian Splatting ecosystem
The gsplat ecosystem centers on fast differentiable Gaussian splatting and efficient rendering.

Key strengths:
- fast training and playback,
- excellent preview experience,
- practical for commercial visualization,
- good match for product scanning and web/mobile viewing.

Limitations:
- not identical to classic NeRF geometry behavior,
- mesh extraction is still a separate concern,
- appearance can be sensitive to capture quality.

Best fit for ScanBox AI:
- default MVP reconstruction mode,
- customer-facing preview,
- rapid iteration loops.

#### 2.4 Recommendation on framework choice
For ScanBox AI, use this order of preference:
1. **Nerfstudio** as the orchestration and research platform.
2. **3D Gaussian Splatting / gsplat** as the default product-facing rendering mode.
3. **instant-ngp** as a benchmark/reference path for speed and quality validation.

---

### 3. Practical use cases

#### 3.1 E-commerce product scanning
Goal: create visually pleasing object representations for catalog pages.

Why NeRF helps:
- preserves specular appearance better than simple mesh/texturing pipelines in many cases,
- supports smooth turntable-like viewing,
- works well when capture is controlled.

Primary success criteria:
- short capture time,
- low operator effort,
- stable view-dependent appearance,
- easy export to web viewers.

#### 3.2 AR / web preview
Goal: provide a lightweight visual asset for customer interaction.

Why NeRF/3DGS helps:
- produces strong visual quality with limited manual cleanup,
- fast enough for iterative content generation,
- can be packaged into viewer-friendly assets.

Primary success criteria:
- predictable scale,
- consistent lighting behavior,
- responsive preview on consumer hardware.

#### 3.3 Heritage / documentation / inspection
Goal: preserve objects or scenes with high-fidelity appearance.

Why NeRF helps:
- captures complex texture and view-dependent effects,
- works well for non-invasive documentation,
- useful when geometry precision is secondary to appearance.

#### 3.4 Engineering-adjacent scanning
Goal: produce a visual reference that may later be converted to geometry.

Important caveat:
- NeRF is not a CAD system.
- If metric accuracy is required, integrate depth sensing, calibration, and mesh reconstruction separately.

---

### 4. Math and core principles

#### 4.1 Radiance field representation
A radiance field is a function:

\[
F(\mathbf{x}, \mathbf{d}) \rightarrow (\mathbf{c}, \sigma)
\]

where:
- \(\mathbf{x}\) = 3D position,
- \(\mathbf{d}\) = viewing direction,
- \(\mathbf{c}\) = RGB color,
- \(\sigma\) = volume density.

This makes appearance view-dependent, which is one reason NeRF can model shiny and complex materials better than purely Lambertian pipelines.

#### 4.2 Volume rendering
Rendering integrates color along a camera ray:

\[
\mathbf{C}(r) = \int_{t_n}^{t_f} T(t)\,\sigma(t)\,\mathbf{c}(t)\,dt
\]

with transmittance:

\[
T(t) = \exp\left(-\int_{t_n}^{t} \sigma(s)\,ds\right)
\]

Interpretation:
- density accumulates opacity,
- color contribution is weighted by visibility along the ray,
- the model learns to reproduce training images by matching rendered rays.

#### 4.3 Positional encoding / scene encoding
Classical NeRF uses sinusoidal positional encoding to let an MLP represent high-frequency details.
Modern variants often replace or augment this with:
- multi-resolution hash grids,
- tri-planes,
- spherical harmonics,
- explicit Gaussian primitives.

This is the main reason modern systems train much faster than the original NeRF paper.

#### 4.4 Pose estimation dependency
NeRF assumes known or estimated camera intrinsics/extrinsics.
That means reconstruction quality depends heavily on:
- image overlap,
- accurate camera pose estimation,
- stable exposure / focus,
- low motion blur,
- enough parallax.

For ScanBox AI, capture quality is not a side detail; it is the main determinant of output quality.

#### 4.5 Practical constraints
NeRF-style methods work best when:
- the object is static,
- the camera path covers sufficient viewpoints,
- illumination is reasonably stable,
- background can be controlled or segmented.

They are weaker when:
- the object moves,
- reflections change unpredictably,
- exposure flickers,
- there is little viewpoint diversity.

---

### 5. Recommendations for ScanBox AI

#### 5.1 Default product strategy
Use a two-track reconstruction strategy:
- **Track A: 3DGS / gsplat** for fast preview and product UX.
- **Track B: NeRF** for high-quality scenes and fallback quality mode.

This gives the best balance of speed, visual fidelity, and operational simplicity.

#### 5.2 Pipeline recommendation
1. Capture controlled multi-view images.
2. Estimate and validate camera poses.
3. Run fast reconstruction first.
4. Render preview and quality checks.
5. If needed, run higher-quality NeRF refinement.
6. Export to viewer-friendly formats or derive mesh.

#### 5.3 MVP recommendation
For the first usable version:
- do **not** optimize for every special case,
- start with one well-controlled capture setup,
- use Nerfstudio as the orchestration entry point,
- prefer preview quality over perfect geometry.

#### 5.4 When to avoid NeRF-first
A NeRF-first approach is risky when the product requires:
- strict metric reconstruction,
- CAD-like geometry,
- many very different capture setups,
- fully real-time production rendering without GPU budget.

In those cases, use NeRF as a visual layer, not the authoritative geometry source.

#### 5.5 Operational priorities
For ScanBox AI, the top priorities should be:
- capture consistency,
- pose quality,
- automated quality scoring,
- reproducible job configs,
- simple export targets.

---

### 6. References and links

#### 6.1 Core projects
- Nerfstudio: https://docs.nerf.studio/
- Nerfstudio GitHub: https://github.com/nerfstudio-project/nerfstudio
- instant-ngp: https://github.com/NVlabs/instant-ngp
- gsplat: https://github.com/nerfstudio-project/gsplat

#### 6.2 Foundational papers
- Mildenhall et al., 2020. *NeRF: Representing Scenes as Neural Radiance Fields for View Synthesis.* https://arxiv.org/abs/2003.08934
- Müller et al., 2022. *Instant Neural Graphics Primitives with a Multiresolution Hash Encoding.* https://arxiv.org/abs/2201.05989
- Kerbl et al., 2023. *3D Gaussian Splatting for Real-Time Radiance Field Rendering.* https://arxiv.org/abs/2308.04079

#### 6.3 Practical guidance
- Nerfstudio tutorials and docs for dataset formats, pose estimation, and exporters.
- instant-ngp for speed baselines and internal benchmarks.
- 3DGS tooling for customer-facing preview and interactive rendering.

---

### 7. Short conclusion
NeRF is a strong reconstruction primitive for ScanBox AI, but it should be treated as one layer inside a larger capture-to-export system.

Best practical choice:
- **Nerfstudio** for orchestration,
- **3DGS / gsplat** for default fast preview,
- **NeRF** for quality-focused reconstruction and research depth.

This architecture fits the product goal: fast, repeatable, visually strong scanning with room to grow into more accurate reconstruction later.
