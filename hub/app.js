// RadAR hub. Protocol: server/relay/protocol.md
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { onVideo, videoSupported } from "./video.js";

const COLORS = ["#3fa7ff", "#ff9f1c", "#2ec4b6", "#e84855"];
let tintOn = false;                 // see the Tint button below
const MAX_POINTS = 6_000_000;   // per responder (90 MB); ring buffer wraps when full
const PATH_STEP = 0.05;         // m; the movement path keeps one point per 5 cm walked, for the whole session
const PATH_CAP = 200_000;       // points per responder (10 km of walking)

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
  // AirPods head direction (protocol `head`): the wearer's field of view, 60° wide x 70° tall, as the
  // outline of a cone with its tip at the eyes opening along -Z, independent of the chest camera
  const FOV_LEN = 1.2, rx = FOV_LEN * Math.tan(30 * Math.PI / 180), ry = FOV_LEN * Math.tan(35 * Math.PI / 180);
  const fov = [];
  for (let i = 0; i < 48; i++) {                       // rim ellipse
    const a = i / 48 * 2 * Math.PI, b = (i + 1) / 48 * 2 * Math.PI;
    fov.push(rx * Math.cos(a), ry * Math.sin(a), -FOV_LEN, rx * Math.cos(b), ry * Math.sin(b), -FOV_LEN);
  }
  for (let i = 0; i < 8; i++) {                        // edges from the eyes to the rim
    const a = i / 8 * 2 * Math.PI;
    fov.push(0, 0, 0, rx * Math.cos(a), ry * Math.sin(a), -FOV_LEN);
  }
  const fovGeo = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(fov, 3));
  const head = new THREE.LineSegments(fovGeo, new THREE.LineBasicMaterial({ color: color.clone().lerp(new THREE.Color("#fff"), 0.5), transparent: true, opacity: 0.8 }));
  head.position.y = 0.35; head.rotation.order = "YXZ"; head.visible = false;
  marker.add(head);
  marker.visible = false;
  scene.add(marker);

  const trailGeo = new THREE.BufferGeometry();       // movement path on the floor, whole session
  trailGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(PATH_CAP * 3), 3));
  trailGeo.setDrawRange(0, 0);
  const trail = new THREE.Line(trailGeo, new THREE.LineBasicMaterial({ color }));
  trail.frustumCulled = false;
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
    <b>Responder ${id}</b><span class="mic" hidden title="talking">🎙</span><span class="meta"></span></header>
    <div class="vid"><img alt="Live camera from responder ${id}"><canvas aria-label="Live video from responder ${id}"></canvas>
    <span class="hr" hidden aria-live="off"></span></div>`;
  side.appendChild(el);

  r = { id, color, marker, arrow, head, hrEl: el.querySelector(".hr"), hrAt: 0, mic: el.querySelector(".mic"), trail, pathN: 0, pathLast: null, cloud, ptsN: 0, ptsHead: 0,
        img: el.querySelector("img"), meta: el.querySelector(".meta"), imgURL: null,
        canvas: el.querySelector("canvas"), feed: el, decoder: null, videoTimes: [], mapLag: null,
        pose: null, lastSeen: 0, poseTimes: [], ptsFrames: 0, patches, patchN: 0, patchByF: new Map() };
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

  addPathPoint(r, _p.x, _p.z);
}

function addPathPoint(r, x, z) {
  const L = r.pathLast;
  if (L && Math.hypot(x - L.x, z - L.z) < PATH_STEP) return;
  if (r.pathN >= PATH_CAP) return;
  const arr = r.trail.geometry.attributes.position;
  arr.setXYZ(r.pathN, x, 0.02, z);
  arr.addUpdateRange(r.pathN * 3, 3);
  arr.needsUpdate = true;
  r.pathN++;
  r.pathLast = { x, z };
  r.trail.geometry.setDrawRange(0, r.pathN);
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

async function onPatch(id, f, payload) {
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

  const tex = texture(bitmap);
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide }));
  mesh.userData.origMat = mesh.material;               // enhanced view swaps the material (see onHD)
  r.patchByF.set(f, mesh);
  mesh.visible = photosOn;
  const dots = new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.012, vertexColors: true }));   // sprites alias badly on a regular grid
  dots.visible = !photosOn;
  r.patches.add(mesh); r.patches.add(dots);
  r.patchN++;
}
function texture(bitmap) {
  const tex = new THREE.Texture(bitmap);
  tex.flipY = false; tex.colorSpace = THREE.SRGBColorSpace; tex.generateMipmaps = false;
  tex.minFilter = THREE.LinearFilter; tex.needsUpdate = true;
  return tex;
}

const surfaceBtn = document.getElementById("surface");
surfaceBtn.onclick = () => {
  photosOn = !photosOn;
  surfaceBtn.textContent = `Surface: ${photosOn ? "photos" : "dots"}`;
  surfaceBtn.setAttribute("aria-pressed", photosOn);
  for (const r of responders.values()) for (const o of r.patches.children) o.visible = o.isMesh ? photosOn : !photosOn;
};

function clearMap() {                                  // relay started a new map file
  clearEnhanced();
  for (const r of responders.values()) {
    for (const o of r.patches.children) { o.geometry.dispose(); o.material.map?.dispose(); o.material.dispose(); }
    r.patches.clear(); r.patchN = 0; r.patchByF.clear();
    r.ptsN = r.ptsHead = 0; r.cloud.geometry.setDrawRange(0, 0);
    r.pathN = 0; r.pathLast = null; r.trail.geometry.setDrawRange(0, 0);
  }
}

// ---------- socket ----------
const conn = document.getElementById("conn");
let gpu = null;                        // latest status from the GPU worker, via the relay
const handlers = {
  pose: onPose,
  gpu: (d) => { gpu = { ...d, at: performance.now() }; },
  maplag: (d) => { responder(d.id).mapLag = d; },
  recording: onRecording,
  hr: (d) => { const r = responder(d.id); r.hrEl.hidden = false; r.hrEl.textContent = `♥ ${Math.round(d.bpm)}`; r.hrAt = performance.now(); },
  head: (d) => {                       // yaw in the ARKit world frame; the cone hangs off the yaw-only marker
    const r = responder(d.id);
    r.head.rotation.set(d.pitch, d.yaw - r.marker.rotation.y, 0);
    r.head.visible = true;
  },
  talk: (d) => { talking[d.id] = d.on; if (d.id !== "hub") responder(d.id).mic.hidden = !d.on; },
  state: (d) => { for (const [id, msgs] of Object.entries(d.responders)) for (const m of Object.values(msgs)) handlers[m.t]?.({ ...m, id }); },
};   // add-on modules register more types here

function onBinary(b) {                 // one relay binary message (live socket or a saved map file)
  const kind = b[0], idlen = b[1];
  const id = new TextDecoder().decode(b.subarray(2, 2 + idlen));
  const payload = b.subarray(6 + idlen);
  const f = new DataView(b.buffer, b.byteOffset + 2 + idlen, 4).getUint32(0, true);
  if (kind === 1) onJpeg(id, payload);
  else if (kind === 2) onPoints(id, payload);
  else if (kind === 3 && videoSupported) {
    const r = responder(id);
    r.lastSeen = performance.now();
    onVideo(r, f, payload);
  } else if (kind === 5) return onPatch(id, f, payload);   // async: map loader awaits it
  else if (kind === 6) return onHD(id, f, payload);
  else if (kind === 7) onAudio(id, payload);
}
window.radar = { scene, responders, responder, handlers, onBinary, THREE, camera, controls };

// ---------- voice call (protocol kind 7: 16 kHz int16 PCM, push-to-talk party line) ----------
let actx = null, sock = null, talk = null, seq = 0, worklet = null;
const talking = {}, nextAt = {};
const audioCtx = () => actx ??= new AudioContext();
addEventListener("pointerdown", () => audioCtx().resume(), { capture: true });   // browsers only play after a click
function onAudio(id, payload) {
  const ctx = audioCtx();
  if (ctx.state !== "running") return;
  const n = payload.length >> 1, dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const buf = ctx.createBuffer(1, n, 16000), ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
  const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
  const now = ctx.currentTime, t = (nextAt[id] ?? 0) > now + 1 ? now + 0.1 : Math.max(now + 0.1, nextAt[id] ?? 0);   // 100 ms jitter buffer
  src.start(t); nextAt[id] = t + buf.duration;
}
// mic -> 16 kHz int16, 40 ms chunks. ponytail: box-average decimation, a real low-pass if it sounds harsh
const PCM_WORKLET = URL.createObjectURL(new Blob([`registerProcessor("pcm16k", class extends AudioWorkletProcessor {
  constructor() { super(); this.step = sampleRate / 16000; this.acc = 0; this.sum = 0; this.cnt = 0; this.out = new Int16Array(640); this.n = 0; }
  process([input]) {
    for (const x of input[0] || []) {
      this.sum += x; this.cnt++;
      if (++this.acc < this.step) continue;
      this.acc -= this.step;
      this.out[this.n++] = Math.max(-1, Math.min(1, this.sum / this.cnt)) * 32767; this.sum = this.cnt = 0;
      if (this.n === 640) { this.port.postMessage(this.out.buffer, [this.out.buffer]); this.out = new Int16Array(640); this.n = 0; }
    }
    return true;
  }
});`], { type: "text/javascript" }));
const talkBtn = document.getElementById("talk");
talkBtn.onclick = async () => {
  if (talk) {
    talk.stream.getTracks().forEach(t => t.stop()); talk.node.port.onmessage = null; talk.src.disconnect(); talk.node.disconnect(); talk = null;
  } else {
    try {
      const ctx = audioCtx(); await ctx.resume();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      await (worklet ??= ctx.audioWorklet.addModule(PCM_WORKLET));
      const node = new AudioWorkletNode(ctx, "pcm16k");
      node.port.onmessage = (e) => {
        if (sock?.readyState !== 1) return;
        const b = new Uint8Array(9 + e.data.byteLength);
        b.set([7, 3, 104, 117, 98]);                                    // kind 7, id "hub"
        new DataView(b.buffer).setUint32(5, seq++, true);
        b.set(new Uint8Array(e.data), 9);
        sock.send(b);
      };
      const src = ctx.createMediaStreamSource(stream);
      src.connect(node); node.connect(ctx.destination);   // node outputs silence; this keeps it pulled
      talk = { stream, src, node };
    } catch (err) { alert(`Talk: ${err.message}`); }
  }
  sock?.send(JSON.stringify({ t: "talk", on: !!talk }));
  talkBtn.textContent = talk ? "🎙 Talking (click to stop)" : "🎙 Talk";
  talkBtn.setAttribute("aria-pressed", !!talk);
};

function connect() {
  const ws = sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/hub`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { conn.textContent = "live"; };
  ws.onclose = () => { conn.textContent = "disconnected, retrying…"; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const d = JSON.parse(ev.data);
      handlers[d.t]?.(d);
      return;
    }
    onBinary(new Uint8Array(ev.data));
  };
}
connect();

