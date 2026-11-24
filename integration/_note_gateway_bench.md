#!/usr/bin/env python3
"""자체 게이트웨이(privseai.com/privseai/llm) 대화 경로 측정.

왜 이 스크립트인가
2026-11-24 서버 실사로 확인한 사실:
· keirouter(:20128)의 `auto/best-fast` 는 **업스트림 무료 제공자가 전부 실패**하면
400 `Model is unavailable` (+12개 대안 목록) 을 돌려준다.
· 즉 "별칭이 있다"와 "지금 응답한다"는 다르다. 대화 경로는 **지금 살아 있는 별칭**을
골라야 하고, 그 판단은 실측으로만 된다.
이 스크립트는 후보 별칭을 같은 질문으로 돌려 TTFT/총시간/한국어율을 비교한다.

실행

# 키가 필요 없다(서버 실사: 키 없이 /v1/models·/v1/chat/completions 모두 200)

.venv-voicemem/bin/python integration/llm_chat_bench.py \
 --base https://privseai.com/privseai/llm/v1 --models auto/best-fast auto/best-chat
"""
