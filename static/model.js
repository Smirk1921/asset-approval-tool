/* 3D 模型预览：自己写的轻量 OBJ 查看器。
 *
 * 为什么不用 three.js：它要 import map 才能把 addons（OBJLoader/OrbitControls）
 * 串起来，而这个运行环境的浏览器不支持 import map。OBJ 的解析本身不复杂，
 * 自己写可控、零依赖、体积小。
 *
 * 支持：
 *   - OBJ 的 v / vn / f（含负数索引、斜杠分隔、多边形扇化）
 *   - 带法线的就用法线，没有就按面算 flat normal
 *   - 没有贴图时给一个带明暗的单色材质，能看清体积
 *   - 鼠标拖拽旋转、滚轮缩放、按住 Shift 拖拽平移
 *   - 左右两侧共用一套相机逻辑，可开关「同步视角」
 */
'use strict';

(function () {

  // ---------------------------------------------------------- OBJ 解析

  function parseObj(text) {
    const positions = [0, 0, 0];        // 1-based，前面补三个 0
    const normals = [0, 0, 0];
    const faces = [];
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line[0] === '#') continue;
      const parts = line.split(/\s+/);
      const cmd = parts[0];
      if (cmd === 'v') {
        positions.push(parseFloat(parts[1]) || 0, parseFloat(parts[2]) || 0, parseFloat(parts[3]) || 0);
      } else if (cmd === 'vn') {
        normals.push(parseFloat(parts[1]) || 0, parseFloat(parts[2]) || 0, parseFloat(parts[3]) || 0);
      } else if (cmd === 'f') {
        const face = [];
        for (let j = 1; j < parts.length; j++) {
          const tok = parts[j].split('/');
          let v = parseInt(tok[0], 10) || 0;
          if (v < 0) v = (positions.length / 3) + v;      // 负数是相对末尾
          let vn = 0;
          if (tok.length > 2 && tok[2]) {
            vn = parseInt(tok[2], 10) || 0;
            if (vn < 0) vn = (normals.length / 3) + vn;
          }
          face.push([v, vn]);
        }
        if (face.length >= 3) faces.push(face);
      }
    }
    return { positions, normals, faces };
  }

  function buildMesh(data) {
    const { positions, normals, faces } = data;
    const hasNormals = normals.length > 3;
    const verts = new Float32Array(faces.length * 3 * 3 * (hasNormals ? 2 : 1));
    const flatNormals = !hasNormals ? new Float32Array(faces.length * 3 * 3) : null;
    let o = 0, fo = 0;

    const min = [Infinity, Infinity, Infinity];
    const max = [-Infinity, -Infinity, -Infinity];

    function vertexAt(vi) {
      const x = positions[vi * 3], y = positions[vi * 3 + 1], z = positions[vi * 3 + 2];
      if (x < min[0]) min[0] = x; if (x > max[0]) max[0] = x;
      if (y < min[1]) min[1] = y; if (y > max[1]) max[1] = y;
      if (z < min[2]) min[2] = z; if (z > max[2]) max[2] = z;
      return [x, y, z];
    }

    for (const face of faces) {
      const n = face.length;
      for (let k = 0; k < n - 2; k++) {
        const tri = [face[0], face[k + 1], face[k + 2]];
        if (hasNormals) {
          for (const [vi, vni] of tri) {
            const [x, y, z] = vertexAt(vi);
            verts[o++] = x; verts[o++] = y; verts[o++] = z;
            const nx = normals[vni * 3] || 0, ny = normals[vni * 3 + 1] || 0, nz = normals[vni * 3 + 2] || 0;
            verts[o++] = nx; verts[o++] = ny; verts[o++] = nz;
          }
        } else {
          const p = tri.map(([vi]) => vertexAt(vi));
          const ux = p[1][0] - p[0][0], uy = p[1][1] - p[0][1], uz = p[1][2] - p[0][2];
          const vx = p[2][0] - p[0][0], vy = p[2][1] - p[0][1], vz = p[2][2] - p[0][2];
          let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
          const len = Math.hypot(nx, ny, nz) || 1;
          nx /= len; ny /= len; nz /= len;
          for (const [x, y, z] of p) {
            flatNormals[fo++] = x; flatNormals[fo++] = y; flatNormals[fo++] = z;
            flatNormals[fo++] = nx; flatNormals[fo++] = ny; flatNormals[fo++] = nz;
          }
        }
      }
    }

    const vertexData = hasNormals ? verts.subarray(0, o) : flatNormals.subarray(0, fo);
    return {
      vertexData,
      count: vertexData.length / 6,
      min, max,
      hasNormals,
    };
  }

  // ---------------------------------------------------------- 着色器

  const VERT = `
attribute vec3 aPos;
attribute vec3 aNormal;
uniform mat4 uMVP;
uniform mat4 uModel;
uniform mat3 uNormalMatrix;
varying vec3 vNormal;
varying vec3 vWorldPos;
void main() {
  vec4 world = uModel * vec4(aPos, 1.0);
  vWorldPos = world.xyz;
  vNormal = uNormalMatrix * aNormal;
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

  const FRAG = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vWorldPos;
uniform vec3 uColor;
uniform vec3 uLightDir;
uniform vec3 uLightColor;
uniform vec3 uAmbient;
uniform vec3 uCamPos;
uniform float uAlpha;
void main() {
  vec3 n = normalize(vNormal);
  vec3 l = normalize(uLightDir);
  float diff = max(dot(n, l), 0.0);
  vec3 v = normalize(uCamPos - vWorldPos);
  vec3 h = normalize(l + v);
  float spec = pow(max(dot(n, h), 0.0), 24.0) * 0.35;
  vec3 color = uAmbient * uColor + diff * uLightColor * uColor + spec * uLightColor;
  gl_FragColor = vec4(color, uAlpha);
}`;

  // ---------------------------------------------------------- 向量/矩阵工具

  function normalize3(v) {
    const l = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / l, v[1] / l, v[2] / l];
  }

  function cross3(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }

  function dot3(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

  function sub3(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }

  function perspective(fovy, aspect, near, far) {
    const t = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return [
      t / aspect, 0, 0, 0,
      0, t, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ];
  }

  function lookAt(eye, target, up) {
    const z = normalize3(sub3(eye, target));
    const x = normalize3(cross3(up, z));
    const y = cross3(z, x);
    return [
      x[0], y[0], z[0], 0,
      x[1], y[1], z[1], 0,
      x[2], y[2], z[2], 0,
      -dot3(x, eye), -dot3(y, eye), -dot3(z, eye), 1,
    ];
  }

  function multiply4(a, b) {
    const out = new Array(16);
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) {
        out[j * 4 + i] = 0;
        for (let k = 0; k < 4; k++) out[j * 4 + i] += a[k * 4 + i] * b[j * 4 + k];
      }
    }
    return out;
  }

  function normalMatrix3(m) {
    // 取视图矩阵的旋转部分（模型没有非均匀缩放时足够用）
    return [m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]];
  }

  // ---------------------------------------------------------- 查看器

  class ModelViewer {
    constructor(canvas) {
      this.canvas = canvas;
      this.gl = canvas.getContext('webgl', { antialias: true }) ||
                canvas.getContext('experimental-webgl', { antialias: true });
      if (!this.gl) throw new Error('WebGL 不可用');

      this._initProgram();
      this._initLights();

      // 相机（球坐标：target + 距离 + 方位角 + 仰角）
      this.target = [0, 0, 0];
      this.distance = 3;
      this.yaw = 0.6;
      this.pitch = 0.35;
      this.fov = 45 * Math.PI / 180;
      this.color = [0.72, 0.74, 0.78];

      this.mesh = null;
      this.stats = { vertices: 0, faces: 0 };
      this._raf = null;
      this._bindEvents();
    }

    _initProgram() {
      const gl = this.gl;
      function compile(type, src) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, src);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          throw new Error(gl.getShaderInfoLog(shader));
        }
        return shader;
      }
      const program = gl.createProgram();
      gl.attachShader(program, compile(gl.VERTEX_SHADER, VERT));
      gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(program));
      }
      this.program = program;
      this.locations = {
        pos: gl.getAttribLocation(program, 'aPos'),
        normal: gl.getAttribLocation(program, 'aNormal'),
        mvp: gl.getUniformLocation(program, 'uMVP'),
        model: gl.getUniformLocation(program, 'uModel'),
        normalMatrix: gl.getUniformLocation(program, 'uNormalMatrix'),
        color: gl.getUniformLocation(program, 'uColor'),
        lightDir: gl.getUniformLocation(program, 'uLightDir'),
        lightColor: gl.getUniformLocation(program, 'uLightColor'),
        ambient: gl.getUniformLocation(program, 'uAmbient'),
        camPos: gl.getUniformLocation(program, 'uCamPos'),
        alpha: gl.getUniformLocation(program, 'uAlpha'),
      };
      this.buffer = gl.createBuffer();
    }

    _initLights() {
      const gl = this.gl;
      gl.enable(gl.DEPTH_TEST);
      gl.clearColor(0.055, 0.06, 0.07, 1);
    }

    resize() {
      const parent = this.canvas.parentElement;
      if (!parent) return;
      const w = parent.clientWidth, h = parent.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) {
        this.canvas.width = w * dpr;
        this.canvas.height = h * dpr;
        this.canvas.style.width = w + 'px';
        this.canvas.style.height = h + 'px';
      }
    }

    async load(url) {
      const response = await fetch(url);
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const text = await response.text();
      const data = parseObj(text);
      this.mesh = buildMesh(data);
      this.stats = {
        vertices: this.mesh.count,
        faces: Math.round(this.mesh.count / 3),
      };
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, this.mesh.vertexData, gl.STATIC_DRAW);
      this._fitCamera(this.mesh);
      // 模型就绪后再算画布尺寸并渲第一帧（避免 resize 在模型加载前把画布设成默认大小）
      this.resize();
      this.render();
    }

    _fitCamera(mesh) {
      const cx = (mesh.min[0] + mesh.max[0]) / 2;
      const cy = (mesh.min[1] + mesh.max[1]) / 2;
      const cz = (mesh.min[2] + mesh.max[2]) / 2;
      this.target = [cx, cy, cz];
      const size = Math.max(
        mesh.max[0] - mesh.min[0],
        mesh.max[1] - mesh.min[1],
        mesh.max[2] - mesh.min[2],
        0.001,
      );
      const radius = size / 2;
      this.distance = radius / Math.tan(this.fov / 2) * 1.35;
      this.yaw = 0.6;
      this.pitch = 0.35;
    }

    eyePosition() {
      const cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
      const cy = Math.cos(this.yaw), sy = Math.sin(this.yaw);
      return [
        this.target[0] + this.distance * cp * sy,
        this.target[1] + this.distance * sp,
        this.target[2] + this.distance * cp * cy,
      ];
    }

    render() {
      if (!this.mesh) return;
      this.resize();
      const gl = this.gl;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

      gl.useProgram(this.program);
      const eye = this.eyePosition();
      const view = lookAt(eye, this.target, [0, 1, 0]);
      const aspect = this.canvas.width / this.canvas.height || 1;
      const proj = perspective(this.fov, aspect, this.distance / 100, this.distance * 10);
      const mvp = multiply4(proj, view);

      gl.uniformMatrix4fv(this.locations.mvp, false, new Float32Array(mvp));
      gl.uniformMatrix4fv(this.locations.model, false, new Float32Array([
        1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
      ]));
      gl.uniformMatrix3fv(this.locations.normalMatrix, false, new Float32Array(normalMatrix3(view)));
      gl.uniform3fv(this.locations.color, this.color);
      gl.uniform3fv(this.locations.lightDir, normalize3([0.5, 0.8, 0.6]));
      gl.uniform3fv(this.locations.lightColor, [1, 1, 1]);
      gl.uniform3fv(this.locations.ambient, [0.32, 0.32, 0.32]);
      gl.uniform3fv(this.locations.camPos, eye);
      gl.uniform1f(this.locations.alpha, 1);

      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.enableVertexAttribArray(this.locations.pos);
      gl.vertexAttribPointer(this.locations.pos, 3, gl.FLOAT, false, 24, 0);
      gl.enableVertexAttribArray(this.locations.normal);
      gl.vertexAttribPointer(this.locations.normal, 3, gl.FLOAT, false, 24, 12);

      gl.drawArrays(gl.TRIANGLES, 0, this.mesh.count);
    }

    start() {
      if (this._raf) return;
      const tick = () => { this._raf = requestAnimationFrame(tick); this.render(); };
      tick();
    }

    stop() {
      if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    }

    reset() {
      if (this.mesh) this._fitCamera(this.mesh);
      this.render();
    }

    syncFrom(other) {
      this.target = other.target.slice();
      this.distance = other.distance;
      this.yaw = other.yaw;
      this.pitch = other.pitch;
      this.render();
    }

    _bindEvents() {
      let dragging = false, lastX = 0, lastY = 0, panning = false;
      const canvas = this.canvas;

      canvas.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        dragging = true;
        panning = e.shiftKey;
        lastX = e.clientX;
        lastY = e.clientY;
        canvas.setPointerCapture(e.pointerId);
      });
      canvas.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const dx = e.clientX - lastX;
        const dy = e.clientY - lastY;
        lastX = e.clientX;
        lastY = e.clientY;
        if (panning) {
          const scale = this.distance / 600;
          const right = normalize3(cross3(sub3(this.target, this.eyePosition()), [0, 1, 0]));
          const up = [0, 1, 0];
          this.target[0] -= right[0] * dx * scale;
          this.target[2] -= right[2] * dx * scale;
          this.target[1] += dy * scale;
        } else {
          this.yaw += dx * 0.008;
          this.pitch = Math.max(-1.45, Math.min(1.45, this.pitch - dy * 0.008));
        }
        this.render();
        if (this._onUserChange) this._onUserChange(this);
      });
      const end = () => { dragging = false; panning = false; };
      canvas.addEventListener('mouseup', end);
      canvas.addEventListener('mouseleave', end);
      canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12;
        this.distance = Math.max(0.01, this.distance * factor);
        this.render();
        if (this._onUserChange) this._onUserChange(this);
      }, { passive: false });
    }
  }

  // ---------------------------------------------------------- 对外接口

  const viewers = new Map();
  let syncEnabled = true;
  let syncing = false;

  function fileUrl(item, side) {
    return '/api/set/' + encodeURIComponent(window.S.set.id) +
      '/file/' + encodeURIComponent(item.key) + '/' + side +
      (item.v ? '?v=' + item.v : '');
  }

  function showEmpty(side, message) {
    const empty = document.getElementById('modelEmpty' + side.toUpperCase());
    const canvas = document.getElementById('modelCanvas' + side.toUpperCase());
    if (empty) { empty.style.display = message ? 'flex' : 'none'; empty.textContent = message || ''; }
    if (canvas) canvas.style.display = message ? 'none' : '';
  }

  function setInfo(text) {
    const el = document.getElementById('modelInfo');
    if (el) el.textContent = text || '';
  }

  function syncPair(fromSide, toSide) {
    const from = viewers.get(fromSide);
    const to = viewers.get(toSide);
    if (!from || !to) return;
    from._onUserChange = (v) => {
      if (!syncEnabled || syncing) return;
      syncing = true;
      to.syncFrom(v);
      syncing = false;
    };
  }

  window.ModelPreview = {
    async renderModelPanes(item) {
      const wrap = document.getElementById('modelWrap');
      if (!wrap) return;
      const roles = { a: window.S.set.sourceA, b: window.S.set.sourceB };

      const tasks = [];
      for (const side of ['a', 'b']) {
        const info = item[side] || {};
        document.getElementById('modelRole' + side.toUpperCase()).textContent = roles[side];
        document.getElementById('modelMeta' + side.toUpperCase()).textContent =
          info.path ? info.path.split(/[\\/]/).slice(-2).join('/') : '';

        // 兜底：拿到的是轻量条目（只有 hasA/hasB，没有 a/b 细节）时从 hasX 判断
        if (!info.exists && typeof info.exists === 'undefined' && item['has' + side.toUpperCase()] === false) {
          showEmpty(side, '缺少该侧文件');
          continue;
        }
        if (!info.exists && item['has' + side.toUpperCase()] !== true) {
          showEmpty(side, info.missing_reason || '缺少该侧文件');
          continue;
        }
        showEmpty(side, '');

        let viewer = viewers.get(side);
        if (!viewer) {
          try {
            viewer = new ModelViewer(document.getElementById('modelCanvas' + side.toUpperCase()));
            viewers.set(side, viewer);
          } catch (err) {
            showEmpty(side, '3D 预览不可用：' + (err.message || err));
            continue;
          }
        }
        viewer.resize();
        tasks.push(
          viewer.load(fileUrl(item, side))
            .then(() => {
              viewer.start();
              setInfo('顶点 ' + viewer.stats.vertices.toLocaleString() +
                      ' · 面 ' + viewer.stats.faces.toLocaleString());
            })
            .catch((err) => showEmpty(side, '模型加载失败：' + (err.message || err))),
        );
      }
      await Promise.all(tasks);

      if (syncEnabled && viewers.has('a') && viewers.has('b')) {
        viewers.get('b').syncFrom(viewers.get('a'));
      }
      syncPair('a', 'b');
      syncPair('b', 'a');
    },

    setModelSync(on) {
      syncEnabled = !!on;
      const btn = document.getElementById('btnModelSync');
      if (btn) btn.classList.toggle('on', syncEnabled);
      if (syncEnabled && viewers.has('a') && viewers.has('b')) {
        viewers.get('b').syncFrom(viewers.get('a'));
      }
    },

    resetModelView() { viewers.forEach((v) => v.reset()); },
    resizeModelView() { viewers.forEach((v) => { v.resize(); v.render(); }); },
    stopModelView() { viewers.forEach((v) => v.stop()); },
  };
})();
