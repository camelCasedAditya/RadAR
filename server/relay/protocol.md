# RadAR wire protocol

One relay, three kinds of client. All over WebSocket.

| Client | Endpoint | Sends | Receives |
|---|---|---|---|
| Phone | `/phone/{id}` | text + jpeg binary | nothing |
| GPU worker | `/gpu/{id}` | points binary | that phone's text + jpeg |
| Hub | `/hub` | nothing | everything, plus `state` on connect |

`id` is a short responder id, e.g. `A`, `B`.

## Text messages (JSON)

Every text message has `t` (type) and `id` (responder). Relay stamps `id` from the endpoint; phones may omit it.

| `t` | Fields | Source | Notes |
|---|---|---|---|
| `hello` | `name` | phone | once on connect |
| `pose` | `f` (frame id, int), `ts` (s, float), `T` (16 floats, column-major 4×4 camera-to-world, metres, ARKit axes: +Y up), `K` ([fx, fy, cx, cy] for the sent image size), `w`, `h` | phone | 10 Hz, immediately followed by the jpeg binary with the same `f` |
| `head` | `yaw`, `pitch` (radians, relative to Start) | phone | Module A |
| `uwb` | `peer`, `distance` (m), `direction` ([x,y,z] or null) | phone | Module C |
| `gps` | `lat`, `lon`, `heading` | phone | Module D |
| `peer_pose` | `peer`, `T` | phone | Module E |
| `uwb_check` | `peer`, `uwb`, `arkit`, `mismatch` (bool) | relay | Module C |
| `state` | `responders`: `{id: {pose, head, uwb, gps, ...}}` latest text message of each type | relay | sent to hub on connect |

## Binary messages

```
byte 0      kind     1 = jpeg, 2 = points
byte 1      idlen
bytes 2..   id       (idlen bytes, utf-8)
next 4      f        uint32 little-endian frame id
rest        payload
```

- kind 1 payload: JPEG bytes. Camera sensor orientation (landscape, unrotated, matching ARKit's camera transform), width 518, height a multiple of 14 (392 for iPhone's 4:3 camera). Phone must set `id` itself.
- kind 2 payload: `n` uint32 LE, then `n*3` float32 LE xyz (metres, ARKit frame), then `n*3` uint8 rgb.

Helpers: `server/relay/wire.py` (`pack_bin`, `unpack_bin`, `pack_points`, `unpack_points`).
