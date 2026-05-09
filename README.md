# ScanBox AI

ScanBox AI is a modular 3D digitization project for turning raw capture data into usable digital assets for e-commerce, AR preview, and later engineering-grade workflows.

## Documents
- `technical-document.md` — core technical specification
- `architecture.md` — system architecture and data flow
- `research-nerf.md` — NeRF research notes
- `research-3dgs.md` — 3D Gaussian Splatting research notes
- `research-lidar-fusion.md` — LiDAR / point cloud fusion research notes

## Working principle
- capture RGB + optional LiDAR / depth data
- reconstruct with 3DGS as the default MVP path
- keep NeRF as the advanced quality mode
- use Cloud Processing Layer for heavy jobs when needed

## Current focus
The current documentation set covers architecture, reconstruction choices, and LiDAR integration. The next step is to expand the roadmap and open questions into a more product-oriented plan.
