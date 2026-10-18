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

  const patches = new THREE.Group();   // LiDAR photo patches (meshes) and their dot twins
  scene.add(patches);

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
        pose: null, lastSeen: 0, poseTimes: [], ptsFrames: 0, patches, patchN: 0 };
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

// ---------- LiDAR patches: the photo draped over its depth (protocol kind 5) ----------
let photosOn = true;                   // Surface button: textured patches, or their vertices as dots

function f16(h) {                      // IEEE half -> float (no Float16Array in every browser yet)
  const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, m = h & 0x3ff;
  return e === 0 ? s * m * 2 ** -24 : e === 31 ? (m ? NaN : s * Infinity) : s * (1 + m / 1024) * 2 ** (e - 15);
}

async function onPatch(id, payload) {
  const r = responder(id);
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const hl = dv.getUint32(0, true);
  const h = JSON.parse(new TextDecoder().decode(payload.subarray(4, 4 + hl)));
  const { dw, dh, s } = h, n = dw * dh;
  const depth = new Float32Array(n);
  for (let i = 0; i < n; i++) depth[i] = f16(dv.getUint16(4 + hl + i * 2, true));
  const jpeg = payload.subarray(4 + hl + n * 2);
  const bitmap = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));

  // vertices: unproject each grid sample with K (lens of the w x h image), ARKit camera axes (-Z forward)
  const [fx, fy, cx, cy] = h.K;
  const pos = new Float32Array(n * 3), uv = new Float32Array(n * 2), col = new Uint8Array(n * 3);
  const cv = document.createElement("canvas"); cv.width = bitmap.width; cv.height = bitmap.height;
  const g = cv.getContext("2d", { willReadFrequently: true }); g.drawImage(bitmap, 0, 0);
  const px = g.getImageData(0, 0, cv.width, cv.height).data;
  for (let j = 0; j < dh; j++) for (let i = 0; i < dw; i++) {
    const k = j * dw + i, u = i * s, v = j * s, z = depth[k];
    pos[k * 3] = (u - cx) / fx * z; pos[k * 3 + 1] = -(v - cy) / fy * z; pos[k * 3 + 2] = -z;
    uv[k * 2] = u / h.w; uv[k * 2 + 1] = v / h.h;
    const q = (Math.min(v, cv.height - 1) * cv.width + Math.min(u, cv.width - 1)) * 4;
    col[k * 3] = px[q]; col[k * 3 + 1] = px[q + 1]; col[k * 3 + 2] = px[q + 2];
  }
  // triangles only where all four corners have depth and agree within 5% (no rubber sheets over edges)
  const idx = [];
  for (let j = 0; j < dh - 1; j++) for (let i = 0; i < dw - 1; i++) {
    const a = j * dw + i, b = a + 1, c = a + dw, d = c + 1;
    const z0 = depth[a], z1 = depth[b], z2 = depth[c], z3 = depth[d];
    if (!(z0 > 0 && z1 > 0 && z2 > 0 && z3 > 0)) continue;
    const lo = Math.min(z0, z1, z2, z3), hi = Math.max(z0, z1, z2, z3);
    if (hi > lo * 1.05) continue;
    idx.push(a, c, b, b, c, d);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3, true));
  geo.setIndex(idx);
  geo.applyMatrix4(_m.fromArray(h.T));                 // camera -> world (column-major, same as THREE)

  const tex = new THREE.Texture(bitmap);
  tex.flipY = false; tex.colorSpace = THREE.SRGBColorSpace; tex.generateMipmaps = false;
  tex.minFilter = THREE.LinearFilter; tex.needsUpdate = true;
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide }));
  mesh.visible = photosOn;
  const dots = new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.012, vertexColors: true }));   // sprites alias badly on a regular grid
  dots.visible = !photosOn;
  r.patches.add(mesh); r.patches.add(dots);
  r.patchN++;
}

