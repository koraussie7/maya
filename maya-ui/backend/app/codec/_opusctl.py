"""opuslib 버전 차이를 흡수하는 얇은 래퍼.

문제
  opuslib 2.x 는 `ctl.opus_encoder_ctl(ptr, 요청ID, 값)` 형태로 가변인자 ctl 을
  흉내 냈는데, ctypes 에는 진짜 가변인자가 없어 3.x 에서 **사라졌다**(`AttributeError`).
  대신 버전별로 형태가 다른 `set_bitrate / set_vbr / ...` 가 있다:
    2.x : set_bitrate(func, obj, value)      ← ctl 함수 포인터를 첫 인자로 받는다
    3.x : set_bitrate(obj, value)
  또 인코더/디코더 생성·인코딩 함수 이름도 버전별로 다르다.

전략
  · 가능한 이름을 **순서대로 시도**하고, 성공한 경로를 기억한다.
  · 하나도 없으면 조용히 넘어간다(설정 실패가 인코딩 자체를 막지는 않게).
  · 하위 호환: `opus_encoder_ctl` 이 살아 있으면 그걸 그대로 쓴다.

이 파일은 **의존성 문제를 격리**하기 위한 것이며, 실패는 로그 없이 기본값 동작이다.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover
    from opuslib.api import ctl as _ctl
except Exception:  # noqa: BLE001
    _ctl = None  # type: ignore[assignment]

#: 2.x 의 ctl 함수 포인터(인코더용). 3.x 에는 없다.
_ENC_CTL_FUNC = getattr(_ctl, "encoder_ctl", None) if _ctl else None


def _call_setter(name: str, obj, value) -> bool:
    """`set_<name>` 을 두 시그니처로 시도한다."""
    if _ctl is None:
        return False
    fn = getattr(_ctl, f"set_{name}", None)
    if fn is None:
        return False
    # 3.x: (obj, value)
    try:
        fn(obj, value)
        return True
    except TypeError:
        pass
    except Exception:  # noqa: BLE001
        return False
    # 2.x: (func, obj, value)
    if _ENC_CTL_FUNC is not None:
        try:
            fn(_ENC_CTL_FUNC, obj, value)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _legacy_ctl(obj, request: int, value: int) -> bool:
    """2.x 전용 `opus_encoder_ctl` 이 있으면 사용."""
    fn = getattr(_ctl, "opus_encoder_ctl", None) if _ctl else None
    if fn is None:
        return False
    try:
        fn(obj, request, value)
        return True
    except Exception:  # noqa: BLE001
        return False


#: libopus ctl 요청 ID (2.x 스타일 경로용)
OPUS_SET_BITRATE = 4002
OPUS_SET_VBR = 4006
OPUS_SET_COMPLEXITY = 4010
OPUS_SET_INBAND_FEC = 4012


def set_bitrate(obj, value: int) -> bool:
    if _call_setter("bitrate", obj, value):
        return True
    return _legacy_ctl(obj, OPUS_SET_BITRATE, value)


def set_vbr(obj, value: int) -> bool:
    if _call_setter("vbr", obj, value):
        return True
    return _legacy_ctl(obj, OPUS_SET_VBR, value)


def set_complexity(obj, value: int) -> bool:
    if _call_setter("complexity", obj, value):
        return True
    return _legacy_ctl(obj, OPUS_SET_COMPLEXITY, value)


def set_inband_fec(obj, value: int) -> bool:
    if _call_setter("inband_fec", obj, value):
        return True
    return _legacy_ctl(obj, OPUS_SET_INBAND_FEC, value)
