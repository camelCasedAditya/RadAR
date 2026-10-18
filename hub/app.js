// RadAR hub. Protocol: server/relay/protocol.md
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { onVideo, videoSupported } from "./video.js";

const COLORS = ["#3fa7ff", "#ff9f1c", "#2ec4b6", "#e84855"];
let tintOn = false;                 // see the Tint button below
const MAX_POINTS = 6_000_000;   // per responder (90 MB); ring buffer wraps when full
const TRAIL_SECONDS = 60;

// ---------- scene ----------
const view = document.getElementById("view");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color("#0b0e12");
const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(6, 14, 18);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(6, 0, 3);
controls.enableDamping = true;
const grid = new THREE.GridHelper(60, 60, 0x263140, 0x182029);
scene.add(grid);
scene.add(new THREE.AxesHelper(1));

function resize() {
  const w = view.clientWidth, h = view.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(view);

// soft round sprite for points: squares read as "blocky", discs read as a surface
const dotTexture = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 32;
  const g = c.getContext("2d"), grd = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  grd.addColorStop(0, "rgba(255,255,255,1)"); grd.addColorStop(0.8, "rgba(255,255,255,1)"); grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd; g.fillRect(0, 0, 32, 32);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.NoColorSpace; return t;
})();

// ---------- responders ----------
const responders = new Map();   // id -> R
const side = document.getElementById("side");

function responder(id) {
  let r = responders.get(id);
  if (r) return r;
  const color = new THREE.Color(COLORS[responders.size % COLORS.length]);

  const marker = new THREE.Group();
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.2, 1.0, 4, 12),
                              new THREE.MeshBasicMaterial({ color }));
  body.position.y = -0.8;  // phone is held ~1.4 m up; capsule stands on the floor below it
  marker.add(body);
  const arrow = new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(), 1.2, color, 0.3, 0.2);
  marker.add(arrow);
  marker.visible = false;
  scene.add(marker);

  const trailGeo = new THREE.BufferGeometry();
  trailGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(TRAIL_SECONDS * 12 * 3), 3));
  trailGeo.setDrawRange(0, 0);
  const trail = new THREE.Line(trailGeo, new THREE.LineBasicMaterial({ color }));
  scene.add(trail);

  const ptsGeo = new THREE.BufferGeometry();
  ptsGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(MAX_POINTS * 3), 3));
  ptsGeo.setAttribute("color", new THREE.BufferAttribute(new Uint8Array(MAX_POINTS * 3), 3, true));
  ptsGeo.setDrawRange(0, 0);
  const cloud = new THREE.Points(ptsGeo, new THREE.PointsMaterial({
    size: 0.011, vertexColors: !tintOn, color: tintOn ? color : 0xffffff,   // = relay voxel size, so surfaces close up
    map: dotTexture, alphaTest: 0.5, transparent: false,   // round dots instead of squares
  }));
  cloud.frustumCulled = false;
  scene.add(cloud);

  const el = document.createElement("section");
  el.className = "feed";
  el.innerHTML = `<header><span class="dot" style="background:${COLORS[responders.size % COLORS.length]}"></span>
    <b>Responder ${id}</b><span class="meta"></span></header><img alt="Live camera from responder ${id}">
    <canvas aria-label="Live video from responder ${id}"></canvas>`;
  side.appendChild(el);

  r = { id, color, marker, arrow, trail, trailPts: [], cloud, ptsN: 0, ptsHead: 0,
        img: el.querySelector("img"), meta: el.querySelector(".meta"), imgURL: null,
        canvas: el.querySelector("canvas"), feed: el, decoder: null, videoTimes: [], mapLag: null,
        pose: null, lastSeen: 0, poseTimes: [], ptsFrames: 0 };
  responders.set(id, r);
  return r;
}

const _m = new THREE.Matrix4(), _p = new THREE.Vector3(), _q = new THREE.Quaternion(), _s = new THREE.Vector3();

