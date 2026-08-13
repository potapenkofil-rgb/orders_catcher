// Браузерный shim для решения AWS WAF challenge внутри py_mini_racer (V8 без
// Node.js). В отличие от js/waf_token_solver.js (который гоняет challenge.js
// в изолированном Node vm.createContext с реальным event loop и реальными
// https/http модулями), здесь НЕТ доступа ни к какому I/O напрямую —
// py_mini_racer это однонаправленный мост (Python -> JS), JS не может
// вызвать Python-функцию напрямую.
//
// Поэтому fetch/XMLHttpRequest не делают запрос сами, а кладут его в очередь
// (__pending) и возвращают Promise/используют колбэки. Python-драйвер
// (waf_solver.py) в цикле:
//   1. вызывает window.__runTimers() — прогоняет накопившиеся setTimeout/setInterval
//   2. вызывает window.__drainPending() — забирает новые запросы из очереди
//   3. реально выполняет их через requests
//   4. вызывает window.__resolvePending(id, ...)/__rejectPending(id, ...),
//      что резолвит Promise внутри V8 и запускает дальнейшие .then()-цепочки
//      (в том числе новые fetch/XHR, которые попадут в __pending на следующей
//      итерации)
//   5. проверяет window.__capturedToken
//
// gokuProps/cookieDomains/challengeJsUrl подставляются Python-ом в начало
// файла как обычные присваивания (см. waf_solver.py) перед evalом
// этого шима, а сам challenge.js — после шима.

