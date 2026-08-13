// Минимальный браузерный шим для запуска fireyejs.js в чистом V8 (py_mini_racer).
// Цель: получить window.__fyModule.getFYToken({reqUrl}) / getUidToken().
// Значения fingerprint подобраны под реальный Chrome/Windows (тот же UA, что
// библиотека шлёт через requests) — чтобы токен был внутренне согласован.

var __now0 = 1784480000000;
var __t = __now0;

// ── таймеры (fireye использует setTimeout/setInterval для trace-сэмплинга) ──
var __timers = [];
function setTimeout(fn, ms) { __timers.push({fn: fn, at: __t + (ms||0)}); return __timers.length; }
function setInterval(fn, ms) { return 0; } // no-op: периодику не гоняем
function clearTimeout() {}
function clearInterval() {}
function requestAnimationFrame(fn) { return 0; }
function cancelAnimationFrame() {}

// ── навигатор ──
var navigator = {
  userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  appVersion: "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  appName: "Netscape",
  appCodeName: "Mozilla",
  platform: "Win32",
  product: "Gecko",
  productSub: "20030107",
  vendor: "Google Inc.",
  vendorSub: "",
  language: "ru-RU",
  languages: ["ru-RU", "ru", "en-US", "en"],
  onLine: true,
  cookieEnabled: true,
  doNotTrack: null,
  hardwareConcurrency: 8,
  deviceMemory: 8,
  maxTouchPoints: 0,
  webdriver: false,
  plugins: { length: 0 },
  mimeTypes: { length: 0 },
  javaEnabled: function () { return false; },
  userAgentData: null,
  connection: { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false },
  permissions: { query: function () { return Promise.resolve({ state: "granted" }); } },
  sendBeacon: function () { return true; },
  getBattery: function () { return Promise.resolve({ charging: true, level: 1, chargingTime: 0, dischargingTime: Infinity }); },
};

// ── экран ──
var screen = {
  width: 2560, height: 1440, availWidth: 2560, availHeight: 1400,
  colorDepth: 24, pixelDepth: 24, availLeft: 0, availTop: 0,
  orientation: { type: "landscape-primary", angle: 0 },
};

// ── location ──
var location = {
  href: "https://chat.qwen.ai/",
  protocol: "https:", host: "chat.qwen.ai", hostname: "chat.qwen.ai",
  port: "", pathname: "/", search: "", hash: "", origin: "https://chat.qwen.ai",
  assign: function () {}, replace: function () {}, reload: function () {}, toString: function () { return this.href; },
};

// ── history ──
var history = {
  length: 1, scrollRestoration: "auto", state: null,
  back: function () {}, forward: function () {}, go: function () {},
  pushState: function () {}, replaceState: function () {},
};

// ── localStorage / sessionStorage ──
function __mkStorage() {
  var m = {};
  return {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null; },
    setItem: function (k, v) { m[k] = String(v); },
    removeItem: function (k) { delete m[k]; },
    clear: function () { m = {}; },
    key: function (i) { return Object.keys(m)[i] || null; },
    get length() { return Object.keys(m).length; },
  };
}
var localStorage = __mkStorage();
var sessionStorage = __mkStorage();

// ── performance ──
var performance = {
  now: function () { return __t - __now0; },
  timeOrigin: __now0,
  timing: { navigationStart: __now0 },
  getEntriesByType: function () { return []; },
  getEntriesByName: function () { return []; },
  mark: function () {}, measure: function () {},
};

