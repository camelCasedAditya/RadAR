import CoreMedia
import Foundation
import VideoToolbox

/// Hardware H.264 encoder. Emits Annex-B access units (start codes; SPS/PPS before each keyframe),
/// the format the hub's WebCodecs decoder takes without extra setup.
nonisolated final class VideoEncoder: @unchecked Sendable {
    /// (frame id, is keyframe, Annex-B bytes). Called on a VideoToolbox thread.
    var onFrame: (@Sendable (UInt32, Bool, Data) -> Void)?

    private var session: VTCompressionSession?
    private var size = (0, 0)
    private var forceKey = false
    private static let startCode: [UInt8] = [0, 0, 0, 1]

    // ponytail: encodes at the camera's native size (1920x1440); add a VTPixelTransferSession
    // downscale if two phones at ~3 Mbit/s each saturate the hotspot.
    func encode(_ pixelBuffer: CVPixelBuffer, time: TimeInterval, frame: UInt32) {
        let w = CVPixelBufferGetWidth(pixelBuffer), h = CVPixelBufferGetHeight(pixelBuffer)
        if session == nil || size != (w, h) { makeSession(w, h) }
        guard let session else { return }
        var props: CFDictionary?
        if forceKey {
            props = [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary
            forceKey = false
        }
        VTCompressionSessionEncodeFrame(session, imageBuffer: pixelBuffer,
                                        presentationTimeStamp: CMTime(seconds: time, preferredTimescale: 600),
                                        duration: .invalid, frameProperties: props, infoFlagsOut: nil) { [weak self] status, _, sample in
            guard status == noErr, let sample, let self else { return }
            self.emit(sample, frame: frame)
        }
    }

    /// Next frame becomes a keyframe (used to resync after dropping frames on a slow network).
    func requestKeyframe() { forceKey = true }

    func stop() {
        if let session { VTCompressionSessionInvalidate(session) }
        session = nil
    }

    private func makeSession(_ w: Int, _ h: Int) {
        stop()
        var s: VTCompressionSession?
        guard VTCompressionSessionCreate(allocator: nil, width: Int32(w), height: Int32(h), codecType: kCMVideoCodecType_H264,
                                         encoderSpecification: nil, imageBufferAttributes: nil, compressedDataAllocator: nil,
                                         outputCallback: nil, refcon: nil, compressionSessionOut: &s) == noErr, let s
        else { return }
        let set = { (k: CFString, v: CFTypeRef) in _ = VTSessionSetProperty(s, key: k, value: v) }
        set(kVTCompressionPropertyKey_RealTime, kCFBooleanTrue)
        set(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel)
        set(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse)       // no B-frames: lowest latency
        set(kVTCompressionPropertyKey_MaxKeyFrameInterval, 30 as CFNumber)
        set(kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, 1 as CFNumber)  // late hubs can start within 1 s
        set(kVTCompressionPropertyKey_ExpectedFrameRate, 30 as CFNumber)
        set(kVTCompressionPropertyKey_AverageBitRate, 3_000_000 as CFNumber)
        set(kVTCompressionPropertyKey_DataRateLimits, [4_000_000 / 8, 1] as CFArray)  // bytes per second cap
        VTCompressionSessionPrepareToEncodeFrames(s)
        session = s
        size = (w, h)
    }

    private func emit(_ sample: CMSampleBuffer, frame: UInt32) {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { return }
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[CFString: Any]]
        let isKey = !(attachments?.first?[kCMSampleAttachmentKey_NotSync] as? Bool ?? false)

        var out = Data()
        if isKey, let fmt = CMSampleBufferGetFormatDescription(sample) {
            for i in 0..<2 {   // SPS, PPS
                var ptr: UnsafePointer<UInt8>?
                var len = 0
                if CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                                                                      parameterSetSizeOut: &len, parameterSetCountOut: nil,
                                                                      nalUnitHeaderLengthOut: nil) == noErr, let ptr {
                    out.append(contentsOf: Self.startCode)
                    out.append(ptr, count: len)
                }
            }
        }
        // AVCC (4-byte big-endian lengths) -> Annex-B (start codes)
        var total = 0
        var base: UnsafeMutablePointer<CChar>?
        guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &total,
                                          dataPointerOut: &base) == noErr, let base else { return }
        base.withMemoryRebound(to: UInt8.self, capacity: total) { p in
            var off = 0
            while off + 4 <= total {
                let n = Int(p[off]) << 24 | Int(p[off + 1]) << 16 | Int(p[off + 2]) << 8 | Int(p[off + 3])
                off += 4
                guard off + n <= total else { break }
                out.append(contentsOf: Self.startCode)
                out.append(p + off, count: n)
                off += n
            }
        }
        onFrame?(frame, isKey, out)
    }
}
