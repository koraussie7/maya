#!/usr/bin/env python3
"""etri 레퍼런스 → 마야 턴 판정/명령 게이트 비교 실측.

왜 이 스크립트가 있나
  /tmp/etri-vc(etri-voice-control)의 두 가지 설계를 우리 파이프라인에 그대로
  가져올 수 있는지 판단하려면, **숫자로 비교**해야 한다.
    1) `etri/voice/gate.py`  : 웨이크워드 없는 상시 청취에서 "지시문 vs 잡담"을
       가르는 규칙(영어 동사 목록 + 0.5 임계값)
    2) `etri/voice/vad.py`   : 적응형 노이즈 바닥 RMS 게이트 (energy 백엔드)

  한국어에서는 이 규칙들이 그대로 안 먹는다:
    - 게이트: `re.findall(r"[a-zA-Z']+")` 라 한국어 문장은 토큰 0개 → 항상 거부
    - VAD   : 마이크 PCM 을 float32 [-1,1] 로 기대하는데, 마야 쪽 엔진
              (pcm_turn_engine)은 Int16 바이트를 넘긴다 → 스케일이 32768배 다르다

  그래서 "한국어 어휘만 추가하면 되는지"를 먼저 확인한다. 여기서 하는 일:
    (a) etri 규칙을 **그대로** 한국어 문장에 적용 → 전부 거부되는지 확인
    (b) 간단한 한국어 동사 목록만 덧붙였을 때 판정이 갈리는지 확인
    (c) 우리 turn_detector 와 나란히 실행(모델 있으면)해서 오판 비교

실행
  python3 integration/etri_gate_ko_probe.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

ETRI = os.environ.get("ETRI_SRC", "/tmp/etri-vc")

# 마야 사용자 실제 발화에 가까운 것 + 잡담/백채널을 섞는다.
UTTERANCES = [
    # ── 지시문으로 봐야 하는 것
    ("cmd", "유튜브 열어줘"),
    ("cmd", "브라우저 열어"),
    ("cmd", "볼륨 30으로 줄여줘"),
    ("cmd", "다음 곡 재생해줘"),
    ("cmd", "메모장 실행해줘"),
    ("cmd", "화면 캡처해줘"),
    ("cmd", "검색해줘 강아지 사진"),
    ("cmd", "그만"),
    ("cmd", "확인"),
    # ── 잡담/백채널로 봐야 하는 것
    ("chat", "응"),
    ("chat", "아 그래"),
    ("chat", "오늘 좀 피곤하네"),
    ("chat", "지금 몇 시야?"),          # 질문: 명령 아님(마야는 답해야 함)
    ("chat", "너 이름이 뭐야?"),
    ("chat", "어 분위기 확 바뀌네"),
]

# 한국어 명령 동사(어간). 형태소 분석 없이 "포함" 규칙만 쓴다 — 최소 침습 검증용.
KO_VERBS = {
    "열어", "켜", "꺼", "줄여", "늘려", "키워", "틀어", "재생", "멈춰", "그만",
    "실행", "검색", "찾아", "보여", "캡처", "저장", "삭제", "닫아", "보내",
    "알려", "설정", "바꿔", "일시정지", "다음", "이전", "확인", "취소",
}
# 질문 어미: 명령이 아니라 답변이 필요한 발화
KO_QUESTION_TAIL = re.compile(r"(야|까|나요|가요|죠|니|냐|래|지)\s*[?.!]?\s*$")
KO_FILLER_ONLY = {"응", "어", "아", "네", "그래", "음", "아 그래", "그렇구나", "맞아"}


def load_etri_gate():
    path = os.path.join(ETRI, "etri", "voice", "gate.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("etri_gate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["etri_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def ko_gate(text: str) -> dict:
    """한국어 최소 규칙: 동사 포함 + 짧은 백채널/질문 제외."""
    t = (text or "").strip()
    if t in KO_FILLER_ONLY:
        return {"is_command": False, "why": "빈칸/백채널"}
    if KO_QUESTION_TAIL.search(t) and "줘" not in t and "해" not in t:
        return {"is_command": False, "why": "질문 어미"}
    verb = next((v for v in KO_VERBS if v in t), None)
    return {"is_command": verb is not None, "verb": verb,
            "why": "동사 포함" if verb else "동사 없음"}


def main() -> int:
    gate_mod = load_etri_gate()
    if gate_mod is None:
        print(f"etri 소스를 찾지 못했습니다: {ETRI} (ETRI_SRC 로 지정)")
        return 1
    heur = getattr(gate_mod, "HeuristicGate", None) or getattr(gate_mod, "CommandGate", None)
    if heur is None:
        print("etri gate.py 에 CommandGate 를 찾지 못했습니다.")
        return 1
    etri = heur()

    print(f"{'정답':<5}{'etri 원본':<10}{'한국어 규칙':<10}발화")
    print("-" * 70)
    wrong_etri = wrong_ko = 0
    rows = []
    for kind, text in UTTERANCES:
        j = etri.judge(text)
        k = ko_gate(text)
        want = kind == "cmd"
        wrong_etri += int(j.is_command != want)
        wrong_ko += int(k["is_command"] != want)
        rows.append({"kind": kind, "text": text,
                     "etri": {"is_command": j.is_command, "conf": j.confidence,
                              "verb": j.verb},
                     "ko": k})
        print(f"{kind:<5}{str(j.is_command):<10}{str(k['is_command']):<10}{text}")

    n = len(UTTERANCES)
    print("-" * 70)
    print(f"오판: etri 원본 {wrong_etri}/{n}, 한국어 규칙(동사목록만 추가) {wrong_ko}/{n}")

    # 우리 turn_detector 가 있으면 함께 돌려 오판을 비교한다(모델 없으면 건너뜀)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from turn_detector import get_detector
        d = get_detector()
        if d.available:
            fp = sum(1 for kind, text in UTTERANCES
                     if kind == "chat" and d.is_end(text))
            print(f"turn_detector(NAMO): 잡담 {len([1 for k, _ in UTTERANCES if k=='chat'])}건 중 "
                  f"'턴 종료' 판정 {fp}건 (높을수록 잡담을 끊는 쪽)")
        else:
            print("turn_detector(NAMO): 모델 미로드 → 건너뜀")
    except Exception as exc:  # noqa: BLE001
        print(f"turn_detector 비교 건너뜀: {type(exc).__name__}: {exc}")

    import json
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etri_gate_ko_probe.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"utterances": rows, "etri_wrong": wrong_etri, "ko_wrong": wrong_ko,
                   "etri_source": ETRI}, f, ensure_ascii=False, indent=2)
    print(f"보고서: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
