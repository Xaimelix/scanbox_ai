# ScanBox AI — Architecture

### 1. Purpose
ScanBox AI is a modular 3D digitization platform that converts raw capture data into a usable digital asset for e-commerce, AR preview, documentation, and later engineering-grade workflows.

The architecture is designed to separate control, capture, reconstruction, cloud processing, and export so that each layer can evolve independently.

---

### 2. System Overview
The system is organized into five layers:

- **Control Layer** — handles user intent, orchestration, and workflow decisions.
- **Capture Layer** — collects RGB images, depth data, LiDAR / point clouds, and capture metadata.
- **Reconstruction Layer** — performs scene reconstruction with Nerfstudio, 3D Gaussian Splatting, or NeRF.
- **Cloud Processing Layer** — optionally offloads heavy training and optimization to GPU infrastructure.
- **Product / Export Layer** — packages the reconstructed asset into delivery formats such as GLB, USDZ, or viewer-ready outputs.

This separation keeps the MVP small while leaving room for later automation, scaling, and model selection.

---

### 3. Component Breakdown

#### 3.1 Control Layer
The Control Layer is the system entry point. It accepts a user command, interprets the requested scan mode, and decides which capture and reconstruction path to use.

Inputs:
- user command
- project presets
- capture constraints

Outputs:
- scan configuration
- device commands
- workflow state

Typical responsibilities:
- choose scan mode
- set quality profile
- trigger capture and reconstruction
- route jobs to local or cloud processing

#### 3.2 Capture Layer
The Capture Layer is responsible for collecting the source data needed for reconstruction.

Inputs:
- object scene
- capture configuration
- device calibration

Outputs:
- RGB image sequence
- LiDAR / depth data
- camera metadata
- device timestamps

Typical components:
- RGB camera
- LiDAR or ToF sensor
- turntable
- illumination controller
- capture controller on Raspberry Pi / ESP32 / Arduino

#### 3.3 Reconstruction Layer
The Reconstruction Layer is where the 3D scene is built.

Inputs:
- captured images
- depth / point cloud data
- calibration files
- camera poses or pose estimates

Outputs:
- reconstructed scene representation
- preview renders
- splats / mesh / scene artifacts
- optimization logs

Primary backend options:
- **Nerfstudio** for pipeline orchestration and NeRF experiments
- **3D Gaussian Splatting / gsplat** for fast, production-friendly preview and visualization
- **NeRF** for more challenging scenes and advanced reconstruction modes

#### 3.4 Cloud Processing Layer
The Cloud Processing Layer is optional, but important for heavier jobs.

Inputs:
- prepared dataset package
- job configuration
- model selection

Outputs:
- trained reconstruction result
- job status and logs
- exported scene artifacts

Typical responsibilities:
- run GPU-heavy training remotely
- queue multiple reconstruction jobs
- scale the pipeline when local hardware is not enough
- preserve processing state for resumable work

#### 3.5 Product / Export Layer
The Product / Export Layer turns the reconstruction result into something usable outside the pipeline.

Inputs:
- reconstructed scene
- preview renders
- quality metadata

Outputs:
- GLB
- USDZ
- web viewer assets
- embed-ready outputs
- delivery package for storage or CMS

Typical responsibilities:
- optimize assets for distribution
- generate preview assets
- prepare AR-ready files
- store or publish the final result

---

### 4. Data Flow
A typical ScanBox AI run follows this path:

1. The user issues a command.
2. The Control Layer converts the command into a scan workflow.
3. The Capture Layer records images, depth, and point cloud data.
4. The dataset is normalized and assembled for reconstruction.
5. The Reconstruction Layer trains or optimizes the chosen backend.
6. The result is previewed and optionally refined.
7. The Product / Export Layer generates the final assets.
8. The final package is delivered to storage, web preview, or AR distribution.

This flow is intentionally linear at the MVP stage, but every stage is isolated enough to be replaced later.

