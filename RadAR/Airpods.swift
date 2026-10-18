import AVFoundation
import CoreMotion
import Foundation
import HealthKit

/// AirPods extras, all optional: each one simply stays quiet when the hardware or permission is missing.
/// Messages: see server/relay/protocol.md (`hr`, `head`, `talk`, binary kind 7).

/// Heart rate from AirPods Pro 3. On iPhone an app only gets it while it runs a HealthKit workout
/// session (iOS 26), so one runs for as long as the phone streams and is saved when it stops.
nonisolated final class HeartRate: NSObject, HKLiveWorkoutBuilderDelegate, HKWorkoutSessionDelegate, @unchecked Sendable {
    var onBPM: ((Double) -> Void)?
    private let store = HKHealthStore()
    private let queue: DispatchQueue                 // all state lives here
    private var session: HKWorkoutSession?
    private var builder: HKLiveWorkoutBuilder?
    private var wanted = false

    init(queue: DispatchQueue) { self.queue = queue }

    func start() {
        queue.async { [self] in
            wanted = true
            guard HKHealthStore.isHealthDataAvailable() else { return }
            store.requestAuthorization(toShare: [HKQuantityType.workoutType()], read: [HKQuantityType(.heartRate)]) { [weak self] ok, err in
                guard let self else { return }
                self.queue.async {
                    guard ok, self.wanted, self.session == nil else { if let err { print("[radar] health: \(err)") }; return }
                    self.begin()
                }
            }
        }
    }

    private func begin() {
        let cfg = HKWorkoutConfiguration()
        cfg.activityType = .other
        cfg.locationType = .indoor
        do {
            let s = try HKWorkoutSession(healthStore: store, configuration: cfg)
            let b = s.associatedWorkoutBuilder()
            b.dataSource = HKLiveWorkoutDataSource(healthStore: store, workoutConfiguration: cfg)
            s.delegate = self
            b.delegate = self
            session = s
            builder = b
            let now = Date()
            s.startActivity(with: now)
            b.beginCollection(withStart: now) { _, err in if let err { print("[radar] workout collection: \(err)") } }
        } catch {
            print("[radar] workout session: \(error)")
        }
    }

    func stop() {
        queue.async { [self] in
            wanted = false
            guard let s = session, let b = builder else { return }
            session = nil
            builder = nil
            s.end()
            b.endCollection(withEnd: Date()) { _, _ in b.finishWorkout { _, err in if let err { print("[radar] workout save: \(err)") } } }
        }
    }

    func workoutBuilder(_ b: HKLiveWorkoutBuilder, didCollectDataOf types: Set<HKSampleType>) {
        let hr = HKQuantityType(.heartRate)
        guard types.contains(hr), let q = b.statistics(for: hr)?.mostRecentQuantity() else { return }
        let bpm = q.doubleValue(for: .count().unitDivided(by: .minute()))
        queue.async { [weak self] in self?.onBPM?(bpm) }
    }

    func workoutBuilderDidCollectEvent(_ b: HKLiveWorkoutBuilder) {}
    func workoutSession(_ s: HKWorkoutSession, didChangeTo to: HKWorkoutSessionState, from: HKWorkoutSessionState, date: Date) {}
    func workoutSession(_ s: HKWorkoutSession, didFailWithError error: Error) { print("[radar] workout failed: \(error)") }
}

