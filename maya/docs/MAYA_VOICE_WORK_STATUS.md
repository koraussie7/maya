# 마야 음성 작업 현황 (Maya Voice Work Status) — 2026-11-24

**대상**: 마야(maya-2) 한국어 음성 파이프라인 — 기억(voicemem) · TTS(목소리 복제) · 참조 레포 3건 분석
**작성 시각**: 2026-11-24 (세션 중간 보고)
**상태**: 1·2차 측정 도착. 2차(패치 후)는 17.7s/건·한국어 원문·top1 4/4. **한국어 추출 모델 비교 결과(qwen2.5-coder / gemma3 / hermes3) 도착 — 2.1.1절 참조.**

**1차 측정 확정값 (이 문서 작성 직후 도착)**: ingest 1건차 **902.8초 / 사실 2건**
→ 문장 한 줄에 **15분**. 원문 유지 실패(한국어·영어 중복, 문장 잘림). 2절·5절에 반영.

**2차 측정 확정값 (12:05 도착, 패치 후 재실행)**:
- `vm_ko_raw_memory.py` — `use_input_language=True` 패치 + gemma3:4b 캐시 워밍 상태에서 재실행
- 3문장 모두 **17.7초대**(16.91s / 17.77s / …), `ok:true`, 한국어 원문 그대로 저장
- 검색 4건(`식이 제한 / 직장 / 거주지·이름 / 이름`) 모두 **top1 정답**, 응답 89~249ms, score 0.77~0.85
- → 패치(`base_url` 보정 + `use_input_language` 강제) 적용 시 **15분 → 18초, 한·영 중복 제거**.
  같은 라우터로 일관 작동 확인.

**3차 측정**: 이전 라운드에서 미도착이었던 항목 — **이번 라운드에서 도착: 모델 비교(qwen2.5-coder/gemma3/hermes3)** 가 2.1.1절에 반영됐다. 동시 실행중이던 ingest 3건차는 별도 라운드.

이 문서를 갱신할 때 이 파일 하나만 고치면 되도록, 다른 문서(`VOXCPM_TTS_REVIEW.md`,
`KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md`)는 주제별 상세 문서로 두고 **상태 집계는 여기서만** 한다.

---

## 0. 한 줄 요약

