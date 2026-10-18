# RadAR wire protocol

One relay, two kinds of client, plus an outbound link to the GPU worker. All over WebSocket.

| Client | Endpoint | Sends | Receives |
|---|---|---|---|
| Phone | `/phone/{id}` | text + jpeg binary | nothing |
| GPU worker | — | relay connects out to it (`--gpu`), see `gpu_link.py` | |
| Hub | `/hub` | nothing | everything, plus `state` on connect |

`id` is a short responder id, e.g. `A`, `B`.

## Text messages (JSON)

Every text message has `t` (type) and `id` (responder). Relay stamps `id` from the endpoint; phones may omit it.

| `t` | Fields | Source | Notes |
|---|---|---|---|
| `hello` | `name` | phone | once on connect |
| `pose` | `f` (frame id, int), `ts` (s, float), `T` (16 floats, column-major 4×4 camera-to-world, metres, ARKit axes: +Y up), `K` ([fx, fy, cx, cy] for the 518-wide map image), `w`, `h`, `fp` (map frames only: ARKit feature points as [u, v, z] pixels in the map image + metric depth, ≤300; the relay reads lingbot depth at these pixels for a per-frame metric scale) | phone | every camera frame (30 Hz). Map JPEGs and video frames reuse the same `f` |
| `head` | `yaw`, `pitch` (radians, relative to Start) | phone | Module A |
| `uwb` | `peer`, `distance` (m), `direction` ([x,y,z] or null) | phone | Module C |
| `gps` | `lat`, `lon`, `heading` | phone | Module D |
| `peer_pose` | `peer`, `T` | phone | Module E |
| `uwb_check` | `peer`, `uwb`, `arkit`, `mismatch` (bool) | relay | Module C |
| `maplag` | `lag` (s from map frame capture to its points), `queued` (frames waiting) | relay | per map result |
| `gpu` | `fps`, `mem_gb`, `streams`, `gpu` (worker status, ~every 2 s) | relay | |
| `state` | `responders`: `{id: {pose, head, uwb, gps, ...}}` latest text message of each type | relay | sent to hub on connect |
| `recording` | `name` (map name or null), `since` (epoch s) | relay | on hub connect and whenever recording starts/stops |

## Binary messages

```
byte 0      kind     1 = jpeg, 2 = points, 3 = h264, 4 = depth, 5 = patch
byte 1      idlen
bytes 2..   id       (idlen bytes, utf-8)
next 4      f        uint32 little-endian frame id
rest        payload
```

- kind 1 payload (**map frame**): JPEG bytes, a steady 4 fps while the phone moves (lingbot-map is a streaming video model and needs overlapping frames), 1 fps when still. Camera sensor orientation (landscape, unrotated, matching ARKit's camera transform), width 518, height a multiple of 14 (392 for iPhone's 4:3 camera). Phone must set `id` itself.
- kind 2 payload: `n` uint32 LE, then `n*3` float32 LE xyz (metres, ARKit frame), then `n*3` uint8 rgb. Sent by the relay after aligning the worker's depth.
- kind 3 payload (**live video**, 30 fps): `flags` u8 (bit 0 = keyframe), then one H.264 access unit in Annex-B form (start codes). Keyframes carry SPS and PPS first. Same sensor orientation as kind 1. Relay forwards it untouched and replays the latest keyframe group to hubs that connect late.
- kind 4 payload (**LiDAR map frame**, iPhone Pro only, sent *instead of* kind 1): `w` u16 LE, `h` u16 LE (ARKit scene depth grid, 256×192), `w*h` float32 LE depth in metres, `w*h` uint8 ARKit confidence (0 low, 1 medium, 2 high), then the map JPEG as in kind 1. Relay keeps high-confidence pixels, colours them from the JPEG, and places them with the `pose` of the same `f`; no GPU involved.
- kind 5 payload (**LiDAR patch**, relay → hub): `hlen` u32 LE, JSON header `{T, K, w, h, dw, dh, s}` (the frame's ARKit pose and lens for the w×h image; a dw×dh grid sampled every `s` pixels), `dw*dh` float16 LE depth in metres along the camera axis (0 = hole), then the map JPEG. The hub drapes the JPEG over a mesh built from the grid, dropping triangles across depth jumps. Sent only on keyframes (camera moved 25 cm or turned 15°). LiDAR phones get patches instead of kind 2 points.

## Session files (saved maps)

While recording (between `POST /maps/start {name}` and `POST /maps/stop`), everything the relay sends hubs (poses, H.264 video, map JPEGs, points, LiDAR patches, status) is appended to `server/maps/<name>.radar`: the bytes `RADAR2\n`, then records `u32 len | u32 t_ms | u8 type | body` (type 0 = text JSON, 1 = binary message; `len` covers everything after it). A file opens with a `state` message and each phone's latest video keyframe group, so a replay starts decodable like a late-joining hub. `GET /maps` lists files (`{name, bytes, patches, mapped, seconds, where}`) plus the recording state; `GET /maps/<name>` returns a file; `POST /maps/start` (JSON `{name}`) begins a recording, clears the relay's voxel/keyframe state and tells hubs `recording {name, since}` (also sent to a hub on connect, with `name: null` when idle; never recorded); `POST /maps/stop` ends it and returns `{saved: meta}`. The hub's Past maps panel loads a map (only kinds 2 and 5, applied at once), replays a session (every record on its original timeline: video, markers, map growth), downloads it, or deletes it (`DELETE /maps/<name>`). With `RADAR_S3_BUCKET` set, `store.py` streams the file to `s3://<bucket>/sessions/<name>` as it is written and adds `<name>.json` `{patches, mapped, seconds, bytes}` at the end; `GET /maps` merges both places (`where`: local, s3, both) and `GET /maps/<name>` redirects to a presigned S3 URL when the file is not local.

Helpers: `server/relay/wire.py` (`pack_bin`, `unpack_bin`, `pack_points`, `unpack_points`).
