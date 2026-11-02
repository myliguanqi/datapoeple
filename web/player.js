// 实时音视频播放器：WebSocket 接收服务端的帧包（JPEG + 40ms PCM），音频用 WebAudio 排队播放，
// 画面按"当前正在发声的音频时间"挑帧显示，从而做到音画同步。协议见 docs/live/API.md。
//
// 用法：
//   const p = new LivePlayer(canvas, { latency: 0.3 });
//   await p.connect(`ws://${location.host}/ws/sessions/default`);   // 必须在点击事件里调用（浏览器自动播放策略）
//   p.addEventListener('message', e => console.log(e.detail));      // 服务端 JSON 事件
//   p.addEventListener('seg', e => showSubtitle(e.detail.text));     // 当前播放的语音段变化

const HEADER_BYTES = 32;
const MAGIC = 0x314c4846; // "FHL1" 小端
const FLAG_FILLER = 2;

export class LivePlayer extends EventTarget {
  constructor(canvas, opts = {}) {
    super();
    this.canvas = canvas;
    this.g = canvas.getContext('2d');
    this.latency = opts.latency ?? 0.3; // 抖动缓冲（秒）：网络越差设得越大
    this.ws = null;
    this.ctx = null;
    this.gain = null;
    this.sampleRate = 16000;
    this.nextTime = 0;
    this.frames = [];
    this.shown = null;
    this.segText = new Map();
    this.curSeg = 0;
    this.raf = 0;
    this.stats = this._freshStats();
    this._fpsCount = 0;
    this._fpsT = performance.now();
    this._render = this._render.bind(this);
  }

  _freshStats() {
    return { packets: 0, fps: 0, bufferMs: 0, resyncs: 0, dropped: 0, fillers: 0, gaps: 0, lastSeq: -1 };
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  get audioLocked() {
    return !!this.ctx && this.ctx.state === 'suspended';
  }

  get connected() {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  async connect(url) {
    this.disconnect();
    if (!this.ctx) {
      try {
        // 直接用 16k 的 AudioContext，由浏览器统一重采样，避免逐包重采样产生的接缝杂音
        this.ctx = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      } catch (e) {
        this.ctx = new AudioContext({ latencyHint: 'interactive' });
      }
      this.gain = this.ctx.createGain();
      this.gain.connect(this.ctx.destination);
    }
    // 不 await：没有用户手势时 resume 会一直挂起（自动连接场景），等用户点击页面后才开始出声出画；
    // OBS 浏览器源等允许自动播放的环境会直接开始
    this.ctx.resume().catch(() => {});
    this.nextTime = 0;
    this.stats = this._freshStats();
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => this._emit('open');
    ws.onclose = (e) => {
      if (this.ws === ws) this.ws = null;
      this._emit('close', { code: e.code, reason: e.reason || 'server' });
    };
    ws.onerror = () => this._emit('error', { message: 'WebSocket 连接错误' });
    ws.onmessage = (ev) => (typeof ev.data === 'string' ? this._onText(ev.data) : this._onPacket(ev.data));
    this.ws = ws;
    if (!this.raf) this.raf = requestAnimationFrame(this._render);
  }

  disconnect() {
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onclose = null;
      ws.close();
      this._emit('close', { code: 1000, reason: 'client' });
    }
    for (const f of this.frames) f.bmp?.close?.();
    this.frames = [];
  }

  send(obj) {
    if (!this.connected) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  setLatency(sec) {
    this.latency = Math.max(0.05, sec);
  }

  setVolume(v) {
    if (this.gain) this.gain.gain.value = v;
  }

  _onText(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === 'hello' && msg.sample_rate) this.sampleRate = msg.sample_rate;
    if (msg.type === 'segment') {
      this.segText.set(msg.seg_id, msg.text);
      if (this.segText.size > 500) this.segText.delete(this.segText.keys().next().value);
    }
    this._emit('message', msg);
  }

  _onPacket(buf) {
    if (buf.byteLength < HEADER_BYTES) return;
    const dv = new DataView(buf);
    if (dv.getUint32(0, true) !== MAGIC || dv.getUint8(4) !== 1) return;
    const flags = dv.getUint8(5);
    const seq = dv.getUint32(8, true);
    const seg = dv.getUint32(20, true);
    const jpegLen = dv.getUint32(24, true);
    const audioLen = dv.getUint32(28, true);
    const st = this.stats;
    st.packets++;
    this._fpsCount++;
    if (st.lastSeq >= 0 && seq !== st.lastSeq + 1) st.gaps++;
    st.lastSeq = seq;
    if (flags & FLAG_FILLER) st.fillers++;

    const ctx = this.ctx;
    const now = ctx.currentTime;
    if (this.nextTime < now + 0.01) {
      // 缓冲见底（首包 / 网络卡顿）：重新留出 latency 的缓冲
      if (this.nextTime > 0) st.resyncs++;
      this.nextTime = now + this.latency;
    } else if (this.nextTime - now > this.latency + 0.6) {
      st.dropped++; // 积压过多（标签页切到后台等）：丢包追上实时
      return;
    }

    const jpeg = new Uint8Array(buf, HEADER_BYTES, jpegLen);
    const pcm = new Int16Array(buf.slice(HEADER_BYTES + jpegLen, HEADER_BYTES + jpegLen + audioLen));
    const ab = ctx.createBuffer(1, Math.max(1, pcm.length), this.sampleRate);
    const ch = ab.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = ctx.createBufferSource();
    src.buffer = ab;
    src.connect(this.gain);
    src.start(this.nextTime);

    const item = { t: this.nextTime, seq, seg, flags, bmp: null };
    this.nextTime += pcm.length / this.sampleRate;
    this.frames.push(item);
    createImageBitmap(new Blob([jpeg], { type: 'image/jpeg' }))
      .then((b) => {
        item.bmp = b;
      })
      .catch(() => {});
  }

  _audibleTime() {
    const ctx = this.ctx;
    if (ctx.getOutputTimestamp) {
      const ts = ctx.getOutputTimestamp();
      if (ts && ts.contextTime > 0) return ts.contextTime;
    }
    return ctx.currentTime - (ctx.outputLatency || ctx.baseLatency || 0);
  }

  _render() {
    this.raf = requestAnimationFrame(this._render);
    if (!this.ctx) return;
    const at = this._audibleTime();
    let pick = null;
    while (this.frames.length && this.frames[0].t <= at) {
      const f = this.frames.shift();
      if (pick && pick !== f) pick.bmp?.close?.();
      pick = f;
    }
    if (pick && pick.bmp) {
      if (this.canvas.width !== pick.bmp.width) {
        this.canvas.width = pick.bmp.width;
        this.canvas.height = pick.bmp.height;
      }
      this.g.drawImage(pick.bmp, 0, 0);
      this.shown?.bmp?.close?.();
      this.shown = pick;
      if (pick.seg !== this.curSeg) {
        this.curSeg = pick.seg;
        this._emit('seg', { seg: pick.seg, text: pick.seg ? this.segText.get(pick.seg) || '' : '' });
      }
    }
    const nowMs = performance.now();
    if (nowMs - this._fpsT >= 1000) {
      this.stats.fps = Math.round((this._fpsCount * 1000) / (nowMs - this._fpsT));
      this._fpsCount = 0;
      this._fpsT = nowMs;
      this.stats.bufferMs = Math.max(0, Math.round((this.nextTime - this.ctx.currentTime) * 1000));
      this._emit('stats', { ...this.stats });
    }
  }
}