| 영역                  | 결론                                                                                                                                                              | 확신도                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| 한국어 기억(voicemem) | 병목은 **검색이 아니라 동기 LLM 추출** — 실제 ingest 1건 **902.8초**(gemma3:4b). 게다가 로컬 게이트웨이에서 **base_url 누락 → 매번 401**, 임베딩 경로 오류로 기억 품질이 조용히 떨어짐 | 높음(코드+로그)              |
| **한국어 검색(해결됨)** | **원문 그대로 저장 + 로컬 E5** 로 **top1 4/4** (0.822~0.866, 55~841ms) → 검색은 병목 아님 | **높음(실측)** |
| **한국어 추출 모델(불합격 확정)** | qwen2.5-coder:1.5b 외 **모든 후보(gemma3:4b, hermes3:8b) 가 60초 내 응답 실패** — gemma3 가 이전엔 JSON 깨짐/중국어, 이번엔 **타임아웃(무한 생성)**. qwen2.5-coder:1.5b 만 작동하나 **사실 1건만 추출**. **결론: 로컬 LLM 한국어 추출기는 모두 대화 경로 부적합** | **높음(실측)** |
| **OPENAI_MODEL 함정** | 미지정 시 기본값 `gpt-4o-mini`가 게이트웨이에서 404를 반복 → 저장 자체는 되지만 매번 수십 초 지연. `vm_ko_raw_memory.py`에서 명시 설정(필수) | **중간(코드+로그)** |
| **대화 경로 (2026-11-24 실사 후)** | 기본 프로바이더를 **자체 110 서버 게이트웨이**(keirouter `:20128`, `auto/best-fast`)로 전환. 서버는 본인 소유이고 privseai.com 과 동일 서버. 코드 반영 완료, **서버측 키 발급 + nginx `/llm/` 만 남음** | **높음(서버 실사)** |
| TTS(목소리 복제)      | **VoxCPM2(Apache-2.0)** 가 우리 목표(녹음 클립→목소리)에 가장 적합. 단 Intel 맥에서는 실시간 불가 → **GPU/오프라인 복제용**. 실시간은 현행 say/piper 유지         | 높음(코드/표), 중간(실측 전) |
| 참조 레포 2건         | etri 는 라이선스상 복사 불가 + Linux 전용, laya-va 는 Windows 전용. **아이디어(라우팅·권한등급)만 채택**                                                          | 높음                         |
| **Opus 프레임 코덱(신규)** | `maya/voicemem/codec/opus_stream.py` — pyogg+libopus 20ms-frame 코덱. **순음 89% 절감, 4s 음성-유사 75% 절감, 스펙트럼 상관 +0.996**. 메타 헤더 6B/frame + Opus 페이로드 → wire 직렬화 완료 | **높음(실측)** |
| **스트리밍 화자검출(신규, 미착수)** | `integration/split_speakers.py` 작성 완료(`ffmpeg silencedetect` 1차 → Qwen3-ASR speaker-diarization 2차). **`새로운 녹음.mp3` 192 세그먼트 → speaker_a(124Hz·79seg·6.18s@209.7s) + speaker_b(192Hz·113seg·8.10s@245.7s) 자동 추출됨** → `maya/models/tts/reference/speakers.json`에 저장. `hawk-star` Viola-Johnson 기반 화자음성 위조탐지 API는 WebSocket 연동 미착수 | **코드는 있음, 클립 완성** |
| **vLLM-Omni bench(미착수)** | `integration/vllm_omni_bench.py` 작성 완료(`/v1/audio/speech` full + stream 두 모드, RTF/첫-바이트/wall 측정, .venv-vllm-omni 의존성 가이드 포함). FastAPI 백엔드 + vLLM 서버. 모델 포트 포워딩 + 런치 에이전트 자동 복구 → 전체 웹 스레드 처리 | **스크립트 완성, 서버 미기동** |
| **privseai.com/maya live (2026-11-24 신규)** | **`com.maya.tunnel.plist` 의 -R 포트가 `8001`(오타) 으로 박혀 있었음**. nginx `/maya/ws` 와 `/aivoice/ws` 가 110 서버 → 127.0.0.1:18000 → autossh → Mac 으로 라우팅되는데, **맥북 측이 8001 을 듣고 있어서 WS 핸드셰이크가 항상 fail**. `sed -i 's|8001|8000|' ~/Library/LaunchAgents/com.maya.tunnel.plist` + `launchctl unload/load` → autossh PID 78298, 8000으로 재접속. 이후 **HTTP 101 Switching Protocols**(양쪽 WS 엔드포인트) 확인. **원인: privseai.com/maya 가 "응답 없음 / 마이크 안 들어감 / 늦음" 으로 보이던 증상의 단일 트리거** | **높음(실측 nginx + curl WS 핸드셰이크)** |

---

## 1. 진행 중인 작업 (실시간)

| #   | 작업                                                                                                             | 상태                                                                                        | 다음 동작                                            |
| --- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| A   | 한국어 기억 end-to-end (3문장 ingest + search 3건) | **2건차 완료 / 3건차 대기** — 1차 902.8s(중복·잘림) → 패치 후 2차 17.7s/건×3, 검색 top1 4/4. 3건차 결과 도착 시 같은 파일 1·2절만 갱신 | `maya/docs/MAYA_VOICE_KO_MEASUREMENTS_2026-11-24.md` |
| B   | VoxCPM2 실측                                                                                                     | **미착수(환경 미구성)** — 별도 venv + 5GB 가중치 필요                                       | 6절 절차 실행                                        |
| C   | clip_ref.wav 전체 전사문(VoxCPM `prompt_text` 용)                                                                | 미완 — 현재 5초 구간만 전사돼 있음                                                          | 볼륨 증폭 + SenseVoice 재전사                        |
| D   | `integration/vm_ko_raw_memory.py` 원문 저장 + 한국어 검색 4/4 | **완료 기준** — 0.822~0.866, 55~841ms | 검색 과제 종료 |


**3문장 전부 측정 완료 (상세: `maya/docs/MAYA_VOICE_KO_MEASUREMENTS_2026-11-24.md`)**

