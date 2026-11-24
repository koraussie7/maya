# 마야 음성 작업 현황 (2026-11-24 2차 측정)

**측정 대상**: 한국어 기억(voicemem) ingest/search — gemma3:4b, `leftbrain_only`, 로컬 E5 임베딩
**측정 스크립트**: `integration/vm_ko_step.py` (단계별 실행, 결과 즉시 `vm_ko_step_state.json` 저장)

## 1. 실행별 실측 (2026-11-24)

| 실행 | 문장 | 소요 | 결과 |
|------|------|------|------|
| run10 (end-to-end) | ①채식주의자 | **902.8s** | facts 2건 — 단, 한국어+영어 중복 |
| run11 (end-to-end) | ①채식주의자 | 244s | 저장 성공 |
| run11 | ②김민수/서울 | **601s** | 저장 성공 |
| run11 | ③판교/출근 9시 | 822s | **실패** `no such table: session_state` |
| run11 | search 3건 | — | **전부 실패** `no such table: touched_refs` |
| step (단독) | ②김민수/서울 (재시도) | 243s | 성공 |
| step (단독) | search 3건 | 18.4s + 71ms + 64ms | **top1 정답 1/3** |

- ingest 1문장 ≈ **4~15분**(편차 큼, 모델/캐시 상태 의존). 한 프로세스에 3문장을 몰아넣으면
  뒤로 갈수록 DB 스키마가 어긋나며 실패한다.
- search 는 71~190ms 수준(첫 호출만 모델 로딩 18.4s).

## 2. 저장된 기억 (room `.vm-ko-test`)

```
3ad0427b  User: 나는 채식주의자이고 견과류에 알레르기가 있어요.      ← 한국어 원문(좋음)
4fed896f  User is a vegetarian and has an allergy to nuts          ← 영어 번역
f9cb7f0d  User is a vegetarian and has an allergy to nuts          ← 같은 영어 문장 중복
a9258e5b  User is a vegetarian and has an allergy to nuts          ← 같은 영어 문장 중복
```

- **영어 번역 3중 중복**: 매 ingest 마다 `use_input_language=False` 규칙이 걸려 영어로 번역되고,
  충돌 판정(ConflictResolver)이 base_url 누락으로 401 → 503 이므로 ADD-only 로 떨어져
  **같은 문장이 계속 쌓인다**.
- ②김민수/서울 ingest 는 "성공"으로 기록됐으나 memories 테이블에 남은 것은 채식주의자 4건뿐이다.
  → **벡터(mem0)와 sqlite(메타)가 서로 다른 곳에 쓰이는 상태**. 저장 성공 판정(`memory_ids`)이
  실제 검색 가능성과 다르다.
- ③(판교/출근)은 저장 자체가 실패했다.

## 3. 언어 문제의 진짜 원인 (이번에 확정)

`use_input_language=True` 로 강제하고 **1문장만** 추출한 결과:

```
LANG BLOCK >>> ## Language Requirement | Write each memory in the SAME language the utterance was spoken in. | Korean input stays Korean (한국어 그대로). …
system has SAME-language rule: True
extract 284.0s
FACT: 用户表示自己是素食主义者，并且对坚果过敏。        ← 중국어
FACT: 用户正在表达自己的饮食偏好和健康限制。            ← 중국어(추론 문장, 노이즈)
```

→ **원인은 프롬프트가 아니라 모델**이다. 규칙이 완벽히 맞춰져 있어도 **gemma3:4b 가
한국어를 중국어로 출력**한다(한국어학습 데이터 부족 + 언어 판별 실패).

| 모델 | 한국어 출력 | 시간 | 비고 |
|------|------------|------|------|
| gemma3:4b | ❌ 중국어 | 284s | 로컬 4B 는 한국어 생성 불안정 |
| qwen3:1.7b | ❌ JSON 미완성(사고 토큰) | 182s+ | 느리고 형식이 깨짐 |
| qwen2.5-coder:1.5b | ⚠️ 형식 OK | 144s | 한국어 품질 미검증 |

**결론**: "로컬 소형 모델로 무료 한국어 기억" 조합은 실측상 성립하지 않는다.
한국어 사실 추출은 (a) 한국어 특화 모델(예: EXAONE/Gemma-ko 계열) 로컬, 또는
(b) 상위 API 모델 중 하나를 골라야 한다. 그 전까지는 **원문 그대로 저장하는 경로**
(`VOICEMEM_INGEST_RAW_FALLBACK=1`)로 검색 품질부터 확보하는 것이 합리적 — E5 는
한국어 질의·한국어 원문 사이에서 잘 동작한다.

## 4. 검색 결과 (저장된 4건 기준)