const surfaceBtn = document.getElementById("surface");
surfaceBtn.onclick = () => {
  photosOn = !photosOn;
  surfaceBtn.textContent = `Surface: ${photosOn ? "photos" : "dots"}`;
  surfaceBtn.setAttribute("aria-pressed", photosOn);
  for (const r of responders.values()) for (const o of r.patches.children) o.visible = o.isMesh ? photosOn : !photosOn;
};

function clearMap() {                                  // relay started a new map file
  for (const r of responders.values()) {
    for (const o of r.patches.children) { o.geometry.dispose(); o.material.map?.dispose(); o.material.dispose(); }
    r.patches.clear(); r.patchN = 0;
    r.ptsN = r.ptsHead = 0; r.cloud.geometry.setDrawRange(0, 0);
  }
}

// ---------- socket ----------
const conn = document.getElementById("conn");
let gpu = null;                        // latest status from the GPU worker, via the relay
const handlers = {
  pose: onPose,
  gpu: (d) => { gpu = { ...d, at: performance.now() }; },
  maplag: (d) => { responder(d.id).mapLag = d; },
  map_new: () => { clearMap(); loadMaps(); },
};   // add-on modules register more types here

function onBinary(b) {                 // one relay binary message (live socket or a saved map file)
  const kind = b[0], idlen = b[1];
  const id = new TextDecoder().decode(b.subarray(2, 2 + idlen));
  const payload = b.subarray(6 + idlen);
  if (kind === 1) onJpeg(id, payload);
  else if (kind === 2) onPoints(id, payload);
  else if (kind === 3 && videoSupported) {
    const r = responder(id);
    r.lastSeen = performance.now();
    onVideo(r, new DataView(b.buffer, b.byteOffset + 2 + idlen, 4).getUint32(0, true), payload);
  } else if (kind === 5) return onPatch(id, payload);   // async: map loader awaits it
}
window.radar = { scene, responders, responder, handlers, onBinary, THREE, camera, controls };

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
    onBinary(new Uint8Array(ev.data));
  };
}
connect();

// ---------- saved maps (relay: GET /maps, GET /maps/<name>, POST /maps/new) ----------
const mapsBox = document.getElementById("mapsbox"), mapsBtn = document.getElementById("maps");
async function loadMaps() {
  const d = await (await fetch("/maps")).json();
  document.getElementById("maps-current").textContent = `recording: ${d.current}`;
  document.getElementById("maps-list").innerHTML = d.maps.map(m =>
    `<li><span>${m.name}<br><span class="meta">${m.patches} patches · ${(m.bytes / 1e6).toFixed(1)} MB</span></span>
     <button data-name="${m.name}">Load</button></li>`).join("");
  for (const b of document.querySelectorAll("#maps-list button")) b.onclick = () => loadMap(b.dataset.name, b);
}
async function loadMap(name, btn) {
  btn.textContent = "loading…"; btn.disabled = true;
  const buf = new Uint8Array(await (await fetch(`/maps/${name}`)).arrayBuffer());
  const dv = new DataView(buf.buffer);
  for (let pos = 0; pos + 4 <= buf.length;) {           // u32 len | binary message
    const len = dv.getUint32(pos, true);
    await onBinary(buf.subarray(pos + 4, pos + 4 + len));
    pos += 4 + len;
  }
  btn.textContent = "loaded"; btn.disabled = false;
}
mapsBtn.onclick = () => {
  mapsBox.hidden = !mapsBox.hidden;
  mapsBtn.setAttribute("aria-pressed", !mapsBox.hidden);
  if (!mapsBox.hidden) loadMaps();
};
document.getElementById("maps-new").onclick = async () => { await fetch("/maps/new", { method: "POST" }); };

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
    return `<span class="${stale ? "warn" : ""}">${r.id}: ${stale ? `lost ${age.toFixed(0)}s` : `pose ${fps.toFixed(0)} fps`}, video ${vfps.toFixed(0)} fps, ${lag}, ${r.patchN ? `${r.patchN} patches` : `${(r.ptsN / 1000).toFixed(0)}k pts`}</span>`;
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