(function () {
    'use strict';

    // globalThis всегда есть в V8; window/self в браузере — просто алиасы
    // на тот же глобальный объект. setG пишет напрямую в globalThis, а не
    // в переменную IIFE — иначе challenge.js (выполняемый ПОСЛЕ этого файла
    // отдельным вызовом mr.eval()) их не увидит.
    function setG(name, value) { globalThis[name] = value; }
    setG('window', globalThis);
    setG('self', globalThis);

    // ── base64 (V8 без Node Buffer/btoa/atob — только чистый JS) ──────────
    var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    function bytesToBase64(bytes) {
        var out = '';
        for (var i = 0; i < bytes.length; i += 3) {
            var b1 = bytes[i], b2 = bytes[i + 1], b3 = bytes[i + 2];
            var has2 = b2 !== undefined, has3 = b3 !== undefined;
            var triplet = (b1 << 16) | ((has2 ? b2 : 0) << 8) | (has3 ? b3 : 0);
            out += B64[(triplet >> 18) & 0x3F];
            out += B64[(triplet >> 12) & 0x3F];
            out += has2 ? B64[(triplet >> 6) & 0x3F] : '=';
            out += has3 ? B64[triplet & 0x3F] : '=';
        }
        return out;
    }

    function base64ToBytes(b64) {
        b64 = String(b64).replace(/[^A-Za-z0-9+/]/g, '');
        var bytes = [];
        var buffer = 0, bits = 0;
        for (var i = 0; i < b64.length; i++) {
            var c = B64.indexOf(b64[i]);
            if (c === -1) continue;
            buffer = (buffer << 6) | c;
            bits += 6;
            if (bits >= 8) {
                bits -= 8;
                bytes.push((buffer >> bits) & 0xFF);
            }
        }
        return bytes;
    }

    function utf8ToBytes(str) {
        var bytes = [];
        for (var i = 0; i < str.length; i++) {
            var code = str.charCodeAt(i);
            if (code < 0x80) {
                bytes.push(code);
            } else if (code < 0x800) {
                bytes.push(0xC0 | (code >> 6), 0x80 | (code & 0x3F));
            } else if (code >= 0xD800 && code <= 0xDBFF && i + 1 < str.length) {
                var lo = str.charCodeAt(i + 1);
                var cp = 0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00);
                i++;
                bytes.push(0xF0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3F), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F));
            } else {
                bytes.push(0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F));
            }
        }
        return bytes;
    }

    function bytesToUtf8(bytes) {
        var out = '', i = 0;
        while (i < bytes.length) {
            var b1 = bytes[i++];
            if (b1 < 0x80) { out += String.fromCharCode(b1); continue; }
            if (b1 >> 5 === 0x6) {
                var b2 = bytes[i++];
                out += String.fromCharCode(((b1 & 0x1F) << 6) | (b2 & 0x3F));
            } else if (b1 >> 4 === 0xE) {
                var b2 = bytes[i++], b3 = bytes[i++];
                out += String.fromCharCode(((b1 & 0xF) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F));
            } else if (b1 >> 3 === 0x1E) {
                var b2 = bytes[i++], b3 = bytes[i++], b4 = bytes[i++];
                var cp = ((b1 & 0x7) << 18) | ((b2 & 0x3F) << 12) | ((b3 & 0x3F) << 6) | (b4 & 0x3F);
                cp -= 0x10000;
                out += String.fromCharCode(0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF));
            } else {
                out += String.fromCharCode(b1);
            }
        }
        return out;
    }

    setG('btoa', function (s) { return bytesToBase64(utf8ToBytes(String(s))); });
    setG('atob', function (s) { return bytesToUtf8(base64ToBytes(s)); });

    function toByteArray(x) {
        if (x === null || x === undefined) return null;
        if (typeof x === 'string') return utf8ToBytes(x);
        if (x instanceof Uint8Array) return Array.prototype.slice.call(x);
        if (x instanceof ArrayBuffer) return Array.prototype.slice.call(new Uint8Array(x));
        if (Array.isArray(x)) return x;
        if (x && x._bytes) return x._bytes; // наш Blob/File
        return utf8ToBytes(String(x));
    }

    function isFormData(x) { return x && Array.isArray(x._d); }

    function serializeFormData(fd) {
        var boundary = 'WebKitFormBoundary' + Math.random().toString(36).slice(2) + Date.now().toString(36);
        var bytes = [];
        fd._d.forEach(function (pair) {
            var key = pair[0], value = pair[1];
            bytes = bytes.concat(utf8ToBytes('--' + boundary + '\r\n'));
            if (value && value._bytes) {
                var filename = value.name || 'blob';
                var type = value.type || 'application/octet-stream';
                bytes = bytes.concat(utf8ToBytes('Content-Disposition: form-data; name="' + key + '"; filename="' + filename + '"\r\nContent-Type: ' + type + '\r\n\r\n'));
                bytes = bytes.concat(value._bytes);
            } else {
                bytes = bytes.concat(utf8ToBytes('Content-Disposition: form-data; name="' + key + '"\r\n\r\n'));
                bytes = bytes.concat(utf8ToBytes(String(value)));
            }
            bytes = bytes.concat(utf8ToBytes('\r\n'));
        });
        bytes = bytes.concat(utf8ToBytes('--' + boundary + '--\r\n'));
        return { bytes: bytes, contentType: 'multipart/form-data; boundary=' + boundary };
    }

    function bodyToBytesWithHeaders(body, headers) {
        if (isFormData(body)) {
            var mp = serializeFormData(body);
            if (!headers['Content-Type'] && !headers['content-type']) headers['Content-Type'] = mp.contentType;
            return mp.bytes;
        }
        return toByteArray(body);
    }

    // ── очередь запросов, которую разбирает Python ─────────────────────────
    var __pending = {};
    var __nextId = 1;
    var __timers = [];
    var __nextTimerId = 1;

    setG('__capturedToken', null);

    // ── URL/URLSearchParams — есть в Node нативно, но не в чистом V8 ──────
    var URLSearchParams = function (init) {
        this._entries = [];
        var self = this;
        if (typeof init === 'string') {
            var s = init.charAt(0) === '?' ? init.slice(1) : init;
            if (s) {
                s.split('&').forEach(function (pair) {
                    if (!pair) return;
                    var idx = pair.indexOf('=');
                    var k = idx === -1 ? pair : pair.slice(0, idx);
                    var v = idx === -1 ? '' : pair.slice(idx + 1);
                    self._entries.push([decodeURIComponent(k.replace(/\+/g, ' ')), decodeURIComponent(v.replace(/\+/g, ' '))]);
                });
            }
        }
    };
    URLSearchParams.prototype.get = function (k) { var e = this._entries.filter(function (p) { return p[0] === k; })[0]; return e ? e[1] : null; };
    URLSearchParams.prototype.getAll = function (k) { return this._entries.filter(function (p) { return p[0] === k; }).map(function (p) { return p[1]; }); };
    URLSearchParams.prototype.set = function (k, v) {
        var found = false;
        this._entries = this._entries.filter(function (p) {
            if (p[0] === k) { if (!found) { p[1] = v; found = true; return true; } return false; }
            return true;
        });
        if (!found) this._entries.push([k, v]);
    };
    URLSearchParams.prototype.append = function (k, v) { this._entries.push([k, v]); };
    URLSearchParams.prototype.has = function (k) { return this._entries.some(function (p) { return p[0] === k; }); };
    URLSearchParams.prototype.delete = function (k) { this._entries = this._entries.filter(function (p) { return p[0] !== k; }); };
    URLSearchParams.prototype.forEach = function (cb) { this._entries.forEach(function (p) { cb(p[1], p[0]); }); };
    URLSearchParams.prototype.toString = function () {
        return this._entries.map(function (p) { return encodeURIComponent(p[0]) + '=' + encodeURIComponent(p[1]); }).join('&');
    };
    setG('URLSearchParams', URLSearchParams);

    var URL = function (url, base) {
        url = String(url);
        if (base && !/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url)) {
            var b = typeof base === 'string' ? new URL(base) : base;
            if (url.charAt(0) === '/') {
                url = b.protocol + '//' + b.host + url;
            } else {
                url = b.protocol + '//' + b.host + b.pathname.replace(/[^/]*$/, '') + url;
            }
        }
        var m = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$/.exec(url);
        if (!m) throw new TypeError('Invalid URL: ' + url);
        this.protocol = m[1] + ':';
        var hostport = m[2];
        var hm = /^([^:]+)(?::(\d+))?$/.exec(hostport);
        this.hostname = hm ? hm[1] : hostport;
        this.port = (hm && hm[2]) || '';
        this.host = hostport;
        this.pathname = m[3] || '/';
        this.search = m[4] || '';
        this.hash = m[5] || '';
        this.origin = this.protocol + '//' + this.host;
        this.href = url;
        this.searchParams = new URLSearchParams(this.search);
    };
    URL.prototype.toString = function () { return this.href; };
    setG('URL', URL);

    setG('__drainPending', function () {
        var out = [];
        for (var id in __pending) {
            var e = __pending[id];
            if (!e.sent) {
                e.sent = true;
                out.push({
                    id: Number(id), method: e.method, url: e.url,
                    headers: e.headers, bodyBase64: e.bodyBase64,
                });
            }
        }
        return JSON.stringify(out);
    });

    // py_mini_racer .call() требует JSON-сериализуемый возврат — не undefined,
    // поэтому везде явный `return true`.
    setG('__resolvePending', function (id, status, headersJson, bodyBase64) {
        var e = __pending[id];
        if (!e) return false;
        delete __pending[id];
        var headers = JSON.parse(headersJson || '{}');
        e.settle({ status: status, headers: headers, bodyBase64: bodyBase64 || '' });
        return true;
    });

    setG('__rejectPending', function (id, message) {
        var e = __pending[id];
        if (!e) return false;
        delete __pending[id];
        e.fail(new Error(message));
        return true;
    });

    setG('__runTimers', function () {
        var due = __timers;
        __timers = [];
        for (var i = 0; i < due.length; i++) {
            try { due[i](); } catch (e) { /* проглатываем — таймер сам разберётся через try/catch внутри challenge.js */ }
        }
        return __timers.length; // сколько НОВЫХ таймеров успело добавиться за это время
    });

    function enqueue(method, url, headers, bodyBytes) {
        var id = __nextId++;
        var bodyBase64 = bodyBytes ? bytesToBase64(bodyBytes) : null;
        var entry = { method: method, url: url, headers: headers || {}, bodyBase64: bodyBase64, sent: false };
        __pending[id] = entry;
        return { id: id, entry: entry };
    }

    // ── fetch ────────────────────────────────────────────────────────────
    setG('fetch', function (url, opts) {
        opts = opts || {};
        var method = (opts.method || 'GET').toUpperCase();
        var headers = {};
        if (opts.headers) {
            if (typeof opts.headers.forEach === 'function' && typeof opts.headers.entries !== 'undefined') {
                opts.headers.forEach(function (v, k) { headers[k] = v; });
            } else {
                for (var k in opts.headers) headers[k] = opts.headers[k];
            }
        }
        var bodyBytes = bodyToBytesWithHeaders(opts.body, headers);
        var q = enqueue(method, url, headers, bodyBytes);
        return new Promise(function (resolve, reject) {
            q.entry.settle = function (r) {
                var respHeaders = r.headers || {};
                resolve({
                    ok: r.status >= 200 && r.status < 300,
                    status: r.status,
                    url: url,
                    headers: {
                        get: function (k) { return respHeaders[k.toLowerCase()] || null; },
                        has: function (k) { return k.toLowerCase() in respHeaders; },
                        forEach: function (cb) { for (var k in respHeaders) cb(respHeaders[k], k); },
                        entries: function () {
                            var arr = [];
                            for (var k in respHeaders) arr.push([k, respHeaders[k]]);
                            var i = 0;
                            return { next: function () { return i < arr.length ? { value: arr[i++], done: false } : { done: true }; } };
                        },
                    },
                    text: function () { return Promise.resolve(bytesToUtf8(base64ToBytes(r.bodyBase64))); },
                    json: function () { return Promise.resolve(JSON.parse(bytesToUtf8(base64ToBytes(r.bodyBase64)))); },
                    arrayBuffer: function () {
                        var bytes = base64ToBytes(r.bodyBase64);
                        var buf = new ArrayBuffer(bytes.length);
                        var view = new Uint8Array(buf);
                        for (var i = 0; i < bytes.length; i++) view[i] = bytes[i];
                        return Promise.resolve(buf);
                    },
                    clone: function () { return this; },
                });
            };
            q.entry.fail = reject;
        });
    });

    // ── XMLHttpRequest ──────────────────────────────────────────────────
    var XMLHttpRequest = function () {
        this.readyState = 0; this.status = 0;
        this.responseText = ''; this.response = '';
        this.withCredentials = false;
        this._headers = {};
        this._resHeaders = {};
    };
    setG('XMLHttpRequest', XMLHttpRequest);
    XMLHttpRequest.prototype.open = function (method, url) { this._method = method.toUpperCase(); this._url = url; };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) { this._headers[k] = v; };
    XMLHttpRequest.prototype.getAllResponseHeaders = function () {
        var out = [];
        for (var k in this._resHeaders) out.push(k + ': ' + this._resHeaders[k]);
        return out.join('\r\n');
    };
    XMLHttpRequest.prototype.getResponseHeader = function (k) { return this._resHeaders[k.toLowerCase()] || null; };
    XMLHttpRequest.prototype.send = function (body) {
        var self = this;
        var bodyBytes = bodyToBytesWithHeaders(body, this._headers);
        var q = enqueue(this._method || 'GET', this._url, this._headers, bodyBytes);
        q.entry.settle = function (r) {
            self._resHeaders = r.headers || {};
            self.status = r.status;
            self.readyState = 4;
            var text = bytesToUtf8(base64ToBytes(r.bodyBase64));
            self.responseText = text;
            self.response = text;
            if (typeof self.onreadystatechange === 'function') self.onreadystatechange();
            if (typeof self.onload === 'function') self.onload({ target: self });
        };
        q.entry.fail = function (err) {
            self.status = 0; self.readyState = 4;
            if (typeof self.onerror === 'function') self.onerror(err);
        };
    };
    XMLHttpRequest.prototype.addEventListener = function (evt, fn) {
        if (evt === 'load') this.onload = fn;
        if (evt === 'error') this.onerror = fn;
        if (evt === 'readystatechange') this.onreadystatechange = fn;
    };
    XMLHttpRequest.prototype.removeEventListener = function () {};
    XMLHttpRequest.prototype.abort = function () {};

    // ── таймеры (без реального event loop — просто очередь, которую Python
    //    прогоняет между итерациями; задержки не соблюдаются) ─────────────
    setG('setTimeout', function (fn) { var id = __nextTimerId++; __timers.push(fn); return id; });
    setG('clearTimeout', function () {});
    setG('setInterval', function (fn) { var id = __nextTimerId++; __timers.push(fn); return id; });
    setG('clearInterval', function () {});
    setG('queueMicrotask', function (fn) { Promise.resolve().then(fn); });
    setG('requestAnimationFrame', function (cb) { __timers.push(function () { cb(Date.now()); }); return __nextTimerId++; });
    setG('cancelAnimationFrame', function () {});
    setG('requestIdleCallback', function (cb) { __timers.push(function () { cb({ timeRemaining: function () { return 50; } }); }); return __nextTimerId++; });
    setG('cancelIdleCallback', function () {});

    // ── document.cookie перехват токена ────────────────────────────────
    var cookieStore = '';
    function setCookie(val) {
        var pair = String(val).split(';')[0];
        var eqIdx = pair.indexOf('=');
        if (eqIdx < 0) return;
        var k = pair.slice(0, eqIdx).trim();
        var v = pair.slice(eqIdx + 1).trim();
        var re = new RegExp('(?:^|;\\s*)' + k + '=[^;]*');
        cookieStore = cookieStore.replace(re, '').replace(/^;\s*/, '');
        cookieStore += (cookieStore ? '; ' : '') + k + '=' + v;
        if (k === 'aws-waf-token' && v && v.length > 10) {
            __capturedToken = 'aws-waf-token=' + v;
        }
    }

    // ── DOM shims (в основном как в js/waf_token_solver.js) ────────────────
    var docScripts = [
        { src: '', innerHTML: 'window.gokuProps', text: 'window.gokuProps', type: 'text/javascript', async: false },
        { src: (typeof __challengeJsUrl !== 'undefined' ? __challengeJsUrl : 'challenge.js'), innerHTML: 'AwsWafIntegration', text: 'AwsWafIntegration', type: 'text/javascript', async: true },
    ];
    var currentScriptEl = docScripts[1];

    var bodyEl = {
        tagName: 'BODY', nodeName: 'BODY', style: {}, className: '', id: '',
        appendChild: function () {}, removeChild: function () {}, insertBefore: function () {},
        classList: { add: function () {}, remove: function () {}, contains: function () { return false; }, toggle: function () {} },
        setAttribute: function () {}, getAttribute: function () { return null; }, hasAttribute: function () { return false; },
        addEventListener: function () {}, removeEventListener: function () {},
        childNodes: [], children: [], childElementCount: 0,
        innerHTML: '', textContent: '', innerText: '',
        getBoundingClientRect: function () { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
        offsetWidth: 1920, offsetHeight: 1080,
    };

    function BlobShim(parts, opts) {
        opts = opts || {};
        var bytes = [];
        (parts || []).forEach(function (p) {
            var b = toByteArray(p);
            if (b) bytes = bytes.concat(b);
        });
        this._bytes = bytes;
        this.size = bytes.length;
        this.type = opts.type || '';
    }
    BlobShim.prototype.text = function () { return Promise.resolve(bytesToUtf8(this._bytes)); };
    BlobShim.prototype.arrayBuffer = function () {
        var buf = new ArrayBuffer(this._bytes.length);
        var view = new Uint8Array(buf);
        for (var i = 0; i < this._bytes.length; i++) view[i] = this._bytes[i];
        return Promise.resolve(buf);
    };
    BlobShim.prototype.slice = function () { return new BlobShim([]); };
    setG('Blob', BlobShim);
    var File = function (parts, name, opts) { BlobShim.call(this, parts, opts); this.name = name; this.lastModified = Date.now(); };
    File.prototype = Object.create(BlobShim.prototype);
    setG('File', File);

    var FileReader = function () { this.result = null; };
    FileReader.prototype.readAsDataURL = function (blob) {
        var self = this;
        __timers.push(function () {
            var mime = (blob && blob.type) || 'application/octet-stream';
            self.result = 'data:' + mime + ';base64,' + bytesToBase64(blob._bytes || []);
            if (self.onload) self.onload({ target: self });
        });
    };
    FileReader.prototype.readAsText = function (blob) {
        var self = this;
        __timers.push(function () {
            self.result = bytesToUtf8(blob._bytes || []);
            if (self.onload) self.onload({ target: self });
        });
    };
    FileReader.prototype.readAsArrayBuffer = function (blob) {
        var self = this;
        __timers.push(function () {
            var bytes = blob._bytes || [];
            var buf = new ArrayBuffer(bytes.length);
            var view = new Uint8Array(buf);
            for (var i = 0; i < bytes.length; i++) view[i] = bytes[i];
            self.result = buf;
            if (self.onload) self.onload({ target: self });
        });
    };
    FileReader.prototype.addEventListener = function (e, fn) { if (e === 'load') this.onload = fn; if (e === 'error') this.onerror = fn; };
    setG('FileReader', FileReader);

    function makeEl(tag) {
        var canvas = {
            tagName: tag.toUpperCase(), nodeName: tag.toUpperCase(),
            style: {}, className: '', id: '', src: '', href: '',
            setAttribute: function () {}, getAttribute: function () { return null; }, hasAttribute: function () { return false; },
            appendChild: function () {}, removeChild: function () {}, insertBefore: function () {},
            addEventListener: function () {}, removeEventListener: function () {},
            classList: { add: function () {}, remove: function () {}, contains: function () { return false; } },
            parentNode: null, parentElement: null,
            childNodes: [], children: [], childElementCount: 0,
            innerHTML: '', textContent: '', innerText: '',
            offsetWidth: 0, offsetHeight: 0,
            getBoundingClientRect: function () { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
            getContext: function (t) {
                if (t === '2d') {
                    return new Proxy({
                        canvas: { width: canvas.width || 300, height: canvas.height || 150 },
                        fillStyle: '', strokeStyle: '', font: '10px sans-serif',
                        globalAlpha: 1, globalCompositeOperation: 'source-over',
                        getImageData: function (x, y, w, h) { return { data: new Uint8ClampedArray((w || 1) * (h || 1) * 4), width: w || 1, height: h || 1 }; },
                        measureText: function (s) { return { width: (String(s || '').length * 6) }; },
                        createLinearGradient: function () { return new Proxy({}, { get: function () { return function () {}; } }); },
                    }, { get: function (t, p) { if (p in t) return typeof t[p] === 'function' ? t[p].bind(t) : t[p]; return typeof p === 'string' ? function () {} : undefined; } });
                }
                if (t === 'webgl' || t === 'webgl2') {
                    return new Proxy({
                        getParameter: function (p) { return null; }, getExtension: function () { return null; }, getSupportedExtensions: function () { return []; },
                    }, { get: function (t, p) { if (p in t) return typeof t[p] === 'function' ? t[p].bind(t) : t[p]; return typeof p === 'string' ? function () {} : undefined; } });
                }
                return null;
            },
            toDataURL: function () { return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='; },
            toBlob: function (cb, type) {
                var mime = type || 'image/png';
                var bytes = base64ToBytes('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==');
                __timers.push(function () { cb(new BlobShim([bytes], { type: mime })); });
            },
            width: 300, height: 150,
        };
        return canvas;
    }

    var docObj = {
        title: 'DeepSeek', referrer: '', readyState: 'complete', visibilityState: 'visible', hidden: false,
        charset: 'UTF-8', URL: __target, domain: __targetHost,
        scripts: docScripts, currentScript: currentScriptEl, body: bodyEl,
        head: { appendChild: function () {}, removeChild: function () {}, childNodes: [], children: [] },
        documentElement: {
            tagName: 'HTML', style: {}, lang: 'en',
            classList: { add: function () {}, remove: function () {}, contains: function () { return false; } },
            setAttribute: function () {}, getAttribute: function () { return null; },
            clientWidth: 1920, clientHeight: 1080, scrollWidth: 1920, scrollHeight: 1080,
        },
        createElement: makeEl, createElementNS: function (_, tag) { return makeEl(tag); },
        createTextNode: function (t) { return { nodeType: 3, textContent: t, nodeValue: t }; },
        createComment: function (t) { return { nodeType: 8, textContent: t }; },
        createDocumentFragment: function () { return { childNodes: [], children: [], appendChild: function () {}, removeChild: function () {} }; },
        getElementById: function () { return null; },
        getElementsByTagName: function (t) { return t === 'script' ? docScripts : []; },
        getElementsByClassName: function () { return []; },
        querySelector: function () { return null; },
        querySelectorAll: function () { return { length: 0, forEach: function () {}, item: function () { return null; } }; },
        addEventListener: function () {}, removeEventListener: function () {}, dispatchEvent: function () { return true; },
        hasFocus: function () { return true; },
        getSelection: function () { return { toString: function () { return ''; } }; },
        createRange: function () { return { setStart: function () {}, setEnd: function () {}, collapse: function () {} }; },
        execCommand: function () { return false; },
        write: function () {}, writeln: function () {},
    };
    Object.defineProperty(docObj, 'cookie', {
        get: function () { return cookieStore; },
        set: setCookie,
        configurable: true,
    });
    setG('document', docObj);

    var locObj = {
        href: __target, origin: 'https://' + __targetHost, protocol: 'https:', host: __targetHost, hostname: __targetHost,
        port: '', pathname: '/', search: '', hash: '',
        reload: function () {}, replace: function () {}, assign: function () {},
        toString: function () { return __target; },
    };
    setG('location', locObj);

    var pluginData = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: '', length: 1 },
    ];
    var navObj = {
        userAgent: __ua, language: 'en-US', languages: ['en-US', 'en'],
        platform: 'Win32', vendor: 'Google Inc.', product: 'Gecko',
        appName: 'Netscape', appVersion: '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
        appCodeName: 'Mozilla', hardwareConcurrency: 8, maxTouchPoints: 0,
        cookieEnabled: true, javaEnabled: function () { return false; }, onLine: true, webdriver: false,
        plugins: pluginData, mimeTypes: [],
        sendBeacon: function (url, data) {
            var bytes = toByteArray(typeof data === 'string' ? data : JSON.stringify(data));
            enqueue('POST', url, { 'Content-Type': 'application/json' }, bytes).entry.settle = function () {};
            return true;
        },
        connection: null,
        permissions: { query: function () { return Promise.resolve({ state: 'granted' }); } },
    };
    setG('navigator', navObj);

    setG('screen', { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24, orientation: { type: 'landscape-primary', angle: 0 } });

    var perfStart = Date.now();
    setG('performance', {
        now: function () { return Date.now() - perfStart; }, timeOrigin: perfStart,
        timing: { navigationStart: perfStart - 1000, requestStart: perfStart - 500, responseStart: perfStart - 200, responseEnd: perfStart - 100, domLoading: perfStart - 80, domInteractive: perfStart - 50, domComplete: perfStart - 10, loadEventStart: perfStart - 5, loadEventEnd: perfStart },
        navigation: { type: 0, redirectCount: 0 },
        memory: { jsHeapSizeLimit: 2190000000, totalJSHeapSize: 50000000, usedJSHeapSize: 30000000 },
        getEntries: function () { return []; }, getEntriesByType: function () { return []; }, getEntriesByName: function () { return []; },
        clearMarks: function () {}, clearMeasures: function () {}, mark: function () {}, measure: function () {},
        addEventListener: function () {}, removeEventListener: function () {},
    });

    function makeStore() {
        var d = {};
        return {
            getItem: function (k) { return d.hasOwnProperty(k) ? d[k] : null; },
            setItem: function (k, v) { d[k] = String(v); },
            removeItem: function (k) { delete d[k]; },
            clear: function () { d = {}; },
            key: function (i) { return Object.keys(d)[i] || null; },
        };
    }
    setG('localStorage', makeStore());
    setG('sessionStorage', makeStore());

    setG('history', { length: 2, scrollRestoration: 'auto', state: null, back: function () {}, forward: function () {}, go: function () {}, pushState: function () {}, replaceState: function () {} });

    setG('MutationObserver', function () { this.observe = function () {}; this.disconnect = function () {}; this.takeRecords = function () { return []; }; });
    setG('IntersectionObserver', function () { this.observe = function () {}; this.unobserve = function () {}; this.disconnect = function () {}; });
    setG('ResizeObserver', function () { this.observe = function () {}; this.unobserve = function () {}; this.disconnect = function () {}; });
    setG('PerformanceObserver', function () { this.observe = function () {}; this.disconnect = function () {}; });

    var FormData = function () { this._d = []; };
    FormData.prototype.append = function (k, v) { this._d.push([k, v]); };
    FormData.prototype.get = function (k) { var f = this._d.filter(function (p) { return p[0] === k; })[0]; return f ? f[1] : null; };
    setG('FormData', FormData);

    var Headers = function (init) {
        this._h = {};
        var self = this;
        if (init) { for (var k in init) self._h[k.toLowerCase()] = init[k]; }
    };
    Headers.prototype.get = function (k) { return this._h[k.toLowerCase()] || null; };
    Headers.prototype.set = function (k, v) { this._h[k.toLowerCase()] = v; };
    Headers.prototype.has = function (k) { return (k.toLowerCase() in this._h); };
    Headers.prototype.append = function (k, v) { this._h[k.toLowerCase()] = v; };
    Headers.prototype.delete = function (k) { delete this._h[k.toLowerCase()]; };
    Headers.prototype.forEach = function (cb) { for (var k in this._h) cb(this._h[k], k); };
    setG('Headers', Headers);

    function EventShim(t, o) { o = o || {}; this.type = t; this.bubbles = !!o.bubbles; this.cancelable = !!o.cancelable; this.target = null; this.currentTarget = null; }
    EventShim.prototype.preventDefault = function () {};
    EventShim.prototype.stopPropagation = function () {};
    EventShim.prototype.stopImmediatePropagation = function () {};
    setG('Event', EventShim);
    var CustomEvent = function (t, o) { EventShim.call(this, t, o); this.detail = (o && o.detail) || null; };
    CustomEvent.prototype = Object.create(EventShim.prototype);
    setG('CustomEvent', CustomEvent);
    setG('EventTarget', function () { this.addEventListener = function () {}; this.removeEventListener = function () {}; this.dispatchEvent = function () { return true; }; });
    setG('AbortController', function () { this.signal = { aborted: false, addEventListener: function () {}, removeEventListener: function () {} }; this.abort = function () { this.signal.aborted = true; }; });
    setG('AbortSignal', { timeout: function () { return { aborted: false, addEventListener: function () {}, removeEventListener: function () {} }; } });

    function WorkerShim() { this.postMessage = function () {}; this.addEventListener = function () {}; this.removeEventListener = function () {}; this.terminate = function () {}; }
    setG('Worker', WorkerShim);
    setG('SharedWorker', WorkerShim);
    setG('ServiceWorker', WorkerShim);

    var WebSocket = function (url) { this.url = url; this.readyState = 3; var self = this; __timers.push(function () { if (self.onerror) self.onerror({}); }); };
    WebSocket.prototype.send = function () {}; WebSocket.prototype.close = function () {};
    WebSocket.prototype.addEventListener = function () {}; WebSocket.prototype.removeEventListener = function () {};
    setG('WebSocket', WebSocket);

    setG('Image', function Image(w, h) { this.width = w || 0; this.height = h || 0; this.complete = false; this.src = ''; });
    setG('Audio', function Audio(src) { this.src = src || ''; this.play = function () { return Promise.resolve(); }; this.pause = function () {}; });

    setG('matchMedia', function (q) { return { matches: false, media: q, addEventListener: function () {}, removeEventListener: function () {}, addListener: function () {}, removeListener: function () {} }; });
    setG('getComputedStyle', function () { return new Proxy({}, { get: function () { return ''; } }); });
    setG('getSelection', function () { return { toString: function () { return ''; }, rangeCount: 0 }; });

    setG('open', function () { return null; });
    setG('close', function () {});
    setG('focus', function () {});
    setG('blur', function () {});
    setG('print', function () {});
    setG('alert', function () {});
    setG('confirm', function () { return true; });
    setG('prompt', function () { return ''; });
    setG('postMessage', function () {});
    setG('structuredClone', function (v) { return JSON.parse(JSON.stringify(v)); });

    setG('innerWidth', 1920); setG('innerHeight', 1080);
    setG('outerWidth', 1920); setG('outerHeight', 1080);
    setG('devicePixelRatio', 1);
    setG('screenX', 0); setG('screenY', 0);
    setG('scrollX', 0); setG('scrollY', 0);
    setG('pageXOffset', 0); setG('pageYOffset', 0);
    setG('origin', 'https://' + __targetHost);

    var winListeners = {};
    setG('addEventListener', function (evt, fn) { (winListeners[evt] = winListeners[evt] || []).push(fn); });
    setG('removeEventListener', function (evt, fn) { if (winListeners[evt]) winListeners[evt] = winListeners[evt].filter(function (f) { return f !== fn; }); });
    setG('dispatchEvent', function (evt) { (winListeners[evt && evt.type] || []).forEach(function (fn) { try { fn(evt); } catch (e) {} }); return true; });

    setG('chrome', {
        runtime: { connect: function () { return { onMessage: { addListener: function () {}, removeListener: function () {} }, postMessage: function () {}, disconnect: function () {} }; }, sendMessage: function () {}, id: undefined },
        loadTimes: function () { return {}; }, csi: function () { return {}; },
        app: { isInstalled: false, InstallState: {}, RunningState: {} },
    });

    // crypto.getRandomValues — не нужна криптографическая стойкость (только
    // фингерпринт-заполнение), Math.random достаточно
    setG('crypto', {
        getRandomValues: function (arr) {
            for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256);
            return arr;
        },
        randomUUID: function () {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
                var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        },
    });

    setG('gokuProps', __gokuProps);
    setG('awsWafCookieDomainList', __cookieDomains);
})();
