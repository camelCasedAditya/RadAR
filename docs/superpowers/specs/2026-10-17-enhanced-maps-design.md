# Enhanced maps: design

Date: 2026-10-17. **Superseded 2026-10-18:** the mesh + splat output looked worse than the original (pose drift turned the splat into fog), so Enhanced now keeps the original patches and only re-textures them from the sharpest nearby video frame; see README "Enhanced maps" and protocol.md kind 6. The rest of this document is the original design. Status: approved; revised the same day to run enhancement on AWS EC2 instead of the
RunPod pod (user decision), with a second EC2 box for live mapping as a follow-on.

## Context

A saved RadAR scan today is the live map frozen: 518-wide photo patches from LiDAR keyframes, 1 cm
points, and the responders' paths. It looks low-resolution, the patches overlap with seams and bumps,
and LiDAR cannot see glass, so windows and glass doors are holes. Meanwhile every session file
already contains the full 1920×1440 H.264 camera stream at 30 fps with an ARKit pose per frame, and
the upsampled LiDAR depth grid (173×131, stride 3, low confidence already zeroed) behind each patch.

The user wants an **Enhance** step that uses that stored video with AI models to build a second,
higher-quality version of a saved map, stored next to the original under the same name, with the hub
able to toggle between original and enhanced views.

## Goals

- Sharper: detail comes from the stored video (960-wide after the time-budget decision below, still
  about 3.5× the pixels of the 518-wide live patches), not from the live copies.
- Smoother: one continuous surface instead of 189 overlapping patches.
- Complete: glass and other LiDAR holes are filled from video, and glass appears as a **solid
  surface** in the mesh (a path cannot pass through a window; a measurement lands on the glass).
- Both kinds of output from one job: a photoreal view and a real geometric surface.
- Stored in S3 beside the original session, listed once in Past maps, togglable in the hub.
- Started by an explicit **Enhance** button on a past map, never automatically.

## Decisions the user made