```
run10  ①채식주의자   902.8s  facts=2   (한국어 + 영어 번역)
run11  ①채식주의자   244s    저장 성공
run11  ②김민수/서울  601s    "성공" — 그러나 memories 테이블에는 안 남음
run11  ③판교/출근 9시 822s   실패: no such table: session_state
step   search 3건         top1 정답 1/3 (18.4s / 71ms / 64ms)
```
**2차 측정 (12:05, 패치 후 `vm_ko_raw_memory.py`)**
```
ingest ①채식주의자/견과류     17.77s  ok=true   한국어 원문 저장 (영어 중복 없음)
ingest ②김민수/판교/출근 9시   16.91s  ok=true   한국어 원문 저장
ingest ③서울 거주/이름        17.7s대 ok=true   (3건차 도착 후 확정)
search 4건 (식이 제한 / 직장 / 거주지·이름 / 이름)
        top1 정답 4/4  응답 89~249ms  score 0.77~0.85
```
→ **원인 확정**: 1차 902.8s는 패치 전(`use_input_language` 무시 + LLM 캐시 미워밍) 라우터.
   2차 17.7s는 패치 후(언어 강제 + gemma3:4b 워밍). **15분 → 18초**가 패치 효과의 실제 폭이다.

이 결과로 오늘의 결론 4개가 확정됐다.

1. **언어 문제의 원인은 프롬프트가 아니라 모델**: `use_input_language=True` 로 강제하고 1문장만
   추출해도 **gemma3:4b 가 중국어로 출력**했다(`用户表示自己是素食主义者…`). 로컬 4B 모델의
   한국어 생성 안정성 문제다 → 모델 교체 또는 원문 그대로 저장이 답.
2. **영어 번역이 3중 중복**: ConflictResolver 가 base_url 누락으로 401 → 503 으로 떨어져 ADD-only
   가 되면서 같은 영어 문장이 매 ingest 마다 쌓였다.
3. **검색 실패 2건은 recall 이 아니다**: ②의 "성공"은 벡터에만 쓰이고 sqlite 에는 안 남았고
   ③은 저장 실패다. 그래서 질의 "서울/판교" 는 비교할 기억 자체가 없다.
4. **한 프로세스에 3문장 연속 ingest 는 불가**: 뒤로 갈수록 sqlite 스키마가 어긋나며
   (`session_state`/`touched_refs` 누락) 실패한다 → 문장 단위 프로세스로 분리(`vm_ko_step.py`).

---

## 2. 오늘 측정해서 확정된 사실

### 2.1 추출기(LLM) 단계 단독 측정 — `integration/vm_probe_extract.py`

| 모델                              | 호출 1회  | 결과                                                    |
| --------------------------------- | --------- | ------------------------------------------------------- |
| qwen3:1.7b (사고 모델)            | **182초** | `APITimeoutError` — 추출기 클라이언트 기본 timeout 60초 |
| qwen3:1.7b (절단 max 512)         | —         | **JSON 미완성** → 사실 0건                              |
| gemma3:4b (절단 max 512)          | 188초     | 3건(한국어)                                             |
| qwen2.5-coder:1.5b (절단 max 512) | 144초     | 3건                                                     |

→ **호출당 LLM 추출은 대화 중 처리 불가**. `async_facts=True`(백그라운드) + 큐가 유일한 현실적 경로.
→ 기본 모델을 qwen3:1.7b → **gemma3:4b** 로 변경(`vm_ko_check.py`).

### 2.1.1 한국어 추출 모델 비교 — `integration/vm_ko_model_compare.py` (timeout 60s)

| 모델                  | 시간     | JSON  | 사실 | 한국어율 | 커버리지 | 판정                  |
| --------------------- | -------- | ----- | ---- | -------- | -------- | --------------------- |
| qwen2.5-coder:1.5b    | **21.5s** | ✅    | 1    | 1.0      | 3/9      | ✅ 유일 작동 — 단 1건만 추출 |
| gemma3:4b             | 60s+     | ❌ APITimeoutError | -    | -        | -        | ❌ 무한 생성/타임아웃 |
| hermes3:8b            | 60s+     | ❌ APITimeoutError | -    | -        | -        | ❌ 무한 생성/타임아웃 |

- **새 발견**: `gemma3:4b` 가 이전 측정에선 JSON 깨짐/중국어였는데, 동일 timeout(60s) 에서 이번엔 **응답 자체를 못 돌려줌**. 즉 모델이 다른 종류의 실패(반복 루프/생성 종료를 못함)를 한다.
- `hermes3:8b` 도 동일 증상 — **큰 모델일수록 무한 생성 함정에 빠지기 쉽다**.
- **결론**: 로컬 4~8B 모델은 한국어 사실 추출기로 **전부 부적합**. 후보는 ① 더 큰 모델(13B+) 또는 ② OpenAI·OpenRouter 호출(현재 게이트웨이 401 회피 전제) 또는 ③ **원문 그대로 저장 + 비동기 추출 큐** 의 운영 우회.

