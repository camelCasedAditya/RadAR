# RadAR — Implementation Plan (Core + Add-ons)

## Context

Design approved in `docs/superpowers/specs/2026-10-17-radar-design.md`. This plan splits delivery into one **Core** milestone that produces a complete, demoable hub, followed by **add-on modules** that each build only on Core and never on each other. Any add-on can be built, skipped, or reordered without touching the rest. Three builders work three tracks in parallel inside Core; after Core, add-ons are handed out one per person.

Repo today: empty SwiftUI template (`RadAR/RadARApp.swift`, `RadAR/ContentView.swift`). Everything below is new code.

## Repo layout (created in Core)

```
RadAR/                 iOS app (Swift, existing Xcode project)
server/relay/          Python FastAPI relay, runs on MacBook
server/gpu/            Python lingbot-map worker, runs on RunPod
hub/                   static Three.js page, served by relay
docs/superpowers/specs/  design doc
```

Message schema is defined once in `server/relay/protocol.md` during Core and every module reads from it.

---

## CORE — "two people, one map"

Done when: both phones stream to the hub, two markers move on a shared metric frame, both video feeds show, and a growing point cloud from lingbot-map sits under the markers at the right scale. No AirPods, UWB, coverage, or GPS yet.

### Track G (GPU) — `server/gpu/`

G1. RunPod pod (24 GB+, CUDA 12.8 image). Clone lingbot-map, install per README, download checkpoint, run `demo.py --image_folder example/courthouse`.
   Verify: viser at :8080 shows the courthouse cloud.

G2. `server/gpu/live_model.py`: class `LiveReconstructor` wrapping `GCTStream` (`lingbot_map/models/gct_stream.py`). Methods: `push(frame_rgb) -> (c2w_4x4, points_xyz, points_rgb, conf)`. Internally: buffer 8 scale frames → one `forward(..., num_frame_per_block=8, causal_inference=True)`; thereafter one `forward(frame, num_frame_per_block=1, causal_inference=True)` per push, keyframe interval 6 via `_set_skip_append`. Decode pose with `pose_encoding_to_extri_intri` (`lingbot_map/utils/pose_enc.py:72`). Reset + re-seed with last 8 frames when keyframe count reaches 300.
   Verify: `python live_model.py --video some.mp4` prints growing point counts and viser shows cloud accumulating frame by frame.

G3. `server/gpu/align.py`: `umeyama(src_pts, dst_pts) -> (s, R, t)` in numpy. `Aligner` keeps last 30 (lingbot cam pos, ARKit cam pos) pairs; returns identity until ARKit travel in window ≥ 0.5 m; otherwise Sim(3). One `assert`-based self-check at bottom: random Sim(3) applied to random points is recovered.
   Verify: `python align.py` passes.

G4. `server/gpu/worker.py`: one process per responder id (`--responder A`). WebSocket client to relay (`ws://<tunnel>/gpu/<id>`). Receives pose JSON + JPEG binary, keeps only newest if behind, pushes through `LiveReconstructor`, aligns, subsamples (conf > 0.5, voxel 5 cm, ≤ 20k pts), sends binary `points` message. Reconnect with backoff; on reconnect re-seed scale frames.
   Verify: with relay replaying a recorded session (see R3), points arrive at hub.

### Track R (Relay) — `server/relay/`

R1. `protocol.md`: message types `hello`, `pose`, `frame` (binary), `points` (binary), `state`. Field list for each. Written first; iOS and hub builders read it.

R2. `relay.py` (FastAPI + `websockets`): endpoints `/phone/{id}`, `/hub`, `/gpu/{id}`. Fan-out phone → hub and phone → gpu; gpu → hub. Latest-state cache per responder; sent to hub on connect. Serves `hub/` at `/`.
   Verify: two `wscat` clients, messages cross.

R3. `record.py` / `replay.py`: dump a phone session to a file; replay it into the relay at real time. Lets Hub and GPU tracks work without a phone in hand.
   Verify: replay of a recorded walk drives the hub.

R4. `ngrok http 8000` (or cloudflared) script + README line. Verify: RunPod worker connects.

### Track I (iOS) — `RadAR/`

I1. ARKit session (`ARWorldTrackingConfiguration`), camera preview, "Start" button, responder id and relay URL in a settings sheet. `NSCameraUsageDescription`, ATS exception for `ws://`.
   Verify: app runs on phone, tracking state shows "normal".

