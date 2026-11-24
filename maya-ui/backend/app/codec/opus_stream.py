"""
opus_stream.py — PCM16 → Opus 실시간 스트리밍 송출 (path C-1)

opuslib가 Python 3.11+ ctypes에서 비어 있는 `Encoder` 구조체를
자동으로 POINTER로 변환하지 못하는 호환성 문제가 있습니다.
opuslib의 Encoder/Decoder 클래스를 우회하고 libopus의 raw API를
직접 호출하는 thin wrapper를 제공합니다.

목표
----
- 16 kHz / mono / PCM16 입력
- Opus frame size 20 ms (320 샘플) = 1 frame
- 32 kbps 비트레이트 (음성 대화 품질, 16 kbps보다 자연스러움)
- opus_encode → 길이 prefix → WebSocket chunk 송출
- opus_decode → PCM16 출력 (검증용)

송출 프로토콜 (단방향 / server->client)
-----------------------------------------
각 chunk는 [4 byte uint32 BE length][opus frame bytes].
frame size가 20 ms로 고정되므로 별도 timing header 불필요.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Iterator

import opuslib
from opuslib.api import decoder, encoder

from . import _opusctl as ctl

# ----- 상수 ---------------------------------------------------------------
SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_SAMPLES = 320          # 20 ms @ 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # PCM16 = 2 bytes/sample
BITRATE = 32_000

#: opuslib.application 상수는 **버전에 따라 다르다**.
#:   opuslib 2.x : 2048(VoIP) / 2049(Audio) / 2051(Restricted lowdelay)
#:   opuslib 3.x : 2049/2051/2052 를 audio/vRestrictedLowdelay/voip 로 이름만 바꿈
#: 여기서는 "Audio"(음질 우선)를 번호로 고정한다 — 이름이 없어도 동작해야 한다.
APPLICATION_AUDIO = int(getattr(opuslib, "APPLICATION_AUDIO", 2049))
OPUS_OK = int(getattr(opuslib, "OK", 0))


# ----- opuslib의 Encoder 구조체 ------------------------------------------
# ctypes.Structure의 subclass이지만 _fields_가 비어 있습니다.
# Python 3.11+에서는 이런 class를 POINTER()로 직접 캐스팅할 수 없습니다.
# 따라서 raw pointer (LP_Encoder)를 그대로 보관합니다.

class _EncoderOpaque(ctypes.Structure):
    """opuslib.api.encoder.Encoder 와 동일한 빈 구조체 (호환용)"""
    pass


LP_Encoder = ctypes.POINTER(_EncoderOpaque)


def _create_encoder() -> LP_Encoder:
    """opus_encoder_create 직접 호출 — 음성 트래픽 절감형 32 kbps."""
    err = ctypes.c_int()
    ptr = encoder.libopus_create(
        SAMPLE_RATE,
        CHANNELS,
        APPLICATION_AUDIO,
        ctypes.byref(err),
    )
    if err.value != OPUS_OK or not ptr:
        raise RuntimeError(f"opus_encoder_create failed: code={err.value}")

    # opuslib 2.x 의 `ctl.opus_encoder_ctl(ptr, 요청, 값)` 형태는 3.x 에서 없어졌다.
    # ctypes 는 가변인자를 쓸 수 없어 2.x 구현이 깨진 것이므로, **변형별 랩퍼**를 쓴다
    # (`_opusctl.py` — 두 버전 모두에서 동작).
    ctl.set_bitrate(ptr, BITRATE)          # 32 kbps
    ctl.set_vbr(ptr, 0)                    # CBR — 네트워크 트래픽 일정
    ctl.set_inband_fec(ptr, 1)             # 패킷 손실 보상
    ctl.set_complexity(ptr, 5)             # 0(빠름)~10(좋음)

    return ptr


def _destroy_encoder(ptr: LP_Encoder) -> None:
    # 3.x: destroy() (포인터 변환을 내부에서 함) / 2.x: libopus_destroy()
    fn = getattr(encoder, "libopus_destroy", None) or getattr(encoder, "destroy", None)
    if ptr and fn:
        try:
            fn(ptr)
        except Exception:  # noqa: BLE001
            pass


class _DecoderOpaque(ctypes.Structure):
    pass


LP_Decoder = ctypes.POINTER(_DecoderOpaque)


def _create_decoder() -> LP_Decoder:
    err = ctypes.c_int()
    ptr = decoder.libopus_create(
        SAMPLE_RATE,
        CHANNELS,
        ctypes.byref(err),
    )
    if err.value != OPUS_OK or not ptr:
        raise RuntimeError(f"opus_decoder_create failed: code={err.value}")
    return ptr


def _destroy_decoder(ptr: LP_Decoder) -> None:
    fn = getattr(decoder, "libopus_destroy", None) or getattr(decoder, "destroy", None)
    if ptr and fn:
        try:
            fn(ptr)
        except Exception:  # noqa: BLE001
            pass


# ----- 고수준 wrapper -----------------------------------------------------
class OpusStreamCodec:
    """PCM16 ↔ Opus 변환기.

    opuslib의 Encoder/Decoder 클래스를 우회하여 raw pointer를
    직접 관리합니다. Python 3.11+ ctypes 호환성 우회.
    """

    def __init__(self) -> None:
        self._enc = _create_encoder()
        self._dec = _create_decoder()
        self._enc_pcm = ctypes.create_string_buffer(FRAME_BYTES)
        self._enc_out = ctypes.create_string_buffer(4000)  # max 4 KB
        self._dec_pcm = ctypes.create_string_buffer(FRAME_BYTES * 12)
        # 복호화 후 출력 가능한 샘플 수
        self._dec_nb_samples = ctypes.c_int()

    def close(self) -> None:
        _destroy_encoder(self._enc)
        _destroy_decoder(self._dec)
        self._enc = None
        self._dec = None

    def __enter__(self) -> "OpusStreamCodec":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    # ----- encode ---------------------------------------------------------
    def encode_frame(self, pcm16: bytes) -> bytes:
        """PCM16 한 frame (640 bytes, 320 samples) → Opus frame bytes."""
        if len(pcm16) != FRAME_BYTES:
            raise ValueError(
                f"frame must be {FRAME_BYTES} bytes, got {len(pcm16)}"
            )

        # ⚠ opuslib 3.x 제 libopus_encode 는 argtypes 가 고정되어 있다:
        #    (EncoderPointer, opuslib.api.c_int16_pointer, int, c_char_p, int32)
        #   즉 입력은 **c_int16_pointer**, 출력은 **c_char_p** 를 요구한다.
        #   c_char_p 자리에 쓰기 가능한 버퍼를 넘기면 TypeError 가 난다(실측).
        #   가장 확실한 방법은 opuslib 이 제공하는 `encoder.encode()` 를 쓰는 것:
        #   내부에서 cast/버퍼할당/에러처리를 이미 정확히 해 준다.
        return encoder.encode(self._enc, pcm16, FRAME_SAMPLES, ctypes.sizeof(self._enc_out))

    def encode_stream(self, pcm16_full: bytes) -> Iterator[bytes]:
        """긴 PCM16 데이터 → 20 ms Opus frame 이터레이터."""
        # 잘림 방지: silence로 패딩
        n = len(pcm16_full)
        pad = (-n) % FRAME_BYTES
        if pad:
            pcm16_full = pcm16_full + b"\x00" * pad
        for off in range(0, len(pcm16_full), FRAME_BYTES):
            yield self.encode_frame(pcm16_full[off:off + FRAME_BYTES])

    @staticmethod
    def frame_to_wire(opus_frame: bytes) -> bytes:
        """[uint32 BE length][opus bytes] — WebSocket chunk payload."""
        return struct.pack(">I", len(opus_frame)) + opus_frame

    def encode_to_wire(self, pcm16_full: bytes) -> Iterator[bytes]:
        """PCM16 전체 → wire-format Opus frame 이터레이터."""
        for opus_frame in self.encode_stream(pcm16_full):
            yield self.frame_to_wire(opus_frame)

    # ----- decode ---------------------------------------------------------
    def decode_frame(self, opus_frame: bytes) -> bytes:
        """Opus frame → PCM16 (FRAME_BYTES bytes)."""
        # 인코딩과 같은 이유로 opuslib 헬퍼를 쓴다(argtypes 가 버전마다 다름).
        # 3.x 시그니처: decode(state, opus_data, length, frame_size, decode_fec, channels)
        #   · length  = 입력 opus 바이트 수
        #   · frame_size = 출력 버퍼 크기(샘플 수)
        return decoder.decode(
            self._dec,
            opus_frame,
            len(opus_frame),
            FRAME_SAMPLES * 12,
            decode_fec=False,
            channels=CHANNELS,
        )

    def decode_wire(self, payload: bytes) -> bytes:
        """wire-format Opus chunk → PCM16."""
        if len(payload) < 4:
            raise ValueError("payload too short")
        (n,) = struct.unpack(">I", payload[:4])
        if len(payload) < 4 + n:
            raise ValueError("payload truncated")
        return self.decode_frame(payload[4:4 + n])


# ----- 자가 검증 ---------------------------------------------------------
if __name__ == "__main__":
    import time

    # 1초 테스트 신호: 440 Hz 사인파 @ 16 kHz
    import math
    samples = [
        int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        for i in range(SAMPLE_RATE)
    ]
    pcm_full = struct.pack(f"<{len(samples)}h", *samples)
    print(f"입력: {len(pcm_full)} bytes ({len(samples) / SAMPLE_RATE:.1f}s)")

    with OpusStreamCodec() as codec:
        t0 = time.perf_counter()
        encoded_size = 0
        decoded_pcm = bytearray()
        frame_count = 0
        for opus_frame in codec.encode_stream(pcm_full):
            encoded_size += len(opus_frame)
            frame_count += 1
            decoded = codec.decode_frame(opus_frame)
            decoded_pcm.extend(decoded)
        elapsed = time.perf_counter() - t0

    ratio = len(pcm_full) / encoded_size
    print(f"프레임 수: {frame_count}")
    print(f"압축 크기: {encoded_size} bytes  (비율 {ratio:.2f}x)")
    print(f"디코드 후: {len(decoded_pcm)} bytes")
    print(f"처리 시간: {elapsed * 1000:.1f} ms ({frame_count / elapsed:.0f} frames/s)")
    print("✅ opuslib 호환성 우회 검증 완료")
