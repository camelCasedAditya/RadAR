# RadAR — Design (DubHacks 2026)

## Context

Live 3D situational awareness for first responders. Two responders walk a building with iPhones and AirPods. A mission-control hub shows, live: both video feeds, a 3D point-cloud model of everything the cameras have seen, each responder's position on that model, and the direction each responder's head is pointed. The 3D model is built from plain phone video by lingbot-map (ECCV 2026, Apache-2.0) running on a RunPod GPU. ARKit provides metric position; UWB provides an independent inter-responder distance check shown on the hub.

Marketing framing: first responders, SWAT, defence. The demo is the hub screen.

## Goals

1. Hub shows a growing colored point cloud of the building, built from phone video, updating within ~2 s of the camera seeing something.
2. Two responder markers move on that model at their true positions, with a head-direction cone each.
3. Both responders are in one coordinate frame with real metric scale.
4. Both live video feeds visible on the hub beside the 3D view.
5. Coverage view: floor cells no camera has mapped are shaded as blind spots.
6. UWB distance between the two phones shown on the hub next to the ARKit-derived distance, with a mismatch warning.

## Non-goals (future features, not built now)

- CV detection / response assistance
- Group voice call (hub ↔ responders)
- Notes / event pins on the model
- LiDAR layer or LiDAR scale correction
- GPS beyond a stored lat/long readout at session start
- Anything on-device beyond sensing and streaming

## Anti-goals

- Two point clouds visibly misaligned on the hub. Better to show one cloud than two that disagree.
- Hub freezing while the GPU catches up. Video and markers must keep moving even if the cloud lags.

## Hardware in hand

| Device | Role | Notes |
|---|---|---|
| iPhone 17 ×2 | Responder phones | UWB gen-2, no LiDAR |
| iPhone 17 Pro ×1 | Spare / third responder | UWB gen-2, LiDAR (unused) |
| AirPods Pro ×2 | Head tracking | `CMHeadphoneMotionManager` |
| MacBook | Runs relay + hub page | Judges watch this |
| RunPod GPU pod (24 GB+, e.g. A5000/4090/L4) | lingbot-map inference | CUDA 12.8 image |

Team: 3+ builders. Three parallel tracks: Swift app, Python relay + GPU worker, Three.js hub.

## Architecture

```
iPhone A ──┐  LAN WebSocket (JPEG @10fps + ARKit pose + head yaw/pitch + UWB range)
iPhone B ──┼──────────────► Relay (Python, FastAPI) on MacBook ──► Hub page (browser, Three.js)
           │                      ▲
           │     ngrok tunnel     │ frames down / aligned points up
           │                      ▼
           │               GPU worker (Python, lingbot-map) on RunPod — connects OUT as a WS client
```

Decisions:
- Relay runs on the MacBook. Phones, MacBook and hub share one network (phone hotspot), plain `ws://`, no TLS, no cloud dependency for markers/video. If the GPU dies the demo still shows markers, video, UWB.
- GPU worker is a WebSocket **client** of the relay, reached through one `ngrok` (or `cloudflared`) tunnel from the MacBook. RunPod pods have no stable inbound port; this shape needs none. Swapping GPU provider changes nothing.
- Hub page served by the relay at `http://<mac>:8000/`.

### Phone app (Swift, existing `RadAR` Xcode project)

