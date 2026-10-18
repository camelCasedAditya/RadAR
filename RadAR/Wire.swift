import ARKit
import Foundation
import simd

/// Message encoding. Must match server/relay/protocol.md and server/relay/wire.py.
nonisolated enum Wire {
    /// Binary frame: [kind, idlen, id..., frameId u32 LE, payload...]
    static func binary(kind: UInt8, id: String, frame: UInt32, payload: Data) -> Data {
        let idBytes = Data(id.utf8)
        var d = Data([kind, UInt8(idBytes.count)])
        d.append(idBytes)
        withUnsafeBytes(of: frame.littleEndian) { d.append(contentsOf: $0) }
        d.append(payload)
        return d
    }

    /// Map frame for the GPU (kind 1).
    static func jpegFrame(id: String, frame: UInt32, jpeg: Data) -> Data {
        binary(kind: 1, id: id, frame: frame, payload: jpeg)
    }

    /// LiDAR map frame (kind 4): w u16, h u16, depth f32[w*h] metres, conf u8[w*h] (ARKit 0/1/2), then the JPEG.
    /// Sent instead of kind 1 on phones with scene depth. nil if ARKit gave no confidence map.
    static func depthFrame(id: String, frame: UInt32, depth: ARDepthData, jpeg: Data) -> Data? {
        let dm = depth.depthMap
        guard let cm = depth.confidenceMap,
              CVPixelBufferGetPixelFormatType(dm) == kCVPixelFormatType_DepthFloat32 else { return nil }
        let w = CVPixelBufferGetWidth(dm), h = CVPixelBufferGetHeight(dm)
        CVPixelBufferLockBaseAddress(dm, .readOnly); CVPixelBufferLockBaseAddress(cm, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(dm, .readOnly); CVPixelBufferUnlockBaseAddress(cm, .readOnly) }
        guard let db = CVPixelBufferGetBaseAddress(dm), let cb = CVPixelBufferGetBaseAddress(cm) else { return nil }
        var p = Data(capacity: 4 + w * h * 5 + jpeg.count)
        withUnsafeBytes(of: UInt16(w).littleEndian) { p.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt16(h).littleEndian) { p.append(contentsOf: $0) }
        let dbr = CVPixelBufferGetBytesPerRow(dm), cbr = CVPixelBufferGetBytesPerRow(cm)
        let d8 = db.assumingMemoryBound(to: UInt8.self), c8 = cb.assumingMemoryBound(to: UInt8.self)
        for y in 0..<h { p.append(d8 + y * dbr, count: w * 4) }   // rows may be padded
        for y in 0..<h { p.append(c8 + y * cbr, count: w) }
        p.append(jpeg)
        return binary(kind: 4, id: id, frame: frame, payload: p)
    }

    /// Live video (kind 3): flags byte (bit 0 = keyframe) + Annex-B access unit.
    static func h264Frame(id: String, frame: UInt32, keyframe: Bool, annexB: Data) -> Data {
        var p = Data([keyframe ? 1 : 0])
        p.append(annexB)
        return binary(kind: 3, id: id, frame: frame, payload: p)
    }

    /// Call audio (kind 7): mono int16 LE PCM, 16 kHz, 40 ms per chunk; frame = sequence number.
    static func audioFrame(id: String, seq: UInt32, pcm: Data) -> Data {
        binary(kind: 7, id: id, frame: seq, payload: pcm)
    }

    /// Small text messages: `hr`, `head`, `talk`.
    static func json(_ obj: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: obj), as: UTF8.self)
    }

    /// `pose` text message. T is camera-to-world, column-major, metres.
    /// `fp`: ARKit feature points as [u, v, z] in the map image (pixels) with metric depth, sent with map frames only.
    static func pose(frame: UInt32, ts: Double, T: simd_float4x4, K: [Float], w: Int, h: Int, fp: [[Float]] = []) -> String {
        let c = [T.columns.0, T.columns.1, T.columns.2, T.columns.3]
        let flat = c.flatMap { [$0.x, $0.y, $0.z, $0.w] }.map(Double.init)
        var obj: [String: Any] = ["t": "pose", "f": Int(frame), "ts": ts, "T": flat,
                                  "K": K.map(Double.init), "w": w, "h": h]
        if !fp.isEmpty { obj["fp"] = fp.map { $0.map { Double(($0 * 100).rounded() / 100) } } }
        let data = try! JSONSerialization.data(withJSONObject: obj)
        return String(decoding: data, as: UTF8.self)
    }
}