function onPose(d) {
  const r = responder(d.id);
  _m.fromArray(d.T);                       // column-major, same as THREE
  _m.decompose(_p, _q, _s);
  r.marker.position.copy(_p);
  // yaw-only marker: drop phone roll/pitch so the capsule stays upright
  const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(_q);
  r.marker.rotation.set(0, Math.atan2(-fwd.x, -fwd.z), 0);
  r.marker.visible = true;
  r.pose = d;
  const now = performance.now();
  r.lastSeen = now;
  r.poseTimes.push(now);
  while (r.poseTimes.length && now - r.poseTimes[0] > 2000) r.poseTimes.shift();

  r.trailPts.push({ t: now, x: _p.x, y: 0.02, z: _p.z });
  while (r.trailPts.length && now - r.trailPts[0].t > TRAIL_SECONDS * 1000) r.trailPts.shift();
  const arr = r.trail.geometry.attributes.position;
  const n = Math.min(r.trailPts.length, arr.count);
  const off = r.trailPts.length - n;
  for (let i = 0; i < n; i++) { const q = r.trailPts[off + i]; arr.setXYZ(i, q.x, q.y, q.z); }
  arr.needsUpdate = true;
  r.trail.geometry.setDrawRange(0, n);
}

function onJpeg(id, payload) {
  const r = responder(id);
  const url = URL.createObjectURL(new Blob([payload], { type: "image/jpeg" }));
  r.img.src = url;
  if (r.imgURL) URL.revokeObjectURL(r.imgURL);
  r.imgURL = url;
  r.lastSeen = performance.now();
}

function onPoints(id, payload) {
  const r = responder(id);
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const n = dv.getUint32(0, true);
  // copy: payload offset may not be 4-byte aligned for Float32Array
  const xyz = new Float32Array(payload.slice(4, 4 + n * 12).buffer);
  const rgb = payload.subarray(4 + n * 12, 4 + n * 15);
  const pos = r.cloud.geometry.attributes.position, col = r.cloud.geometry.attributes.color;
  // copy in at most two slices (ring wrap) and upload only those slices, not the whole 90 MB buffer
  for (let done = 0; done < n;) {
    const j = r.ptsHead, k = Math.min(n - done, MAX_POINTS - j);
    pos.array.set(xyz.subarray(done * 3, (done + k) * 3), j * 3);
    col.array.set(rgb.subarray(done * 3, (done + k) * 3), j * 3);
    pos.addUpdateRange(j * 3, k * 3);
    col.addUpdateRange(j * 3, k * 3);
    r.ptsHead = (j + k) % MAX_POINTS;
    done += k;
  }
  r.ptsN = Math.min(MAX_POINTS, r.ptsN + n);
  pos.needsUpdate = col.needsUpdate = true;
  r.cloud.geometry.setDrawRange(0, r.ptsN);
  r.ptsFrames++;
}

