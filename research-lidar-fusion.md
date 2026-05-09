# ScanBox AI — LiDAR / Point Cloud Fusion Research Notes

## 1. Overview
LiDAR and point cloud fusion are useful in ScanBox AI as an additional geometric source for NeRF and 3D Gaussian Splatting (3DGS).

The core idea is simple:
- RGB images provide color, texture, and photorealism.
- LiDAR / depth provide metric geometry, scale, and a more stable object shape.
- Joint processing reduces errors on low-texture, reflective, or geometrically complex objects.

For ScanBox AI, this means a more stable reconstruction pipeline, especially in cases where pure image-based NeRF / 3DGS starts to lose edges, scale, or shape.

---

## 2. What LiDAR adds to NeRF / 3DGS

### 2.1 Metric geometry
LiDAR provides distance measurements in real-world units. This is useful for:
- recovering scene scale;
- preserving object shape;
- reducing drift in multi-view reconstruction;
- calibrating the final mesh / splat structure.

### 2.2 Depth supervision
Depth from LiDAR can be used as an additional loss term during training.
This helps:
- stabilize optimization;
- reduce ambiguity in empty or homogeneous regions;
- improve object boundaries;
- accelerate convergence in early iterations.

### 2.3 Geometry prior
A point cloud can act as a geometric prior:
- initialize density or Gaussian centers;
- constrain surface shape;
- filter artifacts in background regions;
- support post-processing after training.

### 2.4 Denoising and completion
LiDAR does not replace RGB, but it works well as a geometric reference:
- helps suppress depth noise;
- fills weak regions where photometry is unstable;
- improves robustness in texture-poor scenes.

---

## 3. Main fusion strategies

### 3.1 Pre-fusion: before training
This is the most practical path for ScanBox AI.

Raw data is aligned into a shared space before reconstruction:
- RGB ↔ LiDAR calibration;
- transform the point cloud into the camera coordinate system;
- remove outliers;
- downsample / voxelize;
- generate LiDAR-derived depth maps for training.

Pros:
- easier to debug;
- better input quality control;
- good for an MVP.

Cons:
- more manual engineering;
- sensitive to extrinsic calibration errors.

### 3.2 Mid-fusion: during training
LiDAR participates directly in model optimization.

Typical variants:
- depth loss for NeRF;
- geometric regularization for 3DGS;
- pose refinement using LiDAR residuals;
- point-to-surface constraints.

Pros:
- better sensor usage;
- often higher shape accuracy.

Cons:
- harder to implement;
- more dependent on synchronization and calibration.

### 3.3 Post-fusion: after training
The RGB model is trained first, then refined with the point cloud.

Applications:
- mesh cleanup;
- scale correction;
- normal smoothing;
- hole filling;
- alignment with scan data.

Pros:
- minimally invasive;
- easy to layer onto an existing pipeline.

Cons:
- does not fix fundamental training errors.

---

## 4. Key technical question: alignment / extrinsics
Without correct LiDAR-RGB alignment, most of the benefit disappears quickly.

### 4.1 What must be aligned
- camera intrinsics;
- extrinsics between camera and LiDAR;
- timing synchronization;
- scale and coordinate frame for the scene.

### 4.2 Practical workflow
1. Capture a calibration target or use scene-based registration.
2. Estimate the rigid transform between LiDAR and camera.
3. Verify the projected LiDAR points in the image plane.
4. Compare projected depth with estimated depth.
5. Store the transform as part of the capture profile.

### 4.3 Risks
- a small yaw / pitch error can create visible edge artifacts;
- rolling camera exposure can break synchronization;
- rotating-table setups can introduce frame drift;
- reflective surfaces can produce LiDAR outliers or gaps.

For ScanBox AI, calibration should be treated as a dedicated pipeline stage rather than a one-time setup task.

---

## 5. How LiDAR helps NeRF and 3DGS

### 5.1 NeRF
LiDAR is especially useful as depth supervision.
It helps NeRF:
- recover shape faster;
- reconstruct flat / thin structures better;
- reduce floating artifacts;
- preserve absolute scale.

