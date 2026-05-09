# ScanBox AI — 3D Gaussian Splatting Research Notes

### 1. Overview
3D Gaussian Splatting (3DGS) is a scene representation and rendering approach that stores a scene as a set of anisotropic Gaussian primitives in 3D space. In practice it has become one of the most useful options for fast, photorealistic reconstruction from multi-view images.

For ScanBox AI, 3DGS is a strong fit for the default reconstruction path because it offers:
- fast training compared with classic NeRF pipelines,
- real-time or near-real-time rendering,
- high visual quality for product scanning,
- a simpler operational story for preview and web/mobile delivery.

It is especially attractive when the goal is not only geometry, but a convincing visual asset for e-commerce, AR preview, and remote review.

---

### 2. Core ecosystem

#### 2.1 Graphdeco / Inria reference implementation
The official Inria implementation is the foundational 3DGS codebase.
It introduced the canonical formulation and remains the key reference for methodology and benchmarking.

Why it matters:
- defines the baseline algorithm,
- useful for understanding the original training and rendering pipeline,
- important when validating quality against academic results.

Reference:
- https://github.com/graphdeco-inria/gaussian-splatting
- https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/

#### 2.2 nerfstudio-project/gsplat
`gsplat` is the CUDA-accelerated rasterization backend and research/runtime layer used in the Nerfstudio ecosystem.
It is practical for production experimentation because it focuses on speed, stability, and integration.

Why it matters:
- efficient splatting/rasterization,
- good fit for training and inference pipelines,
- easier integration with Nerfstudio tooling than a raw research repo.

Reference:
- https://github.com/nerfstudio-project/gsplat
- https://docs.gsplat.studio/

#### 2.3 Nerfstudio / Splatfacto
Nerfstudio packages a usable 3DGS workflow in the form of Splatfacto.
This is likely the best starting point for ScanBox AI because it reduces the amount of custom plumbing needed around camera poses, training, evaluation, and visualization.

Reference:
- https://docs.nerf.studio/nerfology/methods/splat.html

#### 2.4 SplaTAM
SplaTAM extends the Gaussian splatting idea into dense RGB-D SLAM, combining tracking and mapping.
This is relevant when ScanBox AI needs live capture feedback, online pose estimation, or a scanning workflow that benefits from depth sensors.

Reference:
- https://github.com/spla-tam/SplaTAM
- https://spla-tam.github.io/

#### 2.5 SuGaR
SuGaR focuses on converting Gaussian splat reconstructions into surface-aligned, mesh-friendly representations.
This is important when the output must be edited, exported, or used in downstream geometry workflows.

Reference:
- https://github.com/Anttwo/SuGaR

#### 2.6 4D-GS ecosystem
4D Gaussian Splatting extends the idea to dynamic scenes and temporal content.
This is relevant for future ScanBox AI features such as scanning moving objects, capturing deformation, or generating time-varying scenes.

References:
- https://github.com/fudan-zvg/4d-gaussian-splatting
- https://github.com/hustvl/4DGaussians

---

### 3. Practical use cases for ScanBox AI

#### 3.1 Product visualization
Best fit for:
- shoes,
- cosmetics,
- accessories,
- consumer electronics,
- decorative objects.

Value:
- very good visual realism,
- fast preview generation,
- strong browser/mobile experience.

#### 3.2 E-commerce pipeline
3DGS is useful when the business wants a convincing turntable-like viewer or interactive product page without waiting for full mesh cleanup.

Recommended output:
- rendered preview images,
- embedded web viewer,
- GLB/mesh only if geometry is good enough.

#### 3.3 Capture QA and operator feedback
Because splats render quickly, they are useful for quick checks after acquisition:
- missing views,
- exposure mismatch,
- insufficient coverage,
- pose or alignment problems.

#### 3.4 RGB-D assisted scanning
When depth data is available, 3DGS can be paired with SLAM-like pose estimation or used as a target representation after depth-guided capture.
This is a good fit for ScanBox AI if the hardware stack includes RGB-D sensors.

---

### 4. Math and principles