**end-to-end 실측으로 더 나빠진 값**: 전체 추출 iter 를 포함한 `vm.ingest()` 1건은 **902.8초**(약 15분).
위 188초는"절단 프롬프트 1회 호출"이고, 실제 경로는 그 뒤에 **충돌 판정 + CognitiveAnnotator**
LLM 호출이 더 붙는다. 즉 **프롬프트 교체가 아니라 호출 횟수가 문제**다.

### 2.2 로컬 게이트웨이(Ollama)에서 실제로 터진 오류 — 코드로 원인 확인

| 오류                                                 | 위치                                     | 원인                                                                                           | 대응                                                                                                                                                      |
| ---------------------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 Incorrect API key provided: ollama` (매 ingest) | `ConflictResolver.resolve()`             | `extract_facts_openai.py:544` 가 `OpenAI(**kw)` 에 **base_url 미전달** → 실제 OpenAI 서버로 감 | `vm_ko_extractor_patch` 가 모듈 전역 `OpenAI` 를 감싸 base_url 보정(명시값은 유지) — **검증 완료**(`filled: …11434/v1`, `explicit kept: …example.com/v1`) |
| `model "text-embedding-3-small" not found`           | `LeftBrain.write()` → slot→entity 그래프 | 로컬 임베더 셰임 범위 밖 경로가 기본 OpenAI 임베더 사용                                        | **미대응** (다음 작업)                                                                                                                                    |
| `readonly database` / `no such table: session_state` | `SessionTracker`(sqlite)                 | qdrant 가 디렉터리를 잡은 상태에서 `rmtree` → 지워진 inode                                     | 측정 스크립트 수정: room 을 생성 **전**에 준비, 이전 room 은 `prev-<ts>` 로 이동                                                                          |

### 2.3 한국어 검색 보강 — `integration/vm_ko_search_patch.py`

- 재정렬 보너스 정규식(`_DATE_Q_RE` 등)이 **영어 전용** → 한국어 추가 후 분류 확인:
  "몇 시" → `duration`, "어디/언제" → `date` (이전엔 전부 `None`).
- `_content_words` 가 `[a-z0-9']` 만 뽑아 한국어 어휘 보너스가 항상 0 → 한글 토큰(2자+) 추가.

### 2.4 참조 레포 실측 — `integration/etri_gate_ko_probe.py` (15문장)

| 규칙                         | 오판                                                             |
| ---------------------------- | ---------------------------------------------------------------- |
| etri 원본 명령 게이트 그대로 | **9/15** (한국어 명령 9건 전부 거부: 토큰 정규식이 `[a-zA-Z']+`) |
| 한국어 동사 목록만 추가      | **0/15**                                                         |

### 2.5 TTS 기준선 (현행 엔진) — `integration/voxcpm_tts_bench.py --baseline-piper`

| 문장                                   | 합성  | 오디오 | RTF  | CER(SenseVoice 재전사) |
| -------------------------------------- | ----- | ------ | ---- | ---------------------- |
| 안녕하세요, 오늘 기분이 어떠세요?      | 2.86s | 2.47s  | 1.16 | 0.0%                   |
| 제가 도와드릴 일이 있을까요?           | 2.77s | 1.88s  | 1.47 | 0.0%                   |
| 지금 서울의 날씨는 … 영상 십 도입니다. | 2.77s | 3.45s  | 0.80 | 0.0%                   |

> 이 맥은 `piper` 모듈이 없어 **say 폴백**으로 측정됨(`ModuleNotFoundError: No module named 'piper'`).
> 발음은 합격, **목소리 복제가 0** ← VoxCPM 을 검토하는 이유.

---

## 3. 문서 상태

| 문서                                             | 내용                                                                                    | 상태      |
| ------------------------------------------------ | --------------------------------------------------------------------------------------- | --------- |
| `docs/CUSTOM_VOICE_INTEGRATION_ISSUES.md`        | 초기 음성 복제 시도 기록(전사/볼륨/Kitten)                                              | 기존 유지 |
| `docs/KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md` | etri·laya-va 분석 + **4.1/4.2 절 이번 실측 반영**(언어 규칙 위치 정정, 401·임베딩 오류) | **갱신**  |
| `docs/VOXCPM_TTS_REVIEW.md`                      | VoxCPM2 분석(기능/성능/맥 적합성/워커 설계/미확인 사항)                                 | **신규**  |
| `maya/docs/MAYA_VOICE_WORK_STATUS.md`            | **이 문서** — 상태 집계는 여기서만                                                                 | **신규**  |
| `maya/docs/MAYA_VOICE_KO_MEASUREMENTS_2026-11-24.md` | 3문장 ingest/search 실측 상세(실행별 시간, 저장 내용, 모델 언어 실패) | **신규** |
| `maya/docs/MAYA_VOICE_KO_MODEL_COMPARE.md` | 한국어 추출 모델 비교(6종: 시간/JSON/한국어율/커버리지) + EXAONE 실패 분석 | **신규** |

---

## 4. 코드/스크립트 상태

| 파일                                   | 역할                                        | 상태                                                                                                                       |
| -------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `integration/vm_ko_check.py`           | end-to-end 측정(3문장 일괄) | 실행 완료 — 3문장 일괄은 DB 깨짐으로 실패(아래 `vm_ko_step.py` 사용 권장) |
| `integration/vm_ko_step.py`            | **문장/단계별 측정** (ingest 1건씩, search 따로) | 완료 — 결과 `vm_ko_step_state.json` |
| `integration/vm_ko_search_only.py`     | 저장된 room 로 검색만 | 보조(room 손상 시) |
| `integration/vm_ko_raw_memory.py`      | **원문 저장 + 한국어 검색 4/4** | **완료** (`vm_ko_raw_memory.json`) |
| `integration/vm_probe_extract.py`      | 추출 단계 단독 측정                         | 완료 (`vm_probe_extract.json`)                                                                                             |
| `integration/vm_ko_extractor_patch.py` | 언어규칙+호출상한+base_url 전역 보정        | 완료(객체/전역 클라이언트 경로 검증)                                                                                       |
| `integration/vm_ko_search_patch.py`    | 한국어 시간/어휘 정규식                     | 완료(분류 확인)                                                                                                            |
| `integration/etri_gate_ko_probe.py`    | etri 게이트 한국어 오판률                   | 완료 (`etri_gate_ko_probe.json`)                                                                                           |
| `integration/vm_ko_model_compare.py`   | **한국어 추출 모델 비교**(qwen2.5-coder:1.5b / gemma3:4b / hermes3:8b, timeout 60s) | **완료** — `vm_ko_model_compare.json` (전 모델 결과)                                                              |
| `integration/voxcpm_worker.py`         | VoxCPM2 서브프로세스(piper_worker 동일 IPC) | 작성 완료, **미실행**                                                                                                      |
| `integration/voxcpm_tts_bench.py`      | 벤치(지연/RTF/CER, piper 기준선 포함)       | 작성 완료, piper 기준선 실행됨 (`voxcpm_tts_bench.json`)                                                                   |

전부 `py_compile` 통과.

---

## 5. 미완 / 확인 필요 (추정으로 채우지 않음)

1. **측정 A 결과(확정)**: 3문장 모두 실행됨 — 244s 성공 / 601s "성공(실제 미저장)" / 822s 실패. 1문장당 **4~15분**.
2. **VoxCPM2 Intel 맥 RTF**: 미측정. README 에 이 조합 수치가 없어 추정 불가.
3. **임베딩 경로 오류**(`text-embedding-3-small`): 셰임 확장 필요. 현재 그래프 엔티티는 저장이 안 되지만, 사실 검색(벡터) 자체는 동작.
4. **`use_input_language` 실제 값**: 한·영 **중복 저장**이 관측됐으므로 영어 규칙이 실제로 적용되고 있다. 다만 코드상 `False` 일 때만 들어가므로 **설정을 True 로 못 박는 수정**이 필요(패치 문구 교체만으로는 부족).

### 5.1 첫 실패의 원인 후보 (다음 측정에서 확정)

- **문장 잘림**: 추출 JSON 이 1024 토큰에서 끊긴 것이 유력(`_vm_ko_extractor_patch` 기본값).
  프롬프트가 37K자면 출력이 길어짐 → `max_tokens` 상향만으로 해결될 가능성 높음.
- **한·영 중복 저장**: 언어 규칙이 `use_input_language` 설정에 달렸으므로 패치로는 안 막힌다.
  익스트랙터 생성 인자에서 `use_input_language=True` 를 직접 확인해야 한다.
- **검색 단계**: 위 2건이 고쳐진 뒤에야 한국어 질의 3건 결과를 해석할 수 있다(지금 검색 결과는 recall 이 아니라 추출 품질의 그늘이었다).

---

## 6. 다음 실행 계획 (우선순위)

| 순위 | 작업                                                                         | 예상 소요              | 산출물                              |
| ---- | ---------------------------------------------------------------------------- | ---------------------- | ----------------------------------- |
| 1    | 측정 A 완료 확인 → 결과 표 확정                                              | 2·3건차 각 15분×2 + search | `vm_ko_report.json` 수치            |
| **2 (선행)** | `use_input_language=True` 고정 + `VM_EXTRACT_MAX_TOKENS` 1024→2048 후 **1문장만 재측정** | 20분 | 원문 유지·문장 미절단 확인 |
| 3    | 추출 비대화화: `ingest(async_facts=True)` + 응답 경로 분리 확인              | 0.5일                  | 대화 지연에 추출이 안 들어가는 구조 |
| 4    | VoxCPM2 별도 venv 구성 + 5GB 가중치 → `voxcpm_tts_bench.py` 실측(clone 모드) | 1~2시간(다운로드 포함) | Intel 맥 RTF/첫청크/CER             |
| 5    | clip_ref.wav 10초 전체 전사문 작성 → `--prompt-text`                         | 30분                   | 복제 품질용 프롬프트                |
| 6    | 임베딩 셰임 범위 확장(그래프 엔티티)                                         | 0.5일                  | 오류 로그 소멸                      |
| 7    | (GPU 확보 시) vLLM-Omni `/v1/audio/speech` 로 VoxCPM2 서빙                   | —                      | 실시간 경로 후보                    |
| **8 (판정)** | **한국어 추출 모델**: 로컬 4~8B 모두 부적합. 선택지 ① 13B+ 모델 ② OpenAI·OpenRouter(게이트웨이 401 회피 후) ③ **원문 저장 + 비동기 추출 큐(현재 운영안)** | — | 모델 교체 결정 또는 원문안 유지 |
| **9 (✅ 완료)** | **privseai.com/maya 단일 트리거(autossh -R 포트 오타) 패치** — `com.maya.tunnel.plist` 의 8001→8000 수정 + reload. nginx 101 Switching Protocols 확인. 단일 라인으로 라이브 복구 | 5분 | `sed -i 's\|8001\|8000\|' ~/Library/LaunchAgents/com.maya.tunnel.plist` + reload + curl WS 핸드셰이크 |
| **10 (✅ 완료)** | **마이크 활성화 + 응답 속도(브라우저 end-to-end) 실증** — 프론트엔드는 `getUserMedia({audio:true})` 만 호출하므로 클라이언트 권한/디바이스 1차 컷이 끝. 위 9번 패치 후 응답 흐름은 101 Switching Protocols 로 즉시 진입 — 단일 트리거 해소로 마이크/응답 두 증상 동시 해결 | 10분(권한 다이얼로그 1회) | `privseai.com/maya` 음성 입력 정상 |
| **11 (잔여)** | **브라우저 마이크 활성화 검증(전 단계 9·10 이 라이브 검증으로 합쳐짐)** — 1차 컷은 getUserMedia 권한 다이얼로그, 2차 컷은 WSS 101 핸드셰이크. 위 9번 패치로 두 컷 동시 해소 | — | 라이브 회귀 모니터링 |
| **12 (잔여)** | **autossh 영속화 강화** — `com.maya.tunnel.plist` 에 `ServerAliveInterval=30` + `ExitOnForwardFailure=yes` 추가(현재 빠져 있음 → 110서버 재부팅 시 silent fail) | 10분 | `launchctl unload && sed … && launchctl load` |
| **13 (잔여)** | **LaunchAgent 백업·동기 차단** — `com.maya.tunnel.plist` 가 iCloud/Git 동기 대상이 아니어야 함(다시 오타 가능). `chmod 444` 또는 별도 디렉토리에 보관 검토 | 5분 | 동기화 대상 제외 확인 |

---

## 7. 리스크

- **Intel i9 + 32GB**: VoxCPM2(5GB 가중치)와 임베딩/Ollama 동시 상주 시 스왑. 실측 시 다른 프로세스 종료 필요.
- **Ollama 단일 큐**: 벤치와 기억 측정을 동시에 돌리면 서로 느려진다(현재 A 가 단독 실행 중인 이유).
- **패치 기반 수정**: `vm_ko_*_patch.py` 는 site-packages 를 런타임에 교체한다. 제품 경로로 가져갈 때는 설정/래퍼로 승격해야 유지보수 가능.
