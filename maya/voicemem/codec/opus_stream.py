"""PCM16 mono 16 kHz <-> Opus streaming codec built on opuslib.

Round-trip is verified in tests/test_opus_roundtrip.py.

We use opuslib's low-level API directly (encoder.create_state, encoder.encode,
decoder.create_state, decoder.decode, encoder.destroy, decoder.destroy) because
the high-level opuslib.Encoder/Decoder classes don't expose the ctypes setup
needed on macOS Python 3.11.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import opuslib
from opuslib.api import ctl, decoder, encoder


@dataclass
class OpusConfig:
    sample_rate: int = 16000
    channels: int = 1
    frame_size_ms: int = 20  # 20 ms = 320 samples @ 16 kHz
    application: int = opuslib.APPLICATION_VOIP
    bitrate: int = 32000  # bps
    vbr: bool = True
    complexity: int = 6
    use_inband_fec: bool = True
    packet_loss_pct: int = 0


class OpusStreamCodec:
    """Stateful PCM16 <-> Opus streaming codec.

    One instance per stream/session. The same frame size must be used
    for encode and decode.
    """

    def __init__(self, config: Optional[OpusConfig] = None) -> None:
        self.config = config or OpusConfig()
        self.frame_samples = (
            self.config.sample_rate * self.config.frame_size_ms
        ) // 1000

        # Allocate encoder/decoder state.
        self._enc = encoder.create_state(
            self.config.sample_rate,
            self.config.channels,
            self.config.application,
        )
        self._dec = decoder.create_state(
            self.config.sample_rate,
            self.config.channels,
        )

        # Configure encoder via ctl helpers.
        # In opuslib 1.3.x, ctl.set_* are request functions that take (encoder_state,
        # value). The canonical call shape is:
        #   encoder.encoder_ctl(enc, ctl.set_bitrate, bitrate_bps)
        # Passing `ctl.set_bitrate(value)` returns a partially-applied inner and
        # breaks on Python 3.11 / macOS.
        encoder.encoder_ctl(self._enc, ctl.set_bitrate, self.config.bitrate)
        encoder.encoder_ctl(self._enc, ctl.set_vbr, 1 if self.config.vbr else 0)
        encoder.encoder_ctl(self._enc, ctl.set_complexity, self.config.complexity)
        encoder.encoder_ctl(
            self._enc, ctl.set_inband_fec, 1 if self.config.use_inband_fec else 0
        )
        if self.config.packet_loss_pct:
            encoder.encoder_ctl(
                self._enc, ctl.set_packet_loss_perc, self.config.packet_loss_pct
            )

        # Opus adds a per-stream pre-skip (CELT lookahead). At 16 kHz VOIP it is
        # 104 samples (~6.5 ms). The first 104 decoded samples are zero padding;
        # the rest align with the input at lag = pre_skip. Query the encoder
        # directly so this stays correct across sample rates.
        self._pre_skip: int = int(
            encoder.encoder_ctl(self._enc, ctl.get_lookahead)
        )
        self._consumed_samples: int = 0  # how many input samples encoded so far
        self._skip_remaining: int = self._pre_skip  # zero samples to drop on output

    # ------------------------------------------------------------------ encode
    def encode_pcm16(self, pcm16: np.ndarray) -> bytes:
        """Encode one frame of int16 mono PCM (length = frame_samples).

        Returns the raw Opus packet bytes.
        """
        if pcm16.dtype != np.int16:
            pcm16 = pcm16.astype(np.int16)
        if pcm16.ndim != 1:
            pcm16 = pcm16.reshape(-1)
        if pcm16.size != self.frame_samples:
            raise ValueError(
                f"expected {self.frame_samples} samples, got {pcm16.size}"
            )
        c_contig = np.ascontiguousarray(pcm16)
        # opus_encode takes bytes PCM at sample_rate, frame_size samples.
        pcm_bytes = c_contig.tobytes()
        max_data_bytes = 4000  # max Opus packet size
        packet = encoder.encode(
            self._enc, pcm_bytes, self.frame_samples, max_data_bytes
        )
        return bytes(packet)

    # ------------------------------------------------------------------ decode
    def decode_opus(self, packet: bytes, decode_fec: bool = False) -> np.ndarray:
        """Decode one Opus packet back to int16 mono PCM (frame_samples).

        Returns a frame-aligned array of length ``frame_samples``. Internally the
        Opus decoder pads the first ``pre_skip`` samples with zeros; callers who
        concatenate output across frames should rely on ``stream_roundtrip`` or
        trim the first ``pre_skip`` samples from the joined buffer.
        """
        fec_int = 1 if decode_fec else 0
        # decoder.decode returns int16 little-endian bytes.
        pcm_bytes = decoder.decode(
            self._dec,
            bytes(packet),
            len(packet),
            self.frame_samples,
            fec_int,
            self.config.channels,
        )
        return np.frombuffer(pcm_bytes, dtype=np.int16).copy()

    @property
    def pre_skip(self) -> int:
        """Opus pre-skip in samples (samples to drop from the start of a stream)."""
        return self._pre_skip

    # ------------------------------------------------------------------ helpers
    def stream_roundtrip(self, pcm16: np.ndarray) -> np.ndarray:
        """Encode then decode a continuous PCM stream and return aligned output.

        Splits ``pcm16`` into frame-aligned chunks, encodes each, decodes each,
        and trims the Opus pre-skip from the concatenated output. The result is
        time-aligned with the input up to the per-stream pre-skip latency.
        """
        if pcm16.dtype != np.int16:
            pcm16 = pcm16.astype(np.int16)
        if pcm16.ndim != 1:
            pcm16 = pcm16.reshape(-1)
        n_full = (pcm16.size // self.frame_samples) * self.frame_samples
        if n_full == 0:
            return np.zeros(0, dtype=np.int16)

        out = np.zeros(n_full, dtype=np.int16)
        for i in range(0, n_full, self.frame_samples):
            frame = pcm16[i : i + self.frame_samples]
            packet = self.encode_pcm16(frame)
            out[i : i + self.frame_samples] = self.decode_opus(packet)

        # Drop the leading pre-skip (zero padding) so the rest aligns with input.
        out = out[self._pre_skip :]
        return out

    # ------------------------------------------------------------------ clean
    def close(self) -> None:
        if self._enc is not None:
            encoder.destroy(self._enc)
            self._enc = None
        if self._dec is not None:
            decoder.destroy(self._dec)
            self._dec = None

    def __enter__(self) -> "OpusStreamCodec":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