I2. `StreamClient`: WebSocket to `/phone/{id}`. Per ARFrame at 10 fps: downscale + rotate camera image to 518×378 landscape JPEG (q≈0.6), send `pose` JSON (`camera.transform`, `intrinsics`, timestamp, frame id) then `frame` binary.
   Verify: relay logs ~10 fps from one phone.

I3. Shared-origin procedure: floor + wall marks, both phones tap Start there. No code beyond a one-line on-screen instruction.
   Verify: walk both phones along the same wall; hub trails overlap.

### Track H (Hub) — `hub/`

H1. `index.html` + `app.js`, Three.js from CDN. Scene, orbit controls, grid floor. WebSocket to `/hub`.
H2. Responder markers: capsule at ARKit position, 60 s trail line, per-responder colour. Camera-yaw arrow (no head cone yet).
H3. Two video panels, `<img>` updated from latest `frame`.
H4. Point cloud: per-responder `THREE.Points` with preallocated 3M-point buffer, append from `points` messages, wrap when full.
H5. Status strip: per-responder fps and last-seen age.
   Verify H1–H5 against `replay.py`, then against live phones.

### Core integration

C1. Both phones live → relay → hub markers + video. (I2 + R2 + H2/H3)
C2. Tunnel up, two GPU workers connected, clouds appear under trails at correct scale. (G4 + R4 + H4)
C3. Hallway loop test from spec "Verification": clouds overlap, trails close within ~1 m.

Commit Core before starting add-ons.

---

## ADD-ONS — each depends on Core only

### Module A — AirPods head cone
- iOS: `HeadTracker` using `CMHeadphoneMotionManager`; yaw/pitch at 10 Hz; capture yaw offset on Start (head assumed aligned with camera). New message `head {yaw, pitch}`. `NSMotionUsageDescription`.
- Relay: pass-through, add to state cache.
- Hub: replace H2 yaw arrow with a cone: direction = ARKit camera yaw + (head yaw − offset), pitch from head.
- Verify: stand still, turn head, cone turns; turn phone, cone follows phone.

### Module B — Coverage / blind spots
- Hub only. `coverage.js`: 0.5 m XZ grid; bin all points every 2 s; cell mapped if ≥ 50 pts; frontier = unmapped cell 4-adjacent to a mapped one; draw frontier cells as translucent red quads at floor height; mapped cells faint.
- Verify: walk one corridor, side rooms shaded red; enter one, its shading clears.

### Module C — UWB range check
- iOS: `MultipeerConnectivity` to find the peer phone and swap `NIDiscoveryToken`; `NISession` reports `distance`, optional `direction`. New message `uwb {peer, distance, direction?}` at ≤ 5 Hz. `NSNearbyInteractionUsageDescription`, `NSLocalNetworkUsageDescription`.
- Relay: compute ‖posA − posB‖ from latest poses; emit `uwb_check {uwb, arkit, mismatch: |Δ| > 0.5}`.
- Hub: status strip shows both numbers; red when mismatch.
- Verify: walk apart in line of sight, numbers track within 0.5 m; put a wall between, UWB drops or mismatch lights.

### Module D — GPS readout
- iOS: one `CLLocation` on Start → `gps {lat, lon, heading}`. `NSLocationWhenInUseUsageDescription`.
- Relay: cache. Hub: show in status strip.
- Verify: number appears.

### Module E (stretch) — Participant-anchor drift correction
- iOS: enable `isCollaborationEnabled`, exchange collaboration data over the Module C Multipeer session (or its own if C is skipped). On each `ARParticipantAnchor` update send `peer_pose {peer, transform}`.
- Relay: pair A's view of B with B's self pose by timestamp; keep last 50 pairs; rigid Kabsch → T_B→A; apply to B's poses and B's points before fan-out.
- Verify: start the two phones at different spots; after both see the same area, trails and clouds converge.

### Future (not planned): CV assistance, group voice, notes/pins, LiDAR layer.

---

## Suggested ownership after Core

| Person | Module |
|---|---|
| iOS builder | A, then C, then D |
| Hub builder | B |
| Python builder | C relay side, then E |

## Verification

Core: spec's hallway-loop test minus head cones and coverage.
Each module: its own Verify line above, run against live phones. Modules are merged independently; a broken module is reverted without touching Core.
