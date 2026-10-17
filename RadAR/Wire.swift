import Foundation
import simd

/// Message encoding. Must match server/relay/protocol.md and server/relay/wire.py.
nonisolated enum Wire {
    /// Binary frame: [kind, idlen, id..., frameId u32 LE, payload...]
    static func jpegFrame(id: String, frame: UInt32, jpeg: Data) -> Data {
        let idBytes = Data(id.utf8)
        var d = Data([1, UInt8(idBytes.count)])
        d.append(idBytes)
        withUnsafeBytes(of: frame.littleEndian) { d.append(contentsOf: $0) }
        d.append(jpeg)
        return d
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