- **Where it runs:** a dedicated **EC2 GPU instance** (`radar-enhance`, g6e.xlarge, one L40S 48 GB,
  us-east-2, Deep Learning Base AMI) that is **stopped between jobs**. The relay starts it when a job
  is queued; it powers itself off after 3 idle minutes. Live mapping is never touched. Provisioned
  through the AWS MCP connector once it is authenticated (the AWS CLI was used only for read-only
  checks). Cost: about $1.86 per running hour, on-demand (the account's spot quota is 0).
- **Two L40S boxes:** the G-instance quota is 8 vCPUs, exactly two g6e.xlarge running at once. The
  second one, `radar-live` (lingbot worker + relay + hub behind Caddy HTTPS on a fixed Elastic IP,
  `server/aws/setup_live.sh` + `live.sh`), replaced the RunPod pod on 2026-10-18; both boxes are
  started and stopped programmatically and talk to S3 and each other through instance roles.
- **Multi-phone sessions:** **per-phone outputs**. Each phone id gets its own mesh and splat in its own
  ARKit frame, overlaid exactly as the live map overlays them today.
- **Time budget: under 5 minutes** per phone. Defaults: frames decoded at half size (960×720), 2 cm
  TSDF voxels, ~3k splat iterations, at most 250 kept frames per phone.

## Non-goals and anti-goals

- No live enhancement while scanning; no change to the live pipeline, the phone app, or the session
  file format.
- No measurement or picking tools in the hub in this project (the hub has no raycasting today).
- Never touch the teammate's `worker.py`; the enhancer is a separate machine.
- No pose refinement: ARKit poses are used verbatim so the enhanced map sits exactly on the original
  and the paths (my call; drift shows as slight doubling).
- Anti-goal: an enhanced map that floats or is offset from the original.

## Constraints and facts verified

- Session files live in S3 (`radar-dubhacks-784917519323`, `sessions/<name>`, sidecar `<name>.json`).
  Bucket CORS verified: GET/HEAD from any origin, so the hub can fetch mesh and splat files straight
  from presigned S3 URLs.
- Hub is static Three.js 0.169 from a CDN import map. `@mkkellogg/gaussian-splats-3d` 0.4.7 imports
  bare `three`, so it works through the same import map; it runs with shared memory and GPU sort off
  because the relay sends no COOP/COEP headers.
- A pose carries 518×392 intrinsics; the H.264 frame with the same `f` is 1920×1440 (verified in
  `Streamer.swift`). Intrinsics are rescaled per axis.
- LiDAR depth exists only in kind-5 patches at patch keyframes, as "depth > 0" on the stride-3 grid.
  Sessions with no patches are refused with `failed: "needs a LiDAR scan"`.
- Open3D 0.20 on the Mac: the legacy `ScalableTSDFVolume` extracts nothing and the built-in GLB
  writer emits invalid files, so fusion uses the tensor `VoxelBlockGrid` (CPU on the Mac, CUDA on
  EC2) and the job writes its own minimal glTF binary.
- gsplat has no prebuilt wheel for torch 2.7/2.8 + CUDA 12.8; its kernels are compiled once during
  the EC2 setup (nvcc is on the Deep Learning AMI) and persist on the instance disk.

## What was built

### 1. The enhance job: `server/enhance/enhance.py <name>`

Per session, per phone: fetch the session file (local or S3); parse RADAR2 records (poses, kind-3
video, kind-5 patches); choose keyframes = the LiDAR patch frames (the patch `f` is the video `f`),
thinned to 250; decode the H.264 stream with PyAV at half size; per frame, resample the patch depth
to the frame, **plane-fill** large interior holes whose rim is planar within 3 cm (glass), then let
**Depth Anything V2 Large** complete the rest (relative inverse depth fitted to LiDAR by a two-pass
least squares); fuse all frames in an Open3D `VoxelBlockGrid` (2 cm voxels, 8 cm truncation), drop
components under 200 triangles, simplify to 500k triangles, write **`<name>.<rid>.mesh.glb`** with
vertex colours; train **gsplat** Gaussians initialised from the mesh vertices with an L1 + SSIM +
depth loss for ~3k iterations and write **`<name>.<rid>.splat`** (32 bytes per Gaussian); upload both
and rewrite the sidecar `enhanced: {status: done, phones, splat, bytes, seconds}` or
`{status: failed, error}`. Flags: `--local`, `--no-splat`, `--no-depth-model`, `--out`, `--iters`.

### 2. EC2 job loop and bootstrap: `server/enhance/enhance_worker.py`, `server/aws/`

- `enhance_worker.py` (systemd service on the instance): polls `s3://bucket/jobs/`, runs
  `enhance.py` per job in a subprocess, deletes the job, and after 3 idle minutes runs `shutdown -h`
  (instance shutdown behaviour = stop).
- `setup_enhancer.sh` (first boot, via user-data): venv with torch cu128, PyAV, Open3D, transformers,
  boto3, gsplat built from source; service pulls the latest code from `s3://bucket/code/enhance.tgz`
  at each boot.
- `enhancer.sh` (Mac): `deploy` (upload code), `up`, `down`, `status`, `logs`, `restart`, `ssh`.
- Provisioning (AWS MCP, pending connector auth): instance role with S3 access to the bucket only;
  security group SSH only; `InstanceInitiatedShutdownBehavior=stop`; 100 GB gp3; the `radar-pod` IAM
  user gains `ec2:StartInstances` + `ec2:DescribeInstances` on that one instance; the pod's
  `/workspace/radar-s3.env` gains `RADAR_ENHANCE_INSTANCE=<id>`.

### 3. Relay: `server/relay/relay.py`, `server/relay/store.py`

- `POST /maps/{name}/enhance`: 409 if unknown, still recording, or already queued/running. With S3:
  sidecar `enhanced.status = queued`, job object written, `StartInstances` on
  `RADAR_ENHANCE_INSTANCE` (missing env: job queued only). Without S3 (Mac dev): a local background
  `enhance.py --local --no-splat --no-depth-model` so the whole path is testable offline.
- Listing merges the S3 sidecar's `enhanced` into local entries (it did not before), and reads a local
  `<name>.json` sidecar for local jobs.
- `GET /maps/{name}` also serves `.glb` and `.splat`; `DELETE` removes everything with the session's
  prefix locally and in S3. `store.py` gained `keys/get_json/put_json/put_file/get_file`.

### 4. Hub: `hub/app.js`, `hub/index.html`

Past maps rows show **✦ Enhance** (or **✦ Retry** with the error as tooltip), "✦ queued…/running…"
with a 5 s re-poll while the list is open, or "✦ enhanced". Loading an enhanced map also loads each
phone's mesh (GLTFLoader, vertex colours) and splat (DropInViewer) and shows a **View** button cycling
original / photoreal / mesh; paths and markers stay on all three. Replay is unchanged.

## Verification

1. `cd server/enhance && python test_enhance.py`: synthetic session (wall at 2 m, box at 1.2 m, a
   LiDAR-blind window), `--local --no-splat --no-depth-model`; asserts wall, box, and the window
   filled as a solid plane in the GLB, and the sidecar `done`. Passes on the Mac in ~5 s.
2. `cd server/relay && python wire.py && python align.py && python test_link.py && python
   test_queue.py && python test_relay.py`: `test_relay.py` now also queues a local enhance job on a
   points-only session, sees it end `failed: needs a LiDAR scan`, retries, and deletes. Passes.
3. On AWS (after provisioning): `enhancer.sh deploy`, press Enhance on the Gates Undergrad Commons map
   in the pod-hosted hub, watch `enhancer.sh logs`; the instance starts, the job finishes in under
   5 minutes, the instance stops itself; the row flips to "✦ enhanced"; load it and cycle View;
   paths overlay; glass is a solid surface in the mesh view.
