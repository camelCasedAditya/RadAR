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

    /// Live video (kind 3): flags byte (bit 0 = keyframe) + Annex-B access unit.
    static func h264Frame(id: String, frame: UInt32, keyframe: Bool, annexB: Data) -> Data {
        var p = Data([keyframe ? 1 : 0])
        p.append(annexB)
        return binary(kind: 3, id: id, frame: frame, payload: p)
    }

    /// `pose` text message. T is camera-to-world, column-major, metres.
    static func pose(frame: UInt32, ts: Double, T: simd_float4x4, K: [Float], w: Int, h: Int) -> String {
        let c = [T.columns.0, T.columns.1, T.columns.2, T.columns.3]
        let flat = c.flatMap { [$0.x, $0.y, $0.z, $0.w] }.map(Double.init)
        let obj: [String: Any] = ["t": "pose", "f": Int(frame), "ts": ts, "T": flat,
                                  "K": K.map(Double.init), "w": w, "h": h]
        let data = try! JSONSerialization.data(withJSONObject: obj)
        return String(decoding: data, as: UTF8.self)
    }
}