Per phone, each session:
- `ARWorldTrackingConfiguration`. **Shared frame, primary method:** both phones tap "Start" while resting on one marked floor spot facing one marked wall; relay treats both ARKit origins as identical. ARKit collaborative sessions do NOT merge origins (each phone sees the other only as an `ARParticipantAnchor` in its own frame), so they are not the primary path. **Stretch:** enable collaboration and have each phone report the other's participant-anchor pose; relay solves a rigid A→B transform from (A's view of B, B's self pose) pairs and corrects drift between the two frames.
- `MultipeerConnectivity` session between phones is still used, only to exchange the UWB discovery token.
- Per frame at 10 fps: JPEG of the camera image (downscaled to 518×378 landscape, lingbot's native res), `camera.transform` (4×4, metric), `camera.intrinsics`, timestamp, device id.
- AirPods: `CMHeadphoneMotionManager` attitude → yaw/pitch, sent at ~10 Hz. Yaw is relative to AirPods' own start heading; app captures an offset at session start by assuming head faces the same way as the phone camera when the user taps "Start".
- UWB: `NISession` with the peer phone (discovery token exchanged over the same Multipeer session); send `distance` and `direction` when available, ~5 Hz.
- GPS: one `CLLocation` at session start, sent once.
- Message format: one JSON envelope per message with a `type` field; JPEG as a separate binary frame immediately after its pose message, tagged by frame id. Exact schema left to builder.

### Relay (Python, FastAPI + websockets)

- Accepts phone connections, fans every message out to the hub, and queues JPEG+pose pairs per phone for the GPU worker.
- Holds latest state per responder (pose, head, UWB, last frame) so a hub reconnect repaints instantly.
- Computes ARKit-derived inter-phone distance and compares with UWB; emits a `uwb_check` message with both values and a flag when they disagree by more than 0.5 m.
- Serves the hub page as static files.

### GPU worker (Python, lingbot-map)

- One `GCTStream` model instance per responder phone, each in its own process (two processes, same checkpoint, ~2× VRAM). A single instance cannot alternate phones: the KV cache has no save/restore (`clean_kv_cache()` is the only control). If VRAM or throughput is short, each process drops frames and processes the newest one, landing at ~2–4 fps per phone; markers and video stay at full rate regardless. Latency target of ~2 s applies at whatever fps the GPU sustains.
- Does **not** use `inference_streaming` (it needs the whole tensor up front). Instead replicates its loop live: buffer the first 8 frames, run `model.forward(scale_frames, num_frame_for_scale=8, num_frame_per_block=8, causal_inference=True)`, then per arriving frame `model.forward(frame, num_frame_for_scale=8, num_frame_per_block=1, causal_inference=True)`. Keyframe interval 6 to bound KV cache. Reference loop: `lingbot_map/models/gct_stream.py:432-505`.
- KV cache is trained to ~320 slots. When keyframe count nears the window, reset the cache and re-seed with the last 8 frames as new scale frames (poor-man's windowed mode). Points already emitted are kept on the hub; only the model restarts.
- Per frame output: `pose_enc [1,1,9]` → decode via `pose_encoding_to_extri_intri` (`lingbot_map/utils/pose_enc.py:72`) to camera-to-world; `world_points [1,1,H,W,3]` + `world_points_conf`.
- **Alignment to metric ARKit frame**: maintain a sliding window (last ~30 frames) of (lingbot camera position, ARKit camera position) pairs. Solve Sim(3) via Umeyama (numpy, ~20 lines) → scale, rotation, translation. Apply to this frame's `world_points`. Recompute every frame; the transform converges within the first few seconds of motion. Because both phones align to ARKit's shared frame, both clouds land in one frame.
- Emit per frame: aligned points subsampled to ≤20k with `conf` above threshold, as a binary Float32 xyz + Uint8 rgb buffer, plus `frame_id` and responder id. ARKit pose for the marker comes straight from the phone, not from lingbot.

### Hub page (browser, Three.js, no framework)

- Full-screen 3D view: accumulating `THREE.Points` per responder (append-only buffers, cap at ~3M points then drop oldest).
- Responder marker: colored capsule at ARKit position; head cone from AirPods yaw/pitch composed onto ARKit camera yaw. Trail line of last 60 s.
- Two video panels (latest JPEG as `<img>`), one per responder.
- Coverage overlay: 0.5 m floor grid in XZ; a cell is "mapped" once ≥ N points project into it; cells 4-neighbour-adjacent to mapped-but-unmapped are shaded as blind spots. Recomputed every 2 s on the client.
- Status strip: per responder fps, GPU latency, UWB vs ARKit distance with warning colour, GPS lat/long.
- Orbit camera; "follow responder" toggle.

## What's left to the builder

- Exact JSON schemas and binary layouts.
- Point subsampling strategy and confidence threshold (start at conf > 0.5, voxel 5 cm).
- Hub visual design.
- Whether to run two model instances or one; decide on first OOM.
- Reconnect/backpressure details.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| lingbot-map env setup eats hours (CUDA 12.8 torch, FlashInfer, checkpoint) | Spin up the RunPod pod first, run their `demo.py` on `example/courthouse` before any other code. SDPA fallback (`--use_sdpa`) if FlashInfer won't build. |
| Two phones drift apart from the shared start origin over a long walk | Keep demo walk under ~5 min; participant-anchor correction is the stretch fix. |
| Tunnel drops, GPU worker disconnects | Worker reconnects with backoff; relay buffers nothing, worker re-seeds scale frames on reconnect. |
| Sim(3) alignment unstable while standing still | Require ≥0.5 m of ARKit travel in window before applying; hold last good transform otherwise. |
| Venue Wi-Fi isolates clients or blocks the tunnel | Phone hotspot for all devices; `cloudflared` as tunnel fallback. |
| 2 model instances OOM on 24 GB | `--offload_to_cpu` equivalents, `num_scale_frames 2`, or run only phone A's cloud and keep phone B to marker + video. |

## Build order

Tracks run in parallel; numbers show dependencies.

1. [GPU] RunPod pod up, lingbot-map `demo.py` runs on example data. (verify: viser shows courthouse cloud)
2. [GPU] Live wrapper: feed a video frame-by-frame through the per-frame forward loop, dump xyz per frame. (verify: cloud grows frame by frame in viser)
3. [iOS] Phone app: ARKit + JPEG + pose over WebSocket to relay; relay prints. (verify: 10 fps arriving)
4. [Hub] Hub page: markers from ARKit pose, video panels, fed by relay replaying a recorded session. (verify: marker moves)
5. [GPU+Hub] GPU worker connects through tunnel, consumes relay frames, Umeyama alignment, points to hub. (verify: cloud appears where you walked, scale matches marker trail)
6. [iOS] Second phone, marked-start shared origin. (verify: two markers, two clouds overlap on a shared wall)
7. AirPods head cone. (verify: turn head, cone turns, phone still)
8. Coverage overlay. (verify: unexplored corridor shaded)
9. UWB range + mismatch flag. (verify: walk apart, numbers track)
10. GPS readout.

## Verification (end-to-end)

Walk a hallway loop with both phones. Pass if: cloud of the hallway forms in both colours and overlaps; markers track the walk and end where they started within ~1 m; head cones swing when heads turn; unexplored side rooms shaded; UWB and ARKit distances within 0.5 m while in line of sight.