// ---------- socket ----------
const conn = document.getElementById("conn");
let gpu = null;                        // latest status from the GPU worker, via the relay
const handlers = {
  pose: onPose,
  gpu: (d) => { gpu = { ...d, at: performance.now() }; },
  maplag: (d) => { responder(d.id).mapLag = d; },
};   // add-on modules register more types here
window.radar = { scene, responders, responder, handlers, THREE, camera, controls };

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/hub`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { conn.textContent = "live"; };
  ws.onclose = () => { conn.textContent = "disconnected, retrying…"; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const d = JSON.parse(ev.data);
      if (d.t === "state") {
        for (const [id, msgs] of Object.entries(d.responders))
          for (const m of Object.values(msgs)) handlers[m.t]?.({ ...m, id });
      } else handlers[d.t]?.(d);
      return;
    }
    const b = new Uint8Array(ev.data);
    const kind = b[0], idlen = b[1];
    const id = new TextDecoder().decode(b.subarray(2, 2 + idlen));
    const payload = b.subarray(6 + idlen);
    if (kind === 1) onJpeg(id, payload);
    else if (kind === 2) onPoints(id, payload);
    else if (kind === 3 && videoSupported) {
      const r = responder(id);
      r.lastSeen = performance.now();
      onVideo(r, new DataView(b.buffer, 2 + idlen, 4).getUint32(0, true), payload);
    }
  };
}
connect();

// ---------- tint + follow + status ----------
// Tint: colour each responder's points with their marker colour instead of camera colour,
// so you can see which phone mapped what and how the two clouds stitch together.
const tintBtn = document.getElementById("tint");
tintBtn.onclick = () => {
  tintOn = !tintOn;
  tintBtn.textContent = `Tint: ${tintOn ? "by responder" : "off"}`;
  tintBtn.setAttribute("aria-pressed", tintOn);
  for (const r of responders.values()) {
    r.cloud.material.vertexColors = !tintOn;
    r.cloud.material.color.set(tintOn ? r.color : 0xffffff);
    r.cloud.material.needsUpdate = true;
  }
};

// Connect: QR code of this relay's phone URL (+ "#ID"), scanned by the app's Scan button.
const qrBox = document.getElementById("qr"), connectBtn = document.getElementById("connect");
let qrId = "A";
function drawQR() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${qrId ? "#" + qrId : ""}`;
  const q = qrcode(0, "M"); q.addData(url); q.make();      // qrcode-generator, loaded in index.html
  document.getElementById("qr-img").innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
  document.getElementById("qr-url").textContent = url;
}
connectBtn.onclick = () => {
  qrBox.hidden = !qrBox.hidden;
  connectBtn.setAttribute("aria-pressed", !qrBox.hidden);
  if (!qrBox.hidden) drawQR();
};
for (const b of document.querySelectorAll("#qr-ids button")) b.onclick = () => {
  qrId = b.dataset.id;
  for (const o of document.querySelectorAll("#qr-ids button")) o.setAttribute("aria-pressed", o === b);
  drawQR();
};

const followBtn = document.getElementById("follow");
let followIdx = -1;
followBtn.onclick = () => {
  const ids = [...responders.keys()];
  followIdx = followIdx + 1 >= ids.length ? -1 : followIdx + 1;
  const on = followIdx >= 0;
  followBtn.textContent = on ? `Follow: ${ids[followIdx]}` : "Follow: off";
  followBtn.setAttribute("aria-pressed", on);
};

const status = document.getElementById("status");
setInterval(() => {
  const now = performance.now();
  status.innerHTML = [...responders.values()].map(r => {
    const age = (now - r.lastSeen) / 1000;
    const fps = r.poseTimes.length / 2;
    const stale = age > 2;
    while (r.videoTimes.length && now - r.videoTimes[0] > 2000) r.videoTimes.shift();
    const vfps = r.videoTimes.length / 2;
    const lag = r.mapLag ? `map lag ${r.mapLag.lag.toFixed(1)}s${r.mapLag.queued ? ` (${r.mapLag.queued} queued)` : ""}` : "map waiting";
    r.meta.textContent = `video ${vfps.toFixed(0)} fps · ${lag}`;
    return `<span class="${stale ? "warn" : ""}">${r.id}: ${stale ? `lost ${age.toFixed(0)}s` : `pose ${fps.toFixed(0)} fps`}, video ${vfps.toFixed(0)} fps, ${lag}, ${(r.ptsN / 1000).toFixed(0)}k pts</span>`;
  }).join("") || "<span>waiting for responders…</span>";
  const g = gpu && now - gpu.at < 6000;
  status.innerHTML += g ? `<span>GPU ${gpu.fps} fps · ${gpu.mem_gb} GB</span>` : `<span class="warn">GPU offline</span>`;
}, 500);

renderer.setAnimationLoop(() => {
  const ids = [...responders.keys()];
  if (followIdx >= 0 && ids[followIdx]) {
    const p = responders.get(ids[followIdx]).marker.position;
    controls.target.lerp(new THREE.Vector3(p.x, 0, p.z), 0.1);
  }
  controls.update();
  renderer.render(scene, camera);
});