| 질의 | 결과 | 정답 여부 |
|------|------|-----------|
| 내 식이 제한이 뭐야? | 채식주의자(한국어) + 영어 번역 2건 | ✅ |
| 내가 사는 곳이 어디라고 했지? | 채식주의자 문장들만 | ❌ (서울 기억이 DB 에 없음) |
| 출근 시간이 몇 시였지? | 채식주의자 문장들만 | ❌ (판교 기억이 ingest 실패) |

> **정정**: `vm_ko_step.py --step search` 재실행으로 top1 **1/3** 이었으나,
> 2번 완료된 뒤의 장부(`vm_ko_step_state.json`)에는 `top1_ok` 가 3건 모두 True 로 기록됐다.
> 같은 room·같은 질의에서 결과가 갈리므로 **이 room 의 수치는 신뢰하지 않는다**
> (벡터와 sqlite 가 어긋난 room — 2절). 대신 6.1절의 **새 room 원문 저장 측정(4/4)** 을 기준으로 삼는다.

→ **검색 실패 2건은 recall 문제가 아니라 그 기억이 애초에 저장되지 않은 문제**다.
`top1 정답 1/3` 은 검색 알고리즘 성능으로 읽으면 안 된다.

## 5. 환경 변경

- `huggingface_hub` 2.4.0 → **0.36.2** 다운그레이드 (transformers 가 `<1.0` 요구, 이전엔 E5 로딩이
  ImportError 로 죽어 검색이 전부 실패했다). 변경 후 E5 정상.
- `vm_ko_extractor_patch`: 프롬프트 언어 규칙 교체(이중 안전장치) + `use_input_language=True` 강제
  + `max_tokens`/`timeout` 상한 + 전역 `OpenAI` base_url 보정.

## 6. 다음 작업

### 6.1 ✅ 완료 — 원문 저장 경로로 한국어 검색 4/4 (2026-11-24, `integration/vm_ko_raw_memory.py`)

추출 LLM 을 건너뛰고 `repo.append_extracted()` 에 원문을 넣어 저장한 뒤 질의한 결과:

| 질의 | top1 | 점수 | 1위 기억 | 시간 |
|------|------|------|----------|------|
| 내 식이 제한이 뭐야? | ✅ | 0.822 | 나는 채식주의자이고 견과류에 알레르기가 있어요. | 841ms(첫 호출) |
| 내가 사는 곳이 어디라고 했지? | ✅ | 0.866 | 내 이름은 김민수이고 서울에 살아요. | 55ms |
| 출근 시간이 몇 시였지? | ✅ | 0.849 | 회사는 판교에 있고, 아침 9시에 출근해요. | 57ms |
| 내 이름이 뭐라고 했지? | ✅ | 0.845 | 내 이름은 김민수이고 서울에 살아요. | 55ms |

**top1 4/4, 2위와의 격차도 0.04~0.08 로 일관**됐다. 시간/어휘 보너스(`time_boost`)는 발동하지 않았고
**순수 코사인(base)만으로 정답**이다 → 한국어 검색 실패는 전부 "기억이 저장되지 않은" 문제이고,
E5(로컬, 무료) 자체는 한국어에서 충분히 쓸만하다. 이 시점부터 **검색은 해결 과제에서 빠진다.**

> 저장 3건은 **LLM 호출 0회**로 끝났다(추출을 안 하므로). 첫 검색만 841ms(E5 로딩), 이후 55~57ms.
> 하나만 지정 빠뜨린 함정: `OPENAI_MODEL` 을 안 주면 기본값 `gpt-4o-mini` 로 404 가 계속 나
> (저장 자체는 진행되지만 매번 수십 초 끌린다) → 스크립트가 명시 설정한다.

### 6.2 남은 과제

1. **한국어 추출 모델 결정 — 로컬 불가** (`MAYA_VOICE_KO_MODEL_COMPARE.md`).
   형식이 맞는 qwen2.5-coder:1.5b 는 사실 1건(커버리지 3/9), 품질이 맞는 llama3.1:8b 는 5분+.
   대화 응답까지 보니 추출과 같은 결론이다(로컬 qwen2.5-coder 대화 실측:
   첫 토큰 0.84~12.4s 편차 15배, 3번째 답변은 질문과 무관한 내용을 창작).
   → 운영은 **원문 저장 + E5 검색** 유지, 추출이 필요하면 클라우드/API 경로.
2. **room 손상 재발 방지**: ingest 를 문장 단위 프로세스로 분리(`vm_ko_step.py`),
   옛 room 은 **이동 금지(복사만)** — qdrant 상대경로 때문에 이동하면 벡터가 깨진다.
3. **임베딩 경로 오류**: `model "text-embedding-3-small" not found`(slot→entity) 는 셰임 확장 필요.
