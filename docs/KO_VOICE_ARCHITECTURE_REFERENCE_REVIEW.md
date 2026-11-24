# 한국어 음성 제어 아키텍처 — 레퍼런스 2건 비교와 마야 이식 판단

**날짜**: 2026-11-24
**상태**: 리서치 + 실측 1건 완료, 코드 이식은 보류 (근거는 6절)

이 문서는 두 레퍼런스 저장소를 읽고, 마야(현재 파이프라인)에 **무엇을 어떤 순서로**
가져올지 판단한 기록이다. 인용은 모두 그 저장소의 실제 파일/라인에서 확인한 내용이다.

| 레포                                                                                      | 성격                                                                                   | 라이선스                                           | 플랫폼                   |
| ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------- | ------------------------ |
| [prajazz66/etri-voice-control](https://github.com/prajazz66/etri-voice-control)           | 음성으로 **데스크톱 조작** (상시 청취, AT-SPI+OCR, Laya grounding)                     | **Proprietary, All Rights Reserved** — 열람만 허용 | Linux/X11 (GNOME AT-SPI) |
| [Polymorph0us/laya-voice-assistant](https://github.com/Polymorph0us/laya-voice-assistant) | **대화형 비서** (Dual-Brain: S1 반사 <5ms + S2 ReAct), 메모리/스케줄러/도구 레지스트리 | MIT                                                | Windows 10/11            |

---

## 1. 먼저 정해야 할 것: 두 레포는 서로 다른 문제를 푼다

```
etri   : "말 → 화면 요소 클릭"      (액션 정확도, 좌표를 음성에서 만들지 않는다)
laya-va: "말 → 대화 + 도구 실행"    (지연 분리, ReAct, 권한 등급, 장기기억)
```

마야는 **대화형 음성 비서**이므로 구조적으로 가까운 쪽은 `laya-voice-assistant` 이고,
etri 는 "행동(조작) 레이어"가 필요해질 때 참고할 부품이다. 한 번에 다 가져오면
윈도우/리눅스 전용 의존성(pycaw, xdotool, AT-SPI)이 들어와 맥 환경에서 안 돌아간다.

---

## 2. etri — 가져올 만한 부품 3개

### 2.1 `etri/voice/gate.py` — 웨이크워드 없는 명령 게이트

- 설계: 영어 **동사 목록**(`COMMAND_VERBS`) + 선택적 호칭("etri", "airy")으로
  지시문/잡담을 가른다. 임계값 0.5, **오탐(잡담을 명령으로) 쪽이 더 위험**하다고
  보고 보수적으로 판정한다 (파일 docstring).
- 판정 규칙(실제 코드): `_leading_verb()` 는 앞 5개 토큰에서 동사를 찾고,
  호칭 +0.55, 동사 +0.55, 8토큰 이하면 +0.1.

**실측 (`integration/etri_gate_ko_probe.py`, 15문장)**

| 규칙                    | 오판                                                                              |
| ----------------------- | --------------------------------------------------------------------------------- |
| etri 원본 그대로        | **9/15** (한국어 명령 9건 전부 거부 — 토큰 정규식이 `[a-zA-Z']+` 라 한글은 0토큰) |
| 한국어 동사 목록만 추가 | **0/15**                                                                          |

해석: 알고리즘은 훌륭하지만 **어휘가 영어/동사 선행 언어에 맞춰져 있다**.
"볼륨 30으로 줄여줘"(동사가 뒤)처럼 한국어는 동사가 문말에 오는 경우가 많아
동사 목록만으로는 오래 못 버틴다. 이식하려면 형태소/동사 형태("줘", "해", "어" 어미)
기반으로 새로 설계해야 한다 → **규칙 이식보다 우리 `turn_detector`(NAMO)로
"명령 vs 잡담"을 분류하는 쪽이 맞다.** 동사 목록은 보조 신호로만 쓰는 것이 적절하다.

### 2.2 `etri/voice/vad.py` — 적응형 노이즈 게이트

- 30ms 프레임, webrtcvad 우선 → 없으면 **RMS vs 적응형 노이즈 바닥**(`0.95*noise+0.05*rms`).
- 우리 `pcm_turn_engine.py` 의 턴 판정은 고정 `rms_threshold=250`(Int16 스케일) +
  silence 시간 판정이다. etri 쪽은 **주변 소음에 자동으로 적응**한다.
- 함정: etri VAD 는 **float32 [-1,1]** 입력을 기대한다. 우리 엔진은 Int16 바이트를
  넘기므로 그대로 붙이면 모든 프레임이 "무음"이 된다(스케일 32768배 차이).
  → 이식 시 `pcm/32768.0` 변환이 필수(우리 `test_turn_engine.py` 는 이미 이 방식).

**결론**: 적응형 노이즈 바닥은 **가져올 가치가 있는 작은 개선**이다. 단, webrtcvad
의존성은 선택(없으면 에너지 폴백) — 우리 Intel Mac 환경에서도 바로 동작한다.

### 2.3 `etri/orchestrator/confirm.py` + `grounding/scorer.py` — 확인/차단 정책

- 파괴적 동작(delete/buy/send/log out…)은 **신뢰도와 무관하게 확인**,
  신뢰도 < 0.4 는 BLOCK, ≥ 0.75 는 AUTO (실제 상수 확인).
- grounding 신뢰도 = `0.7*점수 + 0.3*min(1, 마진*3)` — **2등과의 차이**를 신뢰도에 반영.
- 마야 대화 파이프라인에는 "파괴적 도구 실행"이 아직 없다. **행동 레이어를 붙일 때**
  이 정책을 그대로 참고하면 된다(특히 "애매하면 실행하지 않는다").

> ⚠️ **라이선스**: etri 는 "viewing only, no use/copy/fork/reverse-engineer" 를 명시한다.
> 코드를 복사·이식하는 것은 라이선스 위반이다. 위 내용은 **아이디어 수준 참고**로만
> 쓰고, 우리 쪽은 자체 구현으로 작성해야 한다(본 문서의 VAD 개선안도 그 원칙).

---

## 3. laya-voice-assistant — 구조적으로 마야와 같은 결

### 3.1 Dual-Brain 라우팅 (`src/core/confidence.py`, `decision.py`)

| 신호      | 마야(현재)                          | laya-va                                                      |
| --------- | ----------------------------------- | ------------------------------------------------------------ |
| 빠른 경로 | piper TTS + 고정 응답, 턴 판정 2.4s | S1: <5ms 규칙/하드웨어 (볼륨, 미디어 키, 앱 실행)            |
| 느린 경로 | Ollama LLM(3s) + 문장 스트리밍 TTS  | S2: ReAct + 도구 루프                                        |
| 라우팅    | 없음(항상 LLM)                      | `intent_confidence` 임계값 high/medium/min + `needs_system2` |

**마야에 유효한 부분**: "지금 이 발화를 LLM 에 보내야 하나?" 를 먼저 정하는 라우터.
예) "볼륨 줄여", "그만", "다음 곡" 같은 고빈도 명령을 LLM 3초 뒤로 보내는 건 순수 손해다.
다만 laya-va 의 `DecisionEngine` 은 **실제 Laya 모델 호출부가 비어 있는 스텁**이다
(`predict()` 가 항상 fallback). 그래서 "코드"보다 **정책(임계값/라우팅 표)** 이 참고 대상.

### 3.2 권한 등급 (`src/core/permissions.py`)

`SAFE` / `CONFIRMATION_REQUIRED` / `DANGEROUS` 3단계 + "SAFE 는 동적 확인 신호에
절대 승격되지 않는다" 는 명시적 정책. 마야에 도구 실행을 붙일 때 그대로 채택할 만함.

### 3.3 메모리 (`src/core/memory.py`)

key/value SQLite (`set_fact(key, value, category)`) — 단순하고 견고하지만
**의미 검색이 없다**. 마야의 `voicemem`(E5 벡터 + 사실 추출 + 충돌 판정)이 훨씬 앞서 있다.
→ laya-va 쪽 메모리는 **참고하지 않는다** (우리 쪽이 상위호환).

### 3.4 ReAct 도구 루프 (`src/llm/provider.py`, `src/core/agent.py`)

- Ollama 호출에 `keep_alive: 0`, `timeout: 45s`, 도구 스키마 **동적 프루닝**.
  → 우리 `IntegrationResults` 15-7 "LLM 3초" 문제와 직결: 45s 타임아웃/프루닝을
  참고해 추출기·플래너 호출에 상한을 두는 패턴.
- ⚠️ `keep_alive: 0` 은 **호출이 끝나면 모델을 메모리에서 내린다**는 뜻이다.
  우리 실측에서는 이게 곧 "매 호출 재로딩" → 100~190초 지연의 원인이 됐다
  (`integration/vm_probe_extract.json`). 이 설정은 **복사하면 안 된다**.

---

## 4. 마야 파이프라인에 실제로 생긴 문제와 이번 실측

이번 세션의 실측 결과(`integration/vm_probe_extract.json`, `vm_ko_report.json`):

| 관찰             | 수치                                                                                 | 원인(코드로 확인)                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| 추출 1회 지연    | qwen3:1.7b **182s** → APITimeoutError                                                | 사고 모델 + 추출 클라이언트 기본 `timeout=60s`, `max_tokens` 미지정                                  |
| 절단(max 512) 시 | gemma3:4b 188s·3건, qwen2.5-coder 144s·3건, qwen3:1.7b **0건**(JSON 미완성)          | 작은 모델에서도 수 분 — **호출당 1회 추출로는 실시간 대화 불가**                                     |
| 저장 언어        | 추출기가 **영어로 번역한 사실을 저장** (`User is a vegetarian`) | 유저 프롬프트의 언어 블록(`mem0_additive_prompt_build.py:98-116`)이 `use_input_language=False` 일 때만 "in English" — 값 확인 필요 |
| 충돌 판정 | 매 ingest 마다 `401 Incorrect API key provided: ollama` (로그·결과에는 안 드러남) | `ConflictResolver` 가 `OpenAI()` 에 base_url 을 안 넘김 → 실제 OpenAI 서버 |
| 그래프 쓰기 | `model "text-embedding-3-small" not found` | slot→entity 임베딩이 로컬 임베더 셰임 밖의 기본 OpenAI 임베더를 직접 씀 |
| DB 오류          | `readonly database` / `no such table: session_state`                                 | room 디렉터리를 qdrant 가 잡고 있는 상태에서 `rmtree` → 지워진 inode 에 계속 쓰기                    |

정리: **한국어 기억 품질의 병목은 검색이 아니라 "동기 LLM 추출"** 이다.
검색(E5)은 한국어 질의로도 동작하고(로컬 모델), 이번에 시간/어휘 보너스에 한국어를
추가했다(`integration/vm_ko_search_patch.py`, 오분류: "몇 시"→duration, "어디"→date 확인).

### 4.1 언어 규칙 충돌의 정확한 위치 (정정)

`voicemem/leftbrain/mem0_additive_prompt_build.py:98-116` 을 보면 `use_input_language`
분기가 있다. **True 면 "원문 언어 그대로"**, False 면 "CRITICAL: … in English" 이다.
즉 저장이 영어로 나온 것은 프롬프트에 영어 규칙이 고정으로 박혀서가 아니라,
`use_input_language` 가 False 로 잡혔기 때문이다 (`OpenAIAdditiveExtractorConfig.use_input_language`
기본값 True — 어떤 경로가 False 로 만드는지는 아직 미확정).
→ `vm_ko_extractor_patch` 의 언어 규칙 교체는 **그 자체로는 필요 없을 수 있다.**
먼저 `use_input_language` 가 실제로 무엇으로 들어가는지 찍어 보고, False 라면
설정만 바로잡는 것이 올바른 수정이다(패치는 그게 안 될 때의 안전망으로만 유지).

### 4.2 로컬 게이트웨이를 쓰는 파이프라인에서 실제로 터진 것

| 오류 | 발생 위치 | 원인 |
|------|-----------|------|
| `401 Incorrect API key provided: ollama` | `ConflictResolver.resolve()` | `extract_facts_openai.py:544-555` 가 `OpenAI(**kw)` 에 **base_url 을 안 넘긴다** → 기본 OpenAI 서버로 감 |
| `model "text-embedding-3-small" not found` | `LeftBrain.write()` → slot→entity 그래프 | Ollama 엔드포인트로 임베딩을 요청. 셰임은 저장소 임베더만 교체하고 이 경로는 못 덮음 |

두 건 모두 **대화는 계속되지만 기억 품질만 조용히 떨어지는** 종류다(예외가 잡혀 로그 한 줄로 끝남).
`vm_ko_extractor_patch` 는 이제 모듈 전역 `OpenAI` 심볼까지 감싸 **base_url 누락을 전역에서
보정**한다(명시적 base_url 은 건드리지 않음 — 확인: `filled: …11434/v1/`, `explicit kept: …example.com/v1/`).
임베딩 쪽은 셰임 범위를 넓혀야 별도 대응이 필요하다.

### 이번 세션에서 만든/고친 파일

| 파일                                   | 역할                                                                                                                                   |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `integration/vm_ko_check.py`           | 측정 스크립트 수정: room 을 생성 **전**에 준비, `async_facts=False`, 사실 0건이면 실패 집계, 실제 저장소에서 원문 확인, DB 무결성 체크 |
| `integration/vm_probe_extract.py`      | 추출 단계 단독 측정(모델별 지연/JSON 완성도/한국어 유지)                                                                               |
| `integration/vm_ko_extractor_patch.py` | 런타임 패치: 영어 번역 지시 제거, 호출 상한(max_tokens/timeout)                                                                        |
| `integration/vm_ko_search_patch.py`    | 검색 재정렬 정규식에 한국어 시간/어휘 추가                                                                                             |
| `integration/etri_gate_ko_probe.py`    | etri 명령 게이트를 한국어로 돌려 오판률 측정(9/15 → 0/15)                                                                              |

---

## 5. 우선순위 제안 (지연 → 기억 → 행동)

| 순위  | 작업                                                                                                                         | 근거                                        |
| ----- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| **1** | 사실 추출을 **비대화 경로로 분리** — 응답 생성과 병렬, 또는 `ingest(async_facts=True)` + 큐. 대화 응답은 LLM 응답만 기다린다 | 추출 1회 144~188s (실측)                    |
| **2** | 추출 모델을 **비사고·한국어 강한 모델**로 고정 (gemma3:4b 이상). qwen3 계열은 `think:false` 명시                             | qwen3:1.7b 절단 상태에서 JSON 미완성        |
| **3** | `vm_ko_extractor_patch` 의 언어 규칙을 **제품 경로**로 승격 (site-packages 대신 설정/래퍼)                                   | 영어 번역 블록이 유저 메시지에 있음         |
| **4** | VAD 에 적응형 노이즈 바닥(RMS) 도입 — etri 방식 자체 구현, Int16→float 변환 필수                                             | 고정 임계 250 은 소음 환경에서 취약         |
| **5** | (행동 레이어를 붙일 때) 3단계 권한 + "애매하면 실행 금지" + 2등 마진 기반 신뢰도                                             | etri `confirm.py`, laya-va `permissions.py` |

---

## 6. 왜 지금 코드를 더 이식하지 않았나

1. **라이선스**: etri 는 복사를 금지한다. 아이디어만 참고 가능.
2. **플랫폼**: etri 실행 계층(Linux/AT-SPI/xdotool), laya-va 실행 계층(Windows WASAPI)이
   모두 맥에서 동작하지 않는다. 가져오면 동작하지 않는 코드만 늘어난다.
3. **우리 쪽 실측이 더 정확한 병목을 가리킨다**: 144~188초 추출 지연은 레포 교체로
   풀리는 문제가 아니라 **호출 배치/모델 선택** 문제다(5절 1~2순위).
4. **메모리**: laya-va 의 KV 메모리는 voicemem 보다 단순해 교체 가치가 없다.

즉 다음 작업은 "레포 이식"이 아니라 **추출 비동기화 + 모델 고정 + 언어 규칙 제품화**다.
