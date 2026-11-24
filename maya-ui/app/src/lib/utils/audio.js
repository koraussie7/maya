/**
 * 마이크 캡처 → 16kHz mono Int16 PCM 청크.
 * 백엔드(integration/)와 동일한 계약:
 *   - 브라우저 sample rate 48000/44100 → OfflineAudioContext 로 16000 리샘플
 *   - Float32 → Int16 PCM (little-endian)
 *   - onChunk: ArrayBuffer 또는 Uint8Array 콜백
 */

export class MicCapture {
  /**
   * @param {(chunk: ArrayBuffer) => void} onChunk
   * @param {(rms: number) => void} [onLevel]  0~1 RMS, Waveform용
   * @param {number} [chunkMs=100]
   */
  constructor(onChunk, onLevel, chunkMs = 100) {
    this.onChunk = onChunk;
    this.onLevel = onLevel ?? (() => {});
    this.chunkMs = chunkMs;
    this.stream = null;
    this.audioCtx = null;
    this.source = null;
    this.processor = null;
    this.running = false;
    this._silenceFlush = null;
  }

  async start() {
    if (this.running) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const inRate =
      this.stream.getAudioTracks()[0].getSettings().sampleRate ?? 48000;
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: inRate,
    });
    this.source = this.audioCtx.createMediaStreamSource(this.stream);

    // 100ms 단위 ScriptProcessor — AudioWorklet 의존 없이 동작
    const bufSize = Math.max(
      2048,
      Math.floor((inRate * this.chunkMs) / 1000 / 512) * 2048,
    );
    this.processor = this.audioCtx.createScriptProcessor(bufSize, 1, 1);
    this.processor.onaudioprocess = (e) => this._onProcess(e, inRate);

    this.source.connect(this.processor);
    this.processor.connect(this.audioCtx.destination); // 일부 브라우저는 destination 연결 필요
    this.running = true;
  }

  _onProcess(e, inRate) {
    const input = e.inputBuffer.getChannelData(0);
    // RMS 계산 (Waveform용)
    if (this.onLevel) {
      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
      this.onLevel(Math.min(1, Math.sqrt(sum / input.length) * 3.5));
    }
    // 16kHz mono Int16 으로 다운샘플 (간단한 nearest)
    const outLen = Math.floor((input.length * 16000) / inRate);
    const pcm = new Int16Array(outLen);
    const ratio = inRate / 16000;
    for (let i = 0; i < outLen; i++) {
      const s = input[Math.floor(i * ratio)];
      pcm[i] = Math.max(-32768, Math.min(32767, (s * 32768) | 0));
    }
    this.onChunk(pcm.buffer);
  }

  stop() {
    if (!this.running) return;
    this.running = false;
    try {
      this.processor?.disconnect();
    } catch {}
    try {
      this.source?.disconnect();
    } catch {}
    try {
      this.stream?.getTracks().forEach((t) => t.stop());
    } catch {}
    try {
      this.audioCtx?.close();
    } catch {}
    this.processor = null;
    this.source = null;
    this.stream = null;
    this.audioCtx = null;
    this.onLevel(0);
  }
}

/**
 * TTS 오디오 재생기.
 * - WAV (16kHz mono 16-bit PCM) 또는 raw Int16 PCM 입력
 * - BaseAudioContext 큐로 끊김 없이 재생
 */
export class AudioPlayer {
  constructor() {
    this.ctx = null;
    this.queue = [];
    this.playing = false;
    this.gainNode = null;
    this.onLevel = null;
  }

  _ensureCtx() {
    if (this.ctx) return;
    this.ctx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 16000,
    });
    this.gainNode = this.ctx.createGain();
    this.gainNode.connect(this.ctx.destination);
  }

  /** Int16 PCM (16kHz mono) 청크를 큐에 추가 */
  enqueuePCM(pcmInt16) {
    this._ensureCtx();
    if (this.ctx.state === "suspended") this.ctx.resume();
    const f32 = new Float32Array(pcmInt16.length);
    for (let i = 0; i < pcmInt16.length; i++) f32[i] = pcmInt16[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, 16000);
    buf.copyToChannel(f32, 0);
    this.queue.push(buf);
    this._pump();
  }

  /** WAV 바이트 배열(헤더 포함) → 큐에 추가 (간단 파서) */
  enqueueWAV(wavBytes) {
    const view = new DataView(wavBytes);
    // RIFF 헤더 검증
    if (view.getUint32(0, false) !== 0x52494646) return; // "RIFF"
    // fmt 청크 찾기
    let offset = 12;
    while (offset < view.byteLength) {
      const id = view.getUint32(offset, false);
      const size = view.getUint32(offset + 4, true);
      if (id === 0x666d7420) {
        // "fmt "
        const channels = view.getUint16(offset + 10, true);
        const sampleRate = view.getUint32(offset + 12, true);
        const bitsPerSample = view.getUint16(offset + 22, true);
        if (bitsPerSample !== 16) {
          // 16-bit 만 지원 (파이프라인 계약)
          return;
        }
        const dataStart = offset + 8 + size;
        const samples = (view.byteLength - dataStart) / 2;
        const pcm = new Int16Array(samples);
        for (let i = 0; i < samples; i++)
          pcm[i] = view.getInt16(dataStart + i * 2, true);
        this._ensureCtx();
        const f32 = new Float32Array(pcm.length);
        for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
        // mono 가정 (필요 시 다운믹스)
        if (channels > 1) {
          const monoF32 = new Float32Array(samples / channels);
          for (let i = 0; i < monoF32.length; i++)
            monoF32[i] = f32[i * channels];
          const buf = this.ctx.createBuffer(1, monoF32.length, sampleRate);
          buf.copyToChannel(monoF32, 0);
          this.queue.push(buf);
        } else {
          const buf = this.ctx.createBuffer(1, f32.length, sampleRate);
          buf.copyToChannel(f32, 0);
          this.queue.push(buf);
        }
        this._pump();
        return;
      }
      offset += 8 + size;
    }
  }

  _pump() {
    if (this.playing || this.queue.length === 0) return;
    const buf = this.queue.shift();
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gainNode);
    this.playing = true;
    src.onended = () => {
      this.playing = false;
      this._pump();
    };
    src.start();
  }

  stop() {
    this.queue = [];
    this.playing = false;
    try {
      this.ctx?.close();
    } catch {}
    this.ctx = null;
  }
}