---

### 5. Core Technologies

#### 5.1 Nerfstudio
Nerfstudio is the main reconstruction framework for the system. It provides a modular environment for training, testing, and managing neural scene representations.

It is particularly useful for:
- end-to-end NeRF experimentation
- viewer integration
- capture pipeline normalization
- modular backend selection

#### 5.2 3D Gaussian Splatting / gsplat
3DGS is the preferred default for the MVP because it is fast, interactive, and well suited to preview-oriented workflows.

It is particularly useful for:
- real-time rendering
- commercial previews
- fast reconstruction iteration
- modern production tooling around Gaussian splats

#### 5.3 NeRF
NeRF remains important for advanced scenes where lighting, translucency, or view synthesis quality matters more than speed.

It is particularly useful for:
- complex reflections
- harder illumination conditions
- research-grade reconstruction
- fallback or specialized reconstruction modes

#### 5.4 LiDAR / Point Cloud Fusion
LiDAR is the geometric anchor of the system.

It is useful for:
- metric scale
- scene alignment
- depth supervision
- geometry priors
- denoising and robustness

In the ScanBox architecture, LiDAR should support reconstruction rather than replace image-based methods.

---

### 6. Integration Strategy
The recommended integration strategy is:

- **LiDAR for geometry**
- **RGB for appearance**
- **3DGS as the default MVP backend**
- **NeRF as the advanced backend**
- **Cloud Processing Layer as the best choice for heavy jobs and scalable reconstruction**

This gives the system a strong practical base without overcomplicating the first version.

For ScanBox AI, the cleanest path is to treat the capture stage as a multi-modal data source, the reconstruction stage as a selectable backend, and the export stage as a productization layer.

---

### 7. Deployment Model

#### 7.1 Local MVP
The first version should run locally with minimal infrastructure:
- camera
- turntable
- local orchestration
- reconstruction on a single machine when possible

This keeps iteration fast and makes the prototype easier to test.

#### 7.2 Cloud-Accelerated Mode
For larger scenes or heavier training runs, the dataset can be sent to a GPU-enabled cloud worker.

This mode is useful when:
- local GPU is weak or unavailable
- reconstruction jobs queue up
- higher-quality NeRF training is required
- multiple scenes need to be processed in parallel

#### 7.3 Hybrid Mode
The best long-term structure is hybrid:
- local capture and orchestration
- optional cloud reconstruction
- centralized export and delivery

---

### 8. Risks and Tradeoffs

#### 8.1 Data Quality
The largest risk is poor capture quality. Bad lighting, motion blur, and incomplete coverage will degrade reconstruction more than any backend choice.

#### 8.2 Complex Materials
Reflective, transparent, or low-texture objects remain difficult for both NeRF and 3DGS.

#### 8.3 Scope Growth
It is easy to over-scope by trying to build capture, reconstruction, cloud training, LLM orchestration, AR export, and mesh editing all at once.

#### 8.4 Compute Cost
Cloud reconstruction is powerful, but it introduces latency, queue management, and operational complexity.

---

### 9. Recommended MVP Architecture
The recommended MVP for ScanBox AI is:

- RGB camera
- turntable
- optional LiDAR / depth sensor
- 3D Gaussian Splatting / gsplat as the default backend
- Nerfstudio as the orchestration and experimentation layer
- Cloud Processing Layer for heavy jobs and scaling
- export to GLB and web preview first
- AR export later

This architecture is the smallest version that still gives a strong practical result.

---

### 10. Summary
ScanBox AI should be built as a modular scanning platform rather than a single reconstruction model.

The key architectural idea is:
- **capture data carefully**
- **use LiDAR to stabilize geometry**
- **use 3DGS for fast practical output**
- **keep NeRF for advanced quality-sensitive cases**
- **separate reconstruction from export and delivery**

This keeps the system understandable, extensible, and realistic for an MVP that can later grow into a full product.
