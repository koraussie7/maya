# integration/tools/__init__.py
"""tools/ 패키지 shim — 통합본에서 NAMO 모듈을 찾을 수 있게 한다.

pcm_turn_engine.py 가 `from tools.turn_detector import get_detector` 로 임포트하지만
원본 maya 레이아웃은 `maya/tools/turn_detector.py` 이지 integration/ 안이 아니다.
따라서 통합 환경에서는 이 shim 이 `integration.turn_detector` 를 그대로 노출한다.
"""