// ── canvas / WebGL (fingerprint) ──
function __canvas2d() {
  return {
    fillRect: function () {}, clearRect: function () {}, getImageData: function () { return { data: new Uint8Array(0) }; },
    putImageData: function () {}, createImageData: function () { return {}; }, setTransform: function () {},
    drawImage: function () {}, save: function () {}, fillText: function () {}, restore: function () {},
    beginPath: function () {}, moveTo: function () {}, lineTo: function () {}, closePath: function () {},
    stroke: function () {}, translate: function () {}, scale: function () {}, rotate: function () {},
    arc: function () {}, fill: function () {}, measureText: function () { return { width: 0 }; },
    isPointInPath: function () { return false; }, rect: function () {}, bezierCurveTo: function () {},
    createLinearGradient: function () { return { addColorStop: function () {} }; },
    createRadialGradient: function () { return { addColorStop: function () {} }; },
    fillStyle: "", strokeStyle: "", font: "", textBaseline: "", globalCompositeOperation: "",
    shadowColor: "", shadowBlur: 0,
  };
}
// Таблица значений WebGL getParameter — как у реального Chrome/Windows на ANGLE/Intel.
// Ключи — числовые enum'ы WebGL. Диапазоны отдаём как Float32Array (fireye читает [0]/[1]).
var __WEBGL_PARAMS = {
  7936: "WebKit",                                   // VENDOR
  7937: "WebKit WebGL",                             // RENDERER
  7938: "WebGL 1.0 (OpenGL ES 2.0 Chromium)",      // VERSION
  35724: "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)", // SHADING_LANGUAGE_VERSION
  37445: "Google Inc. (Intel)",                     // UNMASKED_VENDOR_WEBGL
  37446: "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)", // UNMASKED_RENDERER_WEBGL
  33901: new Float32Array([1, 1024]),               // ALIASED_POINT_SIZE_RANGE
  33902: new Float32Array([1, 1]),                  // ALIASED_LINE_WIDTH_RANGE
  3379: 16384,                                      // MAX_TEXTURE_SIZE
  34076: 16384,                                     // MAX_CUBE_MAP_TEXTURE_SIZE
  3386: new Int32Array([32767, 32767]),             // MAX_VIEWPORT_DIMS
  36349: 1024,                                      // MAX_FRAGMENT_UNIFORM_VECTORS
  36347: 4096,                                      // MAX_VERTEX_UNIFORM_VECTORS
  34921: 16,                                        // MAX_VERTEX_ATTRIBS
  34930: 16,                                        // MAX_TEXTURE_IMAGE_UNITS
  35660: 16,                                        // MAX_VERTEX_TEXTURE_IMAGE_UNITS
  35661: 32,                                        // MAX_COMBINED_TEXTURE_IMAGE_UNITS
  36348: 30,                                        // MAX_VARYING_VECTORS
  3410: 8, 3411: 8, 3412: 8, 3413: 8, 3414: 24, 3415: 0, // RED/GREEN/BLUE/ALPHA/DEPTH/STENCIL_BITS
  3408: 1,                                          // SUBPIXEL_BITS ~ (fallback)
  35657: 4,                                         // MAX_FRAGMENT_UNIFORM_COMPONENTS-ish
  34047: 16,                                        // MAX_TEXTURE_MAX_ANISOTROPY_EXT
  32883: 16384,                                     // MAX_3D_TEXTURE_SIZE (webgl2)
};
function __webgl() {
  var GL = {
    canvas: null, drawingBufferWidth: 300, drawingBufferHeight: 150,
    getExtension: function (name) {
      name = String(name || "");
      if (/debug_renderer_info/i.test(name)) return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      if (/debug_shaders/i.test(name)) return { getTranslatedShaderSource: function () { return ""; } };
      if (/texture_filter_anisotropic/i.test(name)) return { MAX_TEXTURE_MAX_ANISOTROPY_EXT: 34047, TEXTURE_MAX_ANISOTROPY_EXT: 34046 };
      // отдаём непустой объект для остальных, чтобы наличие расширения фингерпринтилось
      return {};
    },
    getParameter: function (p) {
      if (Object.prototype.hasOwnProperty.call(__WEBGL_PARAMS, p)) return __WEBGL_PARAMS[p];
      return 0;
    },
    getSupportedExtensions: function () {
      return ["ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
        "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth", "EXT_shader_texture_lod",
        "EXT_texture_compression_bptc", "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
        "EXT_sRGB", "OES_element_index_uint", "OES_fbo_render_mipmap", "OES_standard_derivatives",
        "OES_texture_float", "OES_texture_float_linear", "OES_texture_half_float",
        "OES_texture_half_float_linear", "OES_vertex_array_object", "WEBGL_color_buffer_float",
        "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info",
        "WEBGL_debug_shaders", "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_lose_context",
        "WEBGL_multi_draw"];
    },
    getShaderPrecisionFormat: function () { return { rangeMin: 127, rangeMax: 127, precision: 23 }; },
    getContextAttributes: function () { return { alpha: true, antialias: true, depth: true, desynchronized: false, failIfMajorPerformanceCaveat: false, powerPreference: "default", premultipliedAlpha: true, preserveDrawingBuffer: false, stencil: false, xrCompatible: false }; },
    createBuffer: function () { return {}; }, bindBuffer: function () {}, bufferData: function () {},
    createProgram: function () { return {}; }, createShader: function () { return {}; },
    shaderSource: function () {}, compileShader: function () {}, attachShader: function () {},
    linkProgram: function () {}, useProgram: function () {}, getAttribLocation: function () { return 0; },
    getProgramParameter: function () { return true; }, getShaderParameter: function () { return true; },
    enableVertexAttribArray: function () {}, vertexAttribPointer: function () {},
    getUniformLocation: function () { return {}; }, uniform2f: function () {}, uniform4f: function () {},
    drawArrays: function () {}, drawElements: function () {}, enable: function () {}, disable: function () {},
    depthFunc: function () {}, blendFunc: function () {}, texParameteri: function () {},
    createTexture: function () { return {}; }, bindTexture: function () {}, texImage2D: function () {},
    createFramebuffer: function () { return {}; }, bindFramebuffer: function () {}, framebufferTexture2D: function () {},
    activeTexture: function () {}, generateMipmap: function () {}, pixelStorei: function () {},
    viewport: function () {}, clearColor: function () {}, clearDepth: function () {}, clear: function () {},
    readPixels: function () {}, finish: function () {}, flush: function () {}, isEnabled: function () { return false; },
    checkFramebufferStatus: function () { return 36053; }, deleteBuffer: function () {}, deleteTexture: function () {},
    VERTEX_SHADER: 35633, FRAGMENT_SHADER: 35632, ARRAY_BUFFER: 34962, ELEMENT_ARRAY_BUFFER: 34963,
    STATIC_DRAW: 35044, COLOR_BUFFER_BIT: 16384, DEPTH_BUFFER_BIT: 256, FLOAT: 5126, TRIANGLES: 4,
    UNSIGNED_BYTE: 5121, UNSIGNED_SHORT: 5123, RGBA: 6408, TEXTURE_2D: 3553, COMPILE_STATUS: 35713,
    LINK_STATUS: 35714, FRAMEBUFFER: 36160, COLOR_ATTACHMENT0: 36064, FRAMEBUFFER_COMPLETE: 36053,
  };
  return GL;
}
function __createElement(tag) {
  tag = (tag || "").toLowerCase();
  var el = {
    tagName: tag.toUpperCase(), nodeName: tag.toUpperCase(), nodeType: 1,
    style: {}, attributes: {}, childNodes: [], children: [],
    parentNode: null, ownerDocument: null, id: "", className: "",
    setAttribute: function (k, v) { this.attributes[k] = v; },
    getAttribute: function (k) { return this.attributes[k] != null ? this.attributes[k] : null; },
    removeAttribute: function (k) { delete this.attributes[k]; },
    hasAttribute: function (k) { return this.attributes[k] != null; },
    appendChild: function (c) { if (c) c.parentNode = this; this.childNodes.push(c); return c; },
    removeChild: function (c) {
      var i = this.childNodes.indexOf(c);
      if (i > -1) this.childNodes.splice(i, 1);
      if (c) c.parentNode = null;
      return c;
    },
    insertBefore: function (c, ref) { if (c) c.parentNode = this; this.childNodes.push(c); return c; },
    replaceChild: function (n, o) { return o; },
    cloneNode: function () { return __createElement(tag); },
    remove: function () { if (this.parentNode) this.parentNode.removeChild(this); },
    contains: function () { return false; },
    addEventListener: function () {}, removeEventListener: function () {}, dispatchEvent: function () { return true; },
    getContext: function (type) {
      if (type === "2d") return __canvas2d();
      if (type === "webgl" || type === "experimental-webgl" || type === "webgl2") return __webgl();
      return null;
    },
    toDataURL: function () { return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"; },
    getBoundingClientRect: function () { return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; },
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    getElementsByTagName: function () { return []; },
    focus: function () {}, blur: function () {}, click: function () {},
    width: 0, height: 0, offsetWidth: 0, offsetHeight: 0, clientWidth: 0, clientHeight: 0,
    innerHTML: "", outerHTML: "", innerText: "", textContent: "", value: "",
  };
  // media-элементы (audio/video) — fireye фингерпринтит кодеки через canPlayType
  if (tag === "audio" || tag === "video") {
    el.canPlayType = function (type) {
      type = String(type || "");
      // значения как у реального Chrome/Windows
      if (/mp4|mpeg|m4a|aac|avc1|mp4a/i.test(type)) return "probably";
      if (/ogg|vorbis|theora|opus|webm|vp8|vp9|wav|x-wav/i.test(type)) return "probably";
      if (type && type.indexOf("codecs") === -1) return "maybe";
      return "";
    };
    el.load = function () {}; el.play = function () { return Promise.resolve(); };
    el.pause = function () {}; el.addTextTrack = function () { return {}; };
    el.currentTime = 0; el.duration = NaN; el.paused = true; el.volume = 1;
  }
  return el;
}

// ── document ──
var __head = __createElement("head");
var __body = __createElement("body");
var __htmlEl = __createElement("html");
__htmlEl.appendChild(__head);
__htmlEl.appendChild(__body);
// Настоящий cookie-jar на document.cookie: анти-бот скрипты (sufei_data.js —
// isg, et_f.js — tfstk/ssxmod_itna и т.п.) ставят risk-control cookies через
// `document.cookie = "name=value; domain=...; ..."`. Нужно накапливать их
// по имени (как в реальном браузере), а не просто держать одну строку —
// иначе повторные присваивания стирают предыдущие cookie.
var __cookieJar = {};
var document = {
  get cookie() {
    return Object.keys(__cookieJar).map(function (k) { return k + "=" + __cookieJar[k]; }).join("; ");
  },
  set cookie(v) {
    var pair = String(v).split(";")[0];
    var eq = pair.indexOf("=");
    if (eq > 0) __cookieJar[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
  },
  referrer: "",
  title: "Qwen",
  readyState: "complete",
  documentElement: __htmlEl,
  location: location,
  characterSet: "UTF-8", charset: "UTF-8", compatMode: "CSS1Compat",
  visibilityState: "visible", hidden: false,
  createElement: __createElement,
  createElementNS: function (ns, tag) { return __createElement(tag); },
  createTextNode: function (t) { return { nodeType: 3, textContent: t, data: t }; },
  createDocumentFragment: function () { return __createElement("#fragment"); },
  getElementsByTagName: function (t) {
    t = (t || "").toLowerCase();
    if (t === "head") return [__head];
    if (t === "body") return [__body];
    if (t === "html") return [__htmlEl];
    if (t === "script") return [__createElement("script")];
    return [];
  },
  getElementsByClassName: function () { return []; },
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {}, removeEventListener: function () {},
  attachEvent: function () {}, detachEvent: function () {},
  head: __head,
  body: __body,
  createEvent: function () { return { initEvent: function () {} }; },
  dispatchEvent: function () { return true; },
  hasFocus: function () { return true; },
};
__head.ownerDocument = document;
__body.ownerDocument = document;

// ── window / self / globalThis связываем на глобал ──
var window = this;
window.navigator = navigator;
window.screen = screen;
window.location = location;
window.document = document;
window.localStorage = localStorage;
window.sessionStorage = sessionStorage;
window.performance = performance;
window.setTimeout = setTimeout;
window.setInterval = setInterval;
window.clearTimeout = clearTimeout;
window.clearInterval = clearInterval;
window.requestAnimationFrame = requestAnimationFrame;
window.cancelAnimationFrame = cancelAnimationFrame;
window.addEventListener = function () {};
window.removeEventListener = function () {};
window.attachEvent = function () {};
window.top = window;
window.self = window;
window.parent = window;
window.frames = window;
window.closed = false;
window.innerWidth = 2560;
window.innerHeight = 1329;
window.outerWidth = 2560;
window.outerHeight = 1440;
window.screenX = 0; window.screenY = 0; window.pageXOffset = 0; window.pageYOffset = 0;
window.devicePixelRatio = 1;
window.name = "";
window.history = history;
window.origin = "https://chat.qwen.ai";
window.isSecureContext = true;
window.chrome = { runtime: {}, app: { isInstalled: false }, csi: function () { return {}; }, loadTimes: function () { return {}; } };
window.external = { AddSearchProvider: function () {}, IsSearchProviderInstalled: function () {} };
var self = window;

// Image (baxia-entry использует new Image().src для маячков — no-op)
function Image() { return { src: "" }; }
window.Image = Image;

// XMLHttpRequest — no-op заглушка (fireye trace-репорты нам не нужны)
function XMLHttpRequest() {
  return {
    open: function () {}, send: function () {}, setRequestHeader: function () {},
    addEventListener: function () {}, abort: function () {},
    readyState: 0, status: 0, responseText: "", withCredentials: false,
  };
}
window.XMLHttpRequest = XMLHttpRequest;
window.fetch = function () { return Promise.resolve({ text: function () { return Promise.resolve(""); }, json: function () { return Promise.resolve({}); } }); };

// btoa/atob (V8 их не имеет по умолчанию)
var __b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function btoa(s) {
  var out = "", i = 0;
  s = String(s);
  while (i < s.length) {
    var c1 = s.charCodeAt(i++), c2 = s.charCodeAt(i++), c3 = s.charCodeAt(i++);
    var e1 = c1 >> 2, e2 = ((c1 & 3) << 4) | (c2 >> 4), e3 = ((c2 & 15) << 2) | (c3 >> 6), e4 = c3 & 63;
    if (isNaN(c2)) e3 = e4 = 64; else if (isNaN(c3)) e4 = 64;
    out += __b64.charAt(e1) + __b64.charAt(e2) + __b64.charAt(e3) + __b64.charAt(e4);
  }
  return out;
}
function atob(s) {
  s = String(s).replace(/[^A-Za-z0-9+/=]/g, "");
  var out = "", i = 0;
  while (i < s.length) {
    var e1 = __b64.indexOf(s.charAt(i++)), e2 = __b64.indexOf(s.charAt(i++));
    var e3 = __b64.indexOf(s.charAt(i++)), e4 = __b64.indexOf(s.charAt(i++));
    var c1 = (e1 << 2) | (e2 >> 4), c2 = ((e2 & 15) << 4) | (e3 >> 2), c3 = ((e3 & 3) << 6) | e4;
    out += String.fromCharCode(c1);
    if (e3 !== 64) out += String.fromCharCode(c2);
    if (e4 !== 64) out += String.fromCharCode(c3);
  }
  return out;
}
window.btoa = btoa; window.atob = atob;

// console
var console = { log: function () {}, warn: function () {}, error: function () {}, info: function () {}, debug: function () {} };
window.console = console;

// postMessage / messaging (fireye шлёт сам себе postMessage для async-трейсинга)
window.postMessage = function () {};
window.onmessage = null;
function MessagePort() { return { postMessage: function () {}, start: function () {}, close: function () {}, addEventListener: function () {}, onmessage: null }; }
function MessageChannel() { this.port1 = MessagePort(); this.port2 = MessagePort(); }
window.MessageChannel = MessageChannel;
window.MessagePort = MessagePort;
function Worker() { return { postMessage: function () {}, terminate: function () {}, addEventListener: function () {}, onmessage: null, onerror: null }; }
window.Worker = Worker;
window.dispatchEvent = function () { return true; };
function CustomEvent(t, o) { return { type: t, detail: o && o.detail }; }
window.CustomEvent = CustomEvent;
function Event(t) { return { type: t }; }
window.Event = Event;
// crypto (fireye может использовать getRandomValues)
window.crypto = window.crypto || {
  getRandomValues: function (arr) { for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256); return arr; },
};

// matchMedia (fireye фингерпринтит display-mode/prefers-* через .matches)
function matchMedia(q) {
  q = String(q || "");
  var m = false;
  if (/display-mode:\s*browser/i.test(q)) m = true;
  else if (/display-mode:/i.test(q)) m = false; // standalone/fullscreen/minimal-ui
  else if (/prefers-color-scheme:\s*light/i.test(q)) m = true;
  else if (/prefers-color-scheme:\s*dark/i.test(q)) m = false;
  else if (/prefers-reduced-motion:\s*no-preference/i.test(q)) m = true;
  else if (/prefers-reduced-motion:\s*reduce/i.test(q)) m = false;
  else if (/pointer:\s*fine/i.test(q)) m = true;
  else if (/pointer:\s*coarse/i.test(q)) m = false;
  else if (/hover:\s*hover/i.test(q)) m = true;
  else if (/any-pointer:\s*fine/i.test(q)) m = true;
  return {
    matches: m, media: q, onchange: null,
    addListener: function () {}, removeListener: function () {},
    addEventListener: function () {}, removeEventListener: function () {}, dispatchEvent: function () { return false; },
  };
}
window.matchMedia = matchMedia;

// getComputedStyle (иногда используется для fingerprint)
function getComputedStyle() {
  return { getPropertyValue: function () { return ""; }, length: 0 };
}
window.getComputedStyle = getComputedStyle;

// ── анти-tamper: fireye захватывает нативные аксессоры через
// Object.getOwnPropertyDescriptor(proto, prop).get и вызывает .get.call(obj),
// чтобы прочитать значение в обход возможной подмены. В чистом V8 host-объекты —
// плоские data-property (без .get). Патчим gopd так, чтобы:
//   1) для nullish target вернуть аксессор (fireye берёт head/body/document
//      с "прототипа", которого у нас нет — undefined);
//   2) у любой data-property синтезировать .get, возвращающий значение.
// Без этого getFYToken падает на "qB[wB] is not a function" (wB='get').
(function () {
  var orig = Object.getOwnPropertyDescriptor;
  Object.getOwnPropertyDescriptor = function (o, p) {
    if (o === null || o === undefined) {
      var val = (p === "head") ? document.head
        : (p === "body") ? document.body
        : (p === "document") ? document
        : undefined;
      return { get: function () { return val; }, set: undefined, enumerable: true, configurable: true };
    }
    var d = orig(o, p);
    if (d && !("get" in d) && !("set" in d) && ("value" in d)) {
      var v = d.value;
      return { get: function () { return v; }, set: function (n) { v = n; }, enumerable: d.enumerable, configurable: d.configurable };
    }
    return d;
  };
})();

// bx-umidtoken (getUidToken) читается fireye из localStorage-ключа вида
// "<random>": "<umid>@@<ts>". Компонент umid считается асинхронно и в чистом
// V8 не воспроизводится, поэтому даём хук: если хост-код положит готовый umid
// (снятый один раз из реального браузера, он device-стабилен), getUidToken
// вернёт его. Реализуется в bx_solver.py через seed_umid().
window.__seedUmid = function (umid) { window.__UMID = umid || ""; };

// Событийные хуки для скриптов, которые ждут load/DOMContentLoaded, чтобы
// запустить risk-cookie логику (sufei_data.js/et_f.js). bx_solver.py вызывает
// __fireLoadEvents() после загрузки всех SDK-скриптов.
window.__eventHandlers = { window: {}, document: {} };
(function () {
  var origAddWin = window.addEventListener;
  window.addEventListener = function (t, f) { (window.__eventHandlers.window[t] = window.__eventHandlers.window[t] || []).push(f); };
  document.addEventListener = function (t, f) { (window.__eventHandlers.document[t] = window.__eventHandlers.document[t] || []).push(f); };
})();
window.__fireLoadEvents = function () {
  document.readyState = "complete";
  ["readystatechange", "DOMContentLoaded"].forEach(function (t) {
    (window.__eventHandlers.document[t] || []).forEach(function (f) { try { f({ type: t }); } catch (e) {} });
  });
  ["load", "readystatechange"].forEach(function (t) {
    (window.__eventHandlers.window[t] || []).forEach(function (f) { try { f({ type: t }); } catch (e) {} });
  });
  if (typeof window.onload === "function") { try { window.onload({}); } catch (e) {} }
};

// document.cookie как JSON-словарь — для передачи risk-control cookies (isg
// и т.п., которые анти-бот SDK ставит через document.cookie) наружу в Python.
window.__getCookieJarJSON = function () { return JSON.stringify(__cookieJar); };
