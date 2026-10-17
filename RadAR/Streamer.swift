import ARKit
import CoreImage
import Foundation

/// Runs ARKit and streams to the relay: pose + H.264 video at 30 fps, and a 518-wide JPEG "map frame"
/// for the GPU only when the phone has moved enough to add geometry. All state lives on `queue`.
nonisolated final class Streamer: NSObject, ARSessionDelegate, @unchecked Sendable {
    let session = ARSession()
    var onStatus: (@Sendable (String) -> Void)?

    private let queue = DispatchQueue(label: "radar.stream")
    private let ci = CIContext()
    private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!
    private var ws: URLSessionWebSocketTask?
    private var base: URL?
    private var rid = "A"
    private var frameId: UInt32 = 0
    private let encoder = VideoEncoder()
    private var lastFrame: TimeInterval = 0
    private var lastMap: (time: TimeInterval, T: simd_float4x4)?
    private var pending = 0                  // socket sends not yet completed
    private var dropping = false             // network fell behind: skip video until the next keyframe
    private var videoTimes: [TimeInterval] = []
    private var mapTimes: [TimeInterval] = []
    private var tracking = "starting"
    private var link = "connecting"
    private var lastError = ""

    static let width = 518  // lingbot-map native width; height is rounded to a multiple of 14
    // ponytail: fixed map-frame gates; tune per venue (lower = denser map, more GPU lag).
    static let mapMove: Float = 0.30         // metres moved since last map frame
    static let mapTurn: Float = 15 * .pi / 180
    static let mapFloor: TimeInterval = 1    // at least one map frame per second
    static let mapMinGap: TimeInterval = 1.0 / 3   // at most 3 map frames per second per phone (A40: ~9/s total)

    func start(url: URL, id: String) {
        queue.async { [self] in
            base = url; rid = id; frameId = 0; lastMap = nil
            encoder.onFrame = { [weak self] f, key, data in
                self?.queue.async { self?.sendVideo(f, key: key, data: data) }
            }
            connect()
        }
        let cfg = ARWorldTrackingConfiguration()
        cfg.worldAlignment = .gravity
        session.delegateQueue = queue
        session.delegate = self
        session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
    }

    func stop() {
        session.pause()
        queue.async { [self] in
            base = nil
            encoder.stop()
            ws?.cancel(with: .goingAway, reason: nil)
            ws = nil
        }
    }

    // MARK: socket

    private func connect() {
        guard let base else { return }
        let task = URLSession.shared.webSocketTask(with: base.appendingPathComponent("phone/\(rid)"))
        ws = task
        pending = 0
        dropping = true                      // new socket: start the video on a keyframe
        encoder.requestKeyframe()
        link = "connecting"
        task.resume()
        task.send(.string(#"{"t":"hello","name":"\#(rid)"}"#)) { [weak self] err in
            self?.queue.async { if err == nil { self?.link = "live" } }
        }
        listen(task)
    }

    /// The relay never sends to phones; a receive only completes when the socket dies.
    private func listen(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard self.ws === task else { return }
                if case .failure(let e) = result { self.reconnect(because: "receive: \(e.localizedDescription)") } else { self.listen(task) }
            }
        }
    }

    private func reconnect(because why: String) {
        lastError = why
        print("[radar] socket dropped (\(why)); close code \(ws?.closeCode.rawValue ?? -1)")
        ws?.cancel()
        ws = nil
        link = "reconnecting"
        queue.asyncAfter(deadline: .now() + 1) { [weak self] in
            if self?.ws == nil { self?.connect() }
        }
    }

    // MARK: ARSessionDelegate (called on `queue`)

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        tracking = Self.describe(frame.camera.trackingState)
        guard let ws, frame.timestamp - lastFrame >= 1.0 / 30 - 0.004 else { return }   // ARKit may run at 60
        lastFrame = frame.timestamp

        let res = frame.camera.imageResolution
        let sx = CGFloat(Self.width) / res.width
        let h = Int((res.height * sx / 14).rounded()) * 14
        let sy = CGFloat(h) / res.height
        let k = frame.camera.intrinsics
        let K: [Float] = [k[0][0] * Float(sx), k[1][1] * Float(sy), k[2][0] * Float(sx), k[2][1] * Float(sy)]
        frameId &+= 1
        let f = frameId
        let T = frame.camera.transform
        send(.string(Wire.pose(frame: f, ts: frame.timestamp, T: T, K: K, w: Self.width, h: h)), on: ws)

        if pending > 30 { dropping = true }          // ~1 s of sends backed up
        if !dropping || pending < 5 {
            encoder.encode(frame.capturedImage, time: frame.timestamp, frame: f)
        }

        if isMapFrame(T, at: frame.timestamp), pending < 30 {
            lastMap = (frame.timestamp, T)
            let img = CIImage(cvPixelBuffer: frame.capturedImage).transformed(by: CGAffineTransform(scaleX: sx, y: sy))
            if let jpeg = ci.jpegRepresentation(of: img, colorSpace: sRGB,
                                                options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]) {
                send(.data(Wire.jpegFrame(id: rid, frame: f, jpeg: jpeg)), on: ws)
                mapTimes.append(frame.timestamp)
            }
        }

        videoTimes.removeAll { frame.timestamp - $0 > 2 }
        mapTimes.removeAll { frame.timestamp - $0 > 2 }
        onStatus?("\(link) · video \(videoTimes.count / 2) fps · map \(mapTimes.count / 2) fps · tracking \(tracking)"
                  + (lastError.isEmpty ? "" : "\nlast drop: \(lastError)"))
    }

    /// Moved or turned enough since the last map frame to add new geometry (or the 1 s floor passed).
    private func isMapFrame(_ T: simd_float4x4, at t: TimeInterval) -> Bool {
        guard let last = lastMap else { return true }
        if t - last.time < Self.mapMinGap { return false }
        if t - last.time >= Self.mapFloor { return true }
        let moved = simd_distance(simd_make_float3(T.columns.3), simd_make_float3(last.T.columns.3))
        let r = simd_mul(simd_transpose(simd_float3x3(simd_make_float3(last.T.columns.0), simd_make_float3(last.T.columns.1),
                                                      simd_make_float3(last.T.columns.2))),
                         simd_float3x3(simd_make_float3(T.columns.0), simd_make_float3(T.columns.1), simd_make_float3(T.columns.2)))
        let turned = acos(max(-1, min(1, (r[0][0] + r[1][1] + r[2][2] - 1) / 2)))
        return moved >= Self.mapMove || turned >= Self.mapTurn
    }

    private func sendVideo(_ f: UInt32, key: Bool, data: Data) {
        guard let ws else { return }
        if dropping {
            guard key else { encoder.requestKeyframe(); return }   // resume only on a keyframe
            dropping = false
        }
        send(.data(Wire.h264Frame(id: rid, frame: f, keyframe: key, annexB: data)), on: ws)
        videoTimes.append(lastFrame)
    }

    private func send(_ m: URLSessionWebSocketTask.Message, on ws: URLSessionWebSocketTask) {
        pending += 1
        ws.send(m) { [weak self] err in
            self?.queue.async {
                guard let self else { return }
                self.pending -= 1
                if let err, self.ws === ws { self.reconnect(because: "send: \(err.localizedDescription)") }
            }
        }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        onStatus?("AR error: \(error.localizedDescription)")
    }

    private static func describe(_ s: ARCamera.TrackingState) -> String {
        switch s {
        case .normal: return "normal"
        case .notAvailable: return "unavailable"
        case .limited(let r):
            switch r {
            case .initializing: return "initializing"
            case .excessiveMotion: return "too fast"
            case .insufficientFeatures: return "low detail"
            case .relocalizing: return "relocalizing"
            @unknown default: return "limited"
            }
        }
    }
}
