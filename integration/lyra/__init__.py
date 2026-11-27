"""Lyra: 오픈소스 클로닝 음성 옵션 모듈.

기존 Maya TTS 파이프라인(voxcpm/piper/say)은 변경하지 않고, **보조 옵션**으로만
추가되는 모듈입니다. Supertonic 3 (C 분기) 를 백엔드로 사용합니다.

사용법
------
1) 레퍼런스 음성 캡처 (옵션): ``python -m integration.lyra.record_reference``
   → ``voices/<name>.json`` 으로 보이스 스타일 저장
2) 합성:
       from integration.lyra.lyra_tts import LyraTTS
       tts = LyraTTS()                          # 기본 베이스 보이스(F1)
       wav, sr = tts.synthesize("안녕하세요")   # numpy float32 + sr

핵심 설계
---------
* ``LocalTTS`` 와 직접적인 의존성 없음. ``maya-ui/backend/app/tts.py`` 의
  ``synthesize_lyra()`` 헬퍼가 이 모듈을 호출 (기존 ``synthesize()`` 는 무변경).
* venv 분리 가능: ``SUPERTONIC_PYTHON`` 환경변수로 다른 인터프리터 경로 지정.
"""

from .lyra_tts import LyraTTS, LYRA_BASE_VOICE, LYRA_DEFAULT_SPEED

__all__ = ["LyraTTS", "LYRA_BASE_VOICE", "LYRA_DEFAULT_SPEED"]
