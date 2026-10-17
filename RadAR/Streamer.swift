import ARKit
import CoreImage
import Foundation

/// Runs ARKit and streams pose + JPEG at 10 fps to the relay. All state lives on `queue`.
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
    private var lastSent: TimeInterval = 0
    private var inFlight = 0
    private var sentTimes: [TimeInterval] = []
    private var tracking = "starting"
    private var link = "connecting"

    static let width = 518  // lingbot-map native width; height is rounded to a multiple of 14

    func start(url: URL, id: String) {
        queue.async { [self] in
            base = url; rid = id; frameId = 0
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
            ws?.cancel(with: .goingAway, reason: nil)
            ws = nil
        }
    }

    // MARK: socket

    private func connect() {
        guard let base else { return }
        let task = URLSession.shared.webSocketTask(with: base.appendingPathComponent("phone/\(rid)"))
        ws = task
        inFlight = 0
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
                if case .failure = result { self.reconnect() } else { self.listen(task) }
            }
        }
    }

    private func reconnect() {
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
        guard let ws, frame.timestamp - lastSent >= 0.095, inFlight < 3 else { return }
        lastSent = frame.timestamp

        let res = frame.camera.imageResolution
        let sx = CGFloat(Self.width) / res.width
        let h = Int((res.height * sx / 14).rounded()) * 14
        let sy = CGFloat(h) / res.height
        let img = CIImage(cvPixelBuffer: frame.capturedImage).transformed(by: CGAffineTransform(scaleX: sx, y: sy))
        guard let jpeg = ci.jpegRepresentation(of: img, colorSpace: sRGB,
                                               options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6])
        else { return }

        let k = frame.camera.intrinsics
        let K: [Float] = [k[0][0] * Float(sx), k[1][1] * Float(sy), k[2][0] * Float(sx), k[2][1] * Float(sy)]
        frameId &+= 1
        let f = frameId
        inFlight += 1
        ws.send(.string(Wire.pose(frame: f, ts: frame.timestamp, T: frame.camera.transform, K: K, w: Self.width, h: h))) { _ in }
        ws.send(.data(Wire.jpegFrame(id: rid, frame: f, jpeg: jpeg))) { [weak self] err in
            self?.queue.async {
                guard let self else { return }
                self.inFlight -= 1
                if err != nil, self.ws === ws { self.reconnect() }
            }
        }

        sentTimes.append(frame.timestamp)
        sentTimes.removeAll { frame.timestamp - $0 > 2 }
        onStatus?("\(link) · \(sentTimes.count / 2) fps · tracking \(tracking)")
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
