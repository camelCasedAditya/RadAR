// Live H.264 video per responder (binary kind 3), decoded with the browser's WebCodecs.
// Payload: flags u8 (bit 0 = keyframe), then one Annex-B access unit; keyframes carry SPS/PPS.

export const videoSupported = typeof VideoDecoder !== "undefined";

/** avc1.PPCCLL codec string from the SPS NAL in an Annex-B keyframe. */
export function codecFromAnnexB(au) {
  for (let i = 0; i + 4 < au.length; i++) {
    const sc3 = au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1;
    if (sc3 && (au[i + 3] & 0x1f) === 7) {
      const hex = (b) => b.toString(16).padStart(2, "0");
      return `avc1.${hex(au[i + 4])}${hex(au[i + 5])}${hex(au[i + 6])}`;
    }
  }
  return null;
}

/** Quarter turns (0..3, clockwise) that make world-up point up on screen, from an ARKit pose. */
export function quarterTurns(T) {
  if (!T) return 0;
  // world up expressed in camera axes: x = T[1], y = T[5]; image up is camera +y
  const theta = Math.atan2(T[1], T[5]);
  return ((-Math.round(theta / (Math.PI / 2))) % 4 + 4) % 4;
}

export function onVideo(r, f, payload) {
  const key = (payload[0] & 1) === 1;
  const au = payload.subarray(1);
  if (!r.decoder || r.decoder.state === "closed") {
    if (!key) return;                               // can only start on a keyframe
    const codec = codecFromAnnexB(au);
    if (!codec) return;
    const canvas = r.canvas, ctx = canvas.getContext("2d");
    r.decoder = new VideoDecoder({
      output: (frame) => {
        const q = quarterTurns(r.pose?.T);
        const fw = frame.displayWidth, fh = frame.displayHeight;
        const [cw, ch] = q % 2 ? [fh, fw] : [fw, fh];
        if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
        ctx.setTransform(1, 0, 0, 1, cw / 2, ch / 2);
        ctx.rotate(q * Math.PI / 2);
        ctx.drawImage(frame, -fw / 2, -fh / 2);
        frame.close();
        r.videoTimes.push(performance.now());
      },
      error: (e) => { console.warn(`video ${r.id}:`, e.message); r.decoder = null; },
    });
    r.decoder.configure({ codec, optimizeForLatency: true });   // no description => Annex-B input
    r.feed.classList.add("has-video");
  }
  // fell behind (tab hidden, slow machine): drop deltas until the next keyframe resyncs
  if (r.decoder.decodeQueueSize > 8 && !key) return;
  r.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: f * 33333, data: au }));
}