### 5.2 3D Gaussian Splatting
For 3DGS, LiDAR is useful during initialization and regularization.

Possible effects:
- a more reasonable initial geometry;
- better Gaussian placement on the object surface;
- fewer floating Gaussians in empty space;
- a more stable reconstruction on low-texture objects.

In practice, this makes 3DGS more predictable for product capture.

---

## 6. Recommended pipeline for ScanBox AI

### 6.1 Capture
Collect:
- RGB frames;
- LiDAR point cloud or depth;
- pose / timestamp;
- camera intrinsics;
- LiDAR-camera extrinsics;
- turntable angle, if used.

### 6.2 Preprocess
- temporal synchronization;
- frame filtering;
- outlier removal for the point cloud;
- point cloud downsampling;
- depth-map projection from LiDAR;
- background masking where possible.

### 6.3 Reconstruction
Recommended implementation order:
1. baseline RGB-only 3DGS / NeRF;
2. add LiDAR depth supervision;
3. use the point cloud as a geometry prior;
4. enable post-fusion mesh cleanup.

### 6.4 Export
- for web and preview: splats / lightweight mesh;
- for AR: mesh with scale metadata;
- for engineering: mesh + registered point cloud + calibration package.

---

## 7. Best use cases
LiDAR fusion is especially useful for:
- low-texture objects;
- glossy or partially reflective surfaces;
- objects with thin edges and complex geometry;
- cases where metric scale matters;
- industrial / QA capture;
- fast e-commerce scans that still need stable shape.

It is less useful for:
- fully hidden, strongly reflective, or transparent materials;
- very small objects where LiDAR noise is comparable to the object details;
- scenes where calibration is expensive and the benefit is small.

---

## 8. Caveats / limitations
1. **LiDAR does not replace RGB** — it adds geometry, but it does not provide good texture.
2. **Calibration cost** can be higher than expected.
3. **Sparse points** are not always enough for strong supervision on small objects.
4. **Reflective / transparent materials** remain problematic.
5. **Compute overhead** grows if the fusion model becomes too complex.

For an MVP, it is better to start with a reliable RGB + LiDAR depth-supervision setup than to build a highly complex multi-sensor system too early.

---

## 9. Practical recommendation
For ScanBox AI, the best strategy is:

- **Phase 1**: RGB-only 3DGS / NeRF baseline.
- **Phase 2**: add LiDAR as a geometry prior and depth loss.
- **Phase 3**: use the point cloud for scale correction and mesh cleanup.
- **Phase 4**: automate calibration and quality checks.

This sequence gives a good balance between quality, complexity, and MVP speed.

---

## 10. Recommended architectural role
LiDAR fusion is better treated as an enhancement layer inside the reconstruction pipeline rather than a separate product layer:

- capture layer collects RGB + LiDAR;
- preprocessing aligns and cleans the data;
- reconstruction uses depth / geometry priors;
- post-processing improves mesh and scale;
- export layer packages the final result.

This fits well with the ScanBox AI architecture and does not block future model replacement.

---

## 11. References / links
- Nerfstudio: https://docs.nerf.studio/
- 3D Gaussian Splatting: https://github.com/graphdeco-inria/gaussian-splatting
- Splatfacto / Nerfstudio method docs: https://docs.nerf.studio/nerfology/methods/splatfacto.html
- Survey: *How NeRFs and 3D Gaussian Splatting are Reshaping SLAM*: https://fabiotosi92.github.io/files/survey-slam.pdf
- Survey: *Review of extrinsic parameter calibration of LiDAR and camera*: https://www.sciencedirect.com/science/article/pii/S2096579625000762
- LiDAR-enhanced 3D Gaussian Splatting Mapping: https://arxiv.org/html/2503.05425v1
- Robust LiDAR-Camera Calibration With 2D Gaussian Splatting: https://ieeexplore.ieee.org/iel8/7083369/10935293/10933576.pdf
- Scalable Lidar-Visual Reconstruction with Neural Radiance Fields: https://arxiv.org/html/2403.06877v1
