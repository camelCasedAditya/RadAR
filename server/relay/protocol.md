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
| `pose` | `f` (frame id, int), `ts` (s, float), `T` (16 floats, column-major 4×4 camera-to-world, metres, ARKit axes: +Y up), `K` ([fx, fy, cx, cy] for the 518-wide map image), `w`, `h` | phone | every camera frame (30 Hz). Map JPEGs and video frames reuse the same `f` |
| `head` | `yaw`, `pitch` (radians, relative to Start) | phone | Module A |
| `uwb` | `peer`, `distance` (m), `direction` ([x,y,z] or null) | phone | Module C |
| `gps` | `lat`, `lon`, `heading` | phone | Module D |
| `peer_pose` | `peer`, `T` | phone | Module E |
| `uwb_check` | `peer`, `uwb`, `arkit`, `mismatch` (bool) | relay | Module C |
| `maplag` | `lag` (s from map frame capture to its points), `queued` (frames waiting) | relay | per map result |
| `gpu` | `fps`, `mem_gb`, `streams`, `gpu` (worker status, ~every 2 s) | relay | |
| `state` | `responders`: `{id: {pose, head, uwb, gps, ...}}` latest text message of each type | relay | sent to hub on connect |

## Binary messages

```
byte 0      kind     1 = jpeg, 2 = points, 3 = h264
byte 1      idlen
bytes 2..   id       (idlen bytes, utf-8)
next 4      f        uint32 little-endian frame id
rest        payload
```

- kind 1 payload (**map frame**): JPEG bytes, a steady 6 fps while the phone moves (lingbot-map is a streaming video model and needs overlapping frames), 1 fps when still. Camera sensor orientation (landscape, unrotated, matching ARKit's camera transform), width 518, height a multiple of 14 (392 for iPhone's 4:3 camera). Phone must set `id` itself.
- kind 2 payload: `n` uint32 LE, then `n*3` float32 LE xyz (metres, ARKit frame), then `n*3` uint8 rgb. Sent by the relay after aligning the worker's depth.
- kind 3 payload (**live video**, 30 fps): `flags` u8 (bit 0 = keyframe), then one H.264 access unit in Annex-B form (start codes). Keyframes carry SPS and PPS first. Same sensor orientation as kind 1. Relay forwards it untouched and replays the latest keyframe group to hubs that connect late.

Helpers: `server/relay/wire.py` (`pack_bin`, `unpack_bin`, `pack_points`, `unpack_points`).
