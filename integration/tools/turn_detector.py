# integration/tools/turn_detector.py
"""tools.turn_detector shim — 원본은 integration/turn_detector.py.

pcm_turn_engine.py 가 `from tools.turn_detector import get_detector` 로 임포트한다.
원본 maya 레이아웃은 maya/tools/turn_detector.py 였지만 통합 환경에서는
integration/turn_detector.py 가 실제 구현이므로 이를 그대로 노출한다.
"""
from integration.turn_detector import TurnDetector, get_detector  # noqa: F401

__all__ = ["TurnDetector", "get_detector"]