#### 4.1 Scene representation
The scene is approximated by a set of Gaussians. Each primitive typically stores:
- center position `\mu` in 3D,
- covariance / anisotropic shape `\Sigma`,
- opacity `\alpha`,
- color parameters, often view-dependent via spherical harmonics or a similar basis.

The goal is to render the scene by projecting and compositing these Gaussians onto the image plane.

#### 4.2 Intuition
Instead of representing a scene with voxels or an implicit neural field, 3DGS uses many small oriented blobs.
Each blob contributes to pixels where it projects, and the final image is formed by alpha blending along the view direction.

This gives two important properties:
- fast rendering on GPUs,
- flexible representation of fine detail.

#### 4.3 Optimization loop
Training typically alternates between:
1. initializing Gaussians from sparse structure or point samples,
2. optimizing positions, shapes, opacities, and appearance,
3. densifying or pruning primitives based on contribution and error.

This makes the representation adaptive: important surfaces get more detail, while irrelevant regions are reduced.

#### 4.4 Why it works well visually
3DGS excels because it combines:
- explicit geometry-like primitives,
- differentiable rendering,
- GPU-friendly splatting.

The result is often smoother operationally than NeRF for interactive preview, especially when the target is a product-like object rather than a perfectly watertight CAD asset.

---

### 5. Strengths and limitations

#### Strengths
- very fast training and rendering,
- strong visual fidelity,
- great for interactive previews,
- good fit for web-based product viewing,
- practical for iterative scanning workflows.

#### Limitations
- not automatically a clean mesh,
- geometry can be less explicit than surface reconstruction,
- transparent, reflective, and thin structures remain challenging,
- dynamic scenes require extended methods such as 4D-GS,
- export to standard asset pipelines may require post-processing.

---

### 6. Recommendations for ScanBox AI

#### 6.1 Default reconstruction path
Use Nerfstudio + Splatfacto as the primary pipeline for MVP.
Reason: lowest integration cost with strong quality/performance balance.

#### 6.2 Sensor strategy
If hardware allows, combine RGB with depth for better pose stability and capture QA.
This helps with:
- easier initialization,
- better scale handling,
- stronger reconstruction of object boundaries.

#### 6.3 Product strategy
Treat 3DGS output as the primary deliverable for preview and presentation, not necessarily as the final geometry source.
A good production pattern is:
- 3DGS for visual quality and fast delivery,
- mesh extraction only when downstream tooling requires it.

#### 6.4 Geometry export
If ScanBox AI needs editable surfaces, add a post-processing step with SuGaR or similar mesh-oriented conversion.
This avoids forcing the core reconstruction system to solve mesh extraction too early.

#### 6.5 Dynamic capture roadmap
For future versions, consider 4D-GS only after the static scanning pipeline is stable.
That keeps the MVP focused and reduces complexity.

#### 6.6 Online capture / SLAM
If real-time guidance is needed during acquisition, evaluate SplaTAM or a similar RGB-D tracking layer.
This is especially relevant for guided scanning rigs and operator-assisted capture.

---

### 7. Suggested architecture role in ScanBox AI
Recommended placement:

- **Capture layer**: RGB / RGB-D camera, lighting, turntable
- **Pose / QA layer**: optional SLAM or pose estimation
- **Reconstruction layer**: Nerfstudio + Splatfacto / gsplat
- **Post-processing layer**: filtering, mesh extraction, export
- **Delivery layer**: web viewer, GLB/USDZ, preview images

This keeps 3DGS as the visual reconstruction core while leaving geometry conversion and platform delivery as separate concerns.

---

### 8. References and links
- Graphdeco / Inria official implementation: https://github.com/graphdeco-inria/gaussian-splatting
- Original project page: https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/
- Nerfstudio Splatfacto docs: https://docs.nerf.studio/nerfology/methods/splat.html
- gsplat repository: https://github.com/nerfstudio-project/gsplat
- gsplat docs: https://docs.gsplat.studio/
- SplaTAM: https://github.com/spla-tam/SplaTAM
- SplaTAM project page: https://spla-tam.github.io/
- SuGaR: https://github.com/Anttwo/SuGaR
- 4D Gaussian Splatting: https://github.com/fudan-zvg/4d-gaussian-splatting
- 4DGaussians: https://github.com/hustvl/4DGaussians