// ---------- saved maps (relay: GET /maps, GET /maps/<name>, POST /maps/new) ----------
const mapsBox = document.getElementById("mapsbox"), mapsBtn = document.getElementById("maps");
// ---------- recording ----------
// The relay records a session only between "Record new map" and "Stop recording"; the file is named
// by the user and goes to S3 when stopped. The relay tells every hub the recording state.
const recEl = document.getElementById("rec"), recordBtn = document.getElementById("record");
let recording = { name: null, since: null };
function onRecording(d) {
  if (d.name && d.name !== recording.name) clearMap();        // a new map starts from an empty view
  recording = d;
  recordBtn.textContent = d.name ? "■ Stop recording" : "● Record new map";
  recordBtn.classList.toggle("on", !!d.name);
  recEl.className = ""; recEl.hidden = !d.name;
}
recordBtn.onclick = async () => {
  if (recording.name) {
    const { saved } = await (await fetch("/maps/stop", { method: "POST" })).json();
    recEl.hidden = false; recEl.className = "saved";
    recEl.textContent = saved?.mapped ? `saved "${saved.name.replace(/\.radar$/, "")}" (${saved.patches} patches) · uploading to S3` : "nothing was mapped, not saved";
    setTimeout(() => { if (!recording.name) recEl.hidden = true; }, 6000);
    setTimeout(loadMaps, 4000);
    return;
  }
  const name = prompt("Name this map", new Date().toISOString().slice(0, 16).replace("T", " "));
  if (name === null) return;
  await fetch("/maps/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
};

// ---------- past maps ----------
// Click a name to load just its map (points, patches) at once. ▶ replays the whole session on its
// original timeline: video, markers, map growth. ↓ downloads the file, ✕ deletes it everywhere.
let replaying = null;                   // name of the file being replayed, or null
async function loadMaps() {
  const d = await (await fetch("/maps")).json();
  document.getElementById("maps-where").textContent = d.bucket ? `s3://${d.bucket}` : "relay disk only";
  document.getElementById("maps-list").innerHTML = d.maps.map(m =>
    `<li><button class="name" data-name="${m.name}" data-mode="map" title="load this map">${m.name.replace(/\.radar$/, "")}
       <span class="meta">${Math.floor(m.seconds / 60)}:${String(m.seconds % 60).padStart(2, "0")} · ${m.patches} patches · ${(m.bytes / 1e6).toFixed(1)} MB · ${m.where === "s3" ? "☁ S3" : m.where === "both" ? "☁ + local" : "local"}</span></button>
     <span>${enhanceControl(m)}<button data-name="${m.name}" data-mode="replay" title="replay with video">${replaying === m.name ? "■" : "▶"}</button>
     <a href="/maps/${m.name}" download title="download file">↓</a>
     <button data-name="${m.name}" data-mode="delete" title="delete everywhere">✕</button></span></li>`).join("") || "<li class='meta'>no saved maps yet</li>";
  for (const b of document.querySelectorAll("#maps-list button")) b.onclick = () => playMap(b.dataset.name, b.dataset.mode, b);
  mapsMeta = Object.fromEntries(d.maps.map(m => [m.name, m]));
  const busy = d.maps.some(m => ["queued", "running"].includes(m.enhanced?.status));
  clearTimeout(mapsPoll);
  if (busy && !mapsBox.hidden) mapsPoll = setTimeout(loadMaps, 5000);     // watch the enhancer work
}
let mapsMeta = {}, mapsPoll = null;
function enhanceControl(m) {              // ✦ Enhance | ✦ queued… | ✦ enhanced  (the EC2 enhancer, see enhance.py)
  const e = m.enhanced, s = e?.status;
  if (s === "done" && e.hd) return `<span class="meta" title="${e.phones?.length} phone(s), ${(e.bytes / 1e6).toFixed(0)} MB, ${e.seconds}s">✦ enhanced</span>`;
  if (s === "queued" || s === "running") return `<span class="meta">✦ ${s}…</span>`;
  const why = s === "failed" ? `last attempt failed: ${e.error}` : "re-texture this map with sharp photos from its video (takes a few minutes)";
  return `<button data-name="${m.name}" data-mode="enhance" title="${why.replace(/"/g, "'")}">✦ ${s === "failed" ? "Retry" : "Enhance"}</button>`;
}

// ---------- enhanced: the same patches, re-textured from the video (enhance.py -> <name>.hd) ----------
const viewBtn = document.getElementById("viewmode");
let viewMode = "original";
async function onHD(id, f, payload) {     // kind 6 for patch f: u32 hlen, {sf, T, K normalised}, JPEG
  const mesh = responders.get(id)?.patchByF.get(f);
  if (!mesh) return;
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const hl = dv.getUint32(0, true);
  const h = JSON.parse(new TextDecoder().decode(payload.subarray(4, 4 + hl)));
  const bitmap = await createImageBitmap(new Blob([payload.subarray(4 + hl)], { type: "image/jpeg" }));
  // uv: project each world vertex into the source frame's camera (ARKit axes, -Z forward); behind it = off-image
  const inv = new THREE.Matrix4().fromArray(h.T).invert(), [kx, ky, kcx, kcy] = h.K;
  const pos = mesh.geometry.attributes.position, uv = new Float32Array(pos.count * 2), v = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).applyMatrix4(inv);
    const z = -v.z;
    uv[i * 2] = z > 0 ? kx * v.x / z + kcx : -1; uv[i * 2 + 1] = z > 0 ? -ky * v.y / z + kcy : -1;
  }
  mesh.geometry.setAttribute("uvHD", new THREE.BufferAttribute(uv, 2));
  if (mesh.userData.hd) { mesh.material = mesh.userData.origMat; mesh.userData.hd.tex.dispose(); mesh.userData.hd.mat.dispose(); }
  const tex = texture(bitmap);
  mesh.userData.hd = { tex, mat: hdMaterial(mesh.userData.origMat.map, tex) };
  skin(mesh);
}
// The sharp frame is a neighbour of the patch's own frame, so it may not see all of the patch: draw it
// where its uv lands inside the image and the original photo everywhere else (no smeared edge pixels).
function hdMaterial(orig, hd) {
  const m = new THREE.MeshBasicMaterial({ map: orig, side: THREE.DoubleSide });
  m.onBeforeCompile = (sh) => {
    sh.uniforms.mapHD = { value: hd };
    sh.vertexShader = "attribute vec2 uvHD;\nvarying vec2 vUvHD;\n" + sh.vertexShader.replace("#include <uv_vertex>", "#include <uv_vertex>\nvUvHD = uvHD;");
    sh.fragmentShader = "uniform sampler2D mapHD;\nvarying vec2 vUvHD;\n" + sh.fragmentShader.replace("#include <map_fragment>",
      `bool inHD = all(greaterThanEqual(vUvHD, vec2(0.0))) && all(lessThanEqual(vUvHD, vec2(1.0)));
       diffuseColor *= inHD ? texture2D(mapHD, vUvHD) : texture2D(map, vMapUv);`);
  };
  m.customProgramCacheKey = () => "radar-hd";
  return m;
}
function skin(mesh) {                     // original or enhanced material
  // ponytail: both textures stay on the GPU (~5 MB a patch, ~1 GB for 189) so the toggle is instant;
  // dispose the hidden set if a big map runs out of GPU memory (re-upload then stalls a few seconds)
  const hd = mesh.userData.hd;
  mesh.material = viewMode === "enhanced" && hd ? hd.mat : mesh.userData.origMat;
}
async function loadEnhanced(name, onProgress) {
  viewBtn.hidden = false; setView("enhanced");
  for await (const r of records(`${name}.hd`, onProgress)) if (!r.text) await onBinary(r.body);
}
function clearEnhanced() {
  setView("original"); viewBtn.hidden = true;
  for (const r of responders.values()) for (const m of r.patchByF.values()) { m.userData.hd?.tex.dispose(); m.userData.hd?.mat.dispose(); delete m.userData.hd; }
}
function setView(mode) {                 // Original | Enhanced
  viewMode = mode;
  for (const b of viewBtn.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.view === mode);
  for (const r of responders.values()) for (const m of r.patchByF.values()) skin(m);
}
for (const b of viewBtn.querySelectorAll("button")) b.onclick = () => setView(b.dataset.view);
async function* records(name, onProgress) {   // RADAR2: u32 len | u32 t_ms | u8 type | body, parsed as it downloads
  const res = await fetch(`/maps/${name}`);
  if (!res.ok) throw new Error(`${res.status} fetching the map`);
  const total = +res.headers.get("Content-Length") || mapsMeta[name]?.bytes || 0;
  const reader = res.body.getReader();
  let buf = new Uint8Array(0), pos = 0, got = 0, first = true;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    got += value.length; onProgress?.(got, total);
    buf = pos ? concat(buf.subarray(pos), value) : concat(buf, value); pos = 0;      // keep only the unparsed tail
    if (first) {
      if (buf.length < 7) continue;
      if (new TextDecoder().decode(buf.subarray(0, 7)) !== "RADAR2\n") throw new Error("old file format");
      pos = 7; first = false;
    }
    const dv = new DataView(buf.buffer, buf.byteOffset);
    while (pos + 4 <= buf.length) {
      const len = dv.getUint32(pos, true);
      if (pos + 4 + len > buf.length) break;                                           // record not complete yet
      yield { t: dv.getUint32(pos + 4, true), text: buf[pos + 8] === 0, body: buf.subarray(pos + 9, pos + 4 + len) };
      pos += 4 + len;
    }
  }
}
const concat = (a, b) => { if (!a.length) return b; const c = new Uint8Array(a.length + b.length); c.set(a); c.set(b, a.length); return c; };
const mb = (n) => (n / 1e6).toFixed(0);
async function playMap(name, mode, btn) {
  if (mode === "delete") {
    if (!confirm(`Delete ${name} from the relay and S3?`)) return;
    await fetch(`/maps/${name}`, { method: "DELETE" });
    return loadMaps();
  }
  if (mode === "replay" && replaying === name) { replaying = null; return; }      // Stop
  if (mode === "enhance") {
    btn.disabled = true;
    const res = await fetch(`/maps/${name}/enhance`, { method: "POST" });
    if (!res.ok) alert(`Enhance: ${(await res.json()).detail}`);
    return loadMaps();
  }
  const label2 = name.replace(/\.radar$/, "");
  let progress = "";                                   // "12 / 49 MB" while the file streams in
  const onProgress = (got, total) => { progress = total ? `${mb(got)} / ${mb(total)} MB` : `${mb(got)} MB`; };
  recEl.hidden = false; recEl.className = "saved";
  if (mode === "map") {
    btn.disabled = true;
    clearEnhanced();
    const td = new TextDecoder();
    for (const r of responders.values()) { r.pathN = 0; r.pathLast = null; r.trail.geometry.setDrawRange(0, 0); }   // paths come from the file
    try {
      for await (const r of records(name, onProgress)) {             // draws as the file arrives
        recEl.textContent = `loading "${label2}" ${progress}`;
        if (!r.text) { if (r.body[0] === 2 || r.body[0] === 5) await onBinary(r.body); continue; }
        const d = JSON.parse(td.decode(r.body));
        if (d.t === "pose") { _m.fromArray(d.T); addPathPoint(responder(d.id), _m.elements[12], _m.elements[14]); }   // walked path
      }
    } catch (err) { recEl.textContent = `load failed: ${err.message}`; btn.disabled = false; return; }
    btn.disabled = false;
    recEl.textContent = `loaded "${label2}"`;
    const e = mapsMeta[name]?.enhanced;
    if (e?.status === "done" && e.hd) {
      try {
        await loadEnhanced(name, (got, total) => { recEl.textContent = `loaded "${label2}" · sharp photos ${mb(got)} / ${mb(total)} MB`; });
        recEl.textContent = `loaded "${label2}" (enhanced)`;
      } catch (err) { console.error(err); recEl.textContent = `loaded "${label2}" · sharp photos failed: ${err.message}`; }
    }
    setTimeout(() => { if (!recording.name) recEl.hidden = true; }, 4000);
    return;
  }
  replaying = name; btn.textContent = "■";
  clearMap();
  // Play on the recorded timeline, but never sit through dead air: only phone traffic (poses and
  // binary frames) advances the clock, and a gap of more than MAX_GAP between them (phones not yet
  // connected, nobody moving) is cut to MAX_GAP. Relay status records just ride along.
  // The file streams in while it plays (it only needs ~0.5 MB/s to stay ahead of real time); the
  // total comes from the listing since the last record is not known until the download ends.
  const MAX_GAP = 2000, total = (mapsMeta[name]?.seconds || 0) * 1000;
  let clock = 0, prevT = 0, wall = performance.now();
  const td = new TextDecoder();
  try {
    for await (const r of records(name, onProgress)) {
      if (replaying !== name) break;
      const d = r.text ? JSON.parse(td.decode(r.body)) : null;
      if (!d || d.t === "pose") {
        clock += Math.min(r.t - prevT, MAX_GAP); prevT = r.t;
        const wait = clock - (performance.now() - wall);
        if (wait > 0) await new Promise(res => setTimeout(res, wait));
        recEl.hidden = false; recEl.className = "saved";
        recEl.textContent = `▶ replaying "${label2}" ${mmss(r.t / 1000)} / ${mmss(total / 1000)} · ${progress}`;
      }
      if (d) handlers[d.t]?.(d);
      else await onBinary(r.body);
    }
  } catch (err) { recEl.textContent = `replay failed: ${err.message}`; }
  if (replaying === name) replaying = null;
  btn.textContent = "▶";
  recEl.textContent = `▶ finished "${label2}"`;
  setTimeout(() => { if (!recording.name) recEl.hidden = true; }, 4000);
}
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
mapsBtn.onclick = () => {
  mapsBox.hidden = !mapsBox.hidden;
  mapsBtn.setAttribute("aria-pressed", !mapsBox.hidden);
  if (!mapsBox.hidden) loadMaps();
};

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
    r.hrEl.classList.toggle("stale", now - r.hrAt > 10000);
    const lag = r.mapLag ? `map lag ${r.mapLag.lag.toFixed(1)}s${r.mapLag.queued ? ` (${r.mapLag.queued} queued)` : ""}` : "map waiting";
    r.meta.textContent = `video ${vfps.toFixed(0)} fps · ${lag}`;
    return `<span class="${stale ? "warn" : ""}">${r.id}: ${stale ? `lost ${age.toFixed(0)}s` : `pose ${fps.toFixed(0)} fps`}, video ${vfps.toFixed(0)} fps, ${lag}, ${r.patchN ? `${r.patchN} patches` : `${(r.ptsN / 1000).toFixed(0)}k pts`}</span>`;
  }).join("") || "<span>waiting for responders…</span>";
  if (recording.name && !recEl.classList.contains("saved") && !replaying) {
    const el = Math.max(0, Math.floor(Date.now() / 1000 - recording.since));
    recEl.innerHTML = `<b>●</b> recording "${recording.name}" ${Math.floor(el / 60)}:${String(el % 60).padStart(2, "0")}`;
  }
  if (talking.hub) status.innerHTML += `<span>🎙 hub talking</span>`;
  if (actx?.state !== "running") status.innerHTML += `<span class="warn">🔈 click anywhere to hear the call</span>`;
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