/// AirPods head direction, 10 Hz, as a yaw in the ARKit world frame. The AirPods' own reference frame is
/// arbitrary, so the first sample (and the first after a reconnect) is taken to look where the chest
/// camera looks. ponytail: headphone yaw drifts over minutes; a Recenter control is the upgrade path.
nonisolated final class HeadTracker: NSObject, CMHeadphoneMotionManagerDelegate, @unchecked Sendable {
    var phoneYaw: () -> Double = { 0 }               // called on `queue`
    var onHead: ((_ yaw: Double, _ pitch: Double) -> Void)?
    private(set) var connected = false
    private let manager = CMHeadphoneMotionManager()
    private let ops = OperationQueue()
    private var zero: (phone: Double, head: Double)?
    private var last: TimeInterval = 0

    init(queue: DispatchQueue) {
        ops.underlyingQueue = queue                  // samples arrive on Streamer's queue
        ops.maxConcurrentOperationCount = 1
    }

    func start() {
        guard manager.isDeviceMotionAvailable else { return }
        manager.delegate = self
        zero = nil
        manager.startDeviceMotionUpdates(to: ops) { [weak self] m, _ in
            guard let self, let m, m.timestamp - self.last >= 0.1 else { return }
            self.last = m.timestamp
            self.connected = true
            // ponytail: CMAttitude yaw is + turning left like ARKit's yaw about +Y, pitch + looking up;
            // flip a sign here if the hub cone mirrors the head on real AirPods.
            let z = self.zero ?? (self.phoneYaw(), m.attitude.yaw)
            self.zero = z
            self.onHead?(atan2(sin(z.phone + m.attitude.yaw - z.head), cos(z.phone + m.attitude.yaw - z.head)), m.attitude.pitch)
        }
    }

    func stop() {
        manager.stopDeviceMotionUpdates()
        ops.addOperation { [weak self] in self?.connected = false }
    }

    func headphoneMotionManagerDidConnect(_ manager: CMHeadphoneMotionManager) {}
    func headphoneMotionManagerDidDisconnect(_ manager: CMHeadphoneMotionManager) {
        ops.addOperation { [weak self] in self?.connected = false; self?.zero = nil }
    }
}

/// Push-to-talk party line: mic -> 16 kHz mono int16 LE in 40 ms chunks while talking; everyone else's
/// chunks play through one player node per sender. AirPods mic and earbuds when connected (Bluetooth
/// HFP), otherwise the phone mic and loudspeaker. Voice processing cancels the echo of the call itself.
nonisolated final class VoiceCall: @unchecked Sendable {
    var onChunk: ((Data) -> Void)?                   // called on the audio thread
    private let engine = AVAudioEngine()
    private let wire = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private let playFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    private var converter: AVAudioConverter?
    private var players: [String: AVAudioPlayerNode] = [:]   // only touched on Streamer's queue
    private let lock = NSLock()                      // guards talking + buffered
    private var talking = false
    private var buffered = Data()

    func start() {
        AVAudioApplication.requestRecordPermission { [weak self] granted in
            guard let self else { return }
            do { try self.run(mic: granted) } catch { print("[radar] voice: \(error)") }
        }
    }

    private func run(mic: Bool) throws {
        let s = AVAudioSession.sharedInstance()
        try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP, .defaultToSpeaker])
        try s.setActive(true)
        if mic {
            let input = engine.inputNode
            try input.setVoiceProcessingEnabled(true)
            let format = input.outputFormat(forBus: 0)
            converter = AVAudioConverter(from: format, to: wire)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buf, _ in self?.captured(buf) }
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        for p in players.values { engine.detach(p) }
        players = [:]
        setTalking(false)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func setTalking(_ on: Bool) {
        lock.lock(); talking = on; buffered = Data(); lock.unlock()
    }

    private func captured(_ buf: AVAudioPCMBuffer) {
        lock.lock(); let on = talking; lock.unlock()
        guard on, let converter else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * 16000 / buf.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: cap) else { return }
        var fed = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buf
        }
        guard err == nil, let samples = out.int16ChannelData else { return }
        lock.lock()
        buffered.append(Data(bytes: samples[0], count: Int(out.frameLength) * 2))
        var chunks: [Data] = []
        while buffered.count >= 1280 {               // 640 samples = 40 ms
            chunks.append(Data(buffered.prefix(1280)))
            buffered.removeFirst(1280)
        }
        lock.unlock()
        chunks.forEach { onChunk?($0) }
    }

    /// One received chunk from `id` (int16 LE PCM). Call on Streamer's queue.
    func play(_ id: String, _ pcm: Data) {
        let n = pcm.count / 2
        guard engine.isRunning, n > 0, let buf = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(n)),
              let dst = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        pcm.withUnsafeBytes { raw in
            for i in 0..<n { dst[i] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768 }
        }
        let player = players[id] ?? {
            let p = AVAudioPlayerNode()
            engine.attach(p)
            engine.connect(p, to: engine.mainMixerNode, format: playFormat)
            p.play()
            players[id] = p
            return p
        }()
        player.scheduleBuffer(buf)
    }
}
