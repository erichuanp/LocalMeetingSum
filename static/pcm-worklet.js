// AudioWorklet that emits float32 mono PCM in fixed-size chunks.
// Runs in the audio rendering thread; posts merged buffers to the main thread
// every ~emitMs milliseconds (default ~100ms = 1600 samples at 16kHz).
class PCMWorklet extends AudioWorkletProcessor {
  static get parameterDescriptors() { return []; }

  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    // sampleRate is a global provided by AudioWorkletGlobalScope.
    this._emitSamples = Math.max(64, Math.round((o.emitMs || 100) * sampleRate / 1000));
    this._buf = new Float32Array(this._emitSamples * 2);
    this._fill = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (!ch || ch.length === 0) return true;

    // Append channel 0 to ring; if multi-channel, downmix here.
    let block;
    if (input.length > 1) {
      block = new Float32Array(ch.length);
      for (let c = 0; c < input.length; c++) {
        const src = input[c];
        for (let i = 0; i < block.length; i++) block[i] += src[i];
      }
      const inv = 1 / input.length;
      for (let i = 0; i < block.length; i++) block[i] *= inv;
    } else {
      block = ch;
    }

    // Grow buffer if needed (shouldn't happen).
    if (this._fill + block.length > this._buf.length) {
      const grown = new Float32Array((this._fill + block.length) * 2);
      grown.set(this._buf.subarray(0, this._fill));
      this._buf = grown;
    }
    this._buf.set(block, this._fill);
    this._fill += block.length;

    // Emit full chunks.
    while (this._fill >= this._emitSamples) {
      const out = new Float32Array(this._emitSamples);
      out.set(this._buf.subarray(0, this._emitSamples));
      // Shift residue down.
      this._buf.copyWithin(0, this._emitSamples, this._fill);
      this._fill -= this._emitSamples;
      // Transfer ownership to avoid copy.
      this.port.postMessage({ pcm: out, sr: sampleRate }, [out.buffer]);
    }
    return true;
  }
}

registerProcessor('pcm-worklet', PCMWorklet);
