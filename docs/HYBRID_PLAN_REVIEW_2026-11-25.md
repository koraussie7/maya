# 하이브리드 플랜 평가 — Rocket.Chat × rust-anything × fast-anything × Maya × Tauri

> 평가일: 2026-11-25
> 평가 대상: 사용자가 제시한 4계층 하이브리드 아키텍처 + Phase 0~4 로드맵 + KPI 표
> 검증 방식: `maya-2` 실제 코드 실측 (`maya-ui/backend/app/*`, `integration/*`), 이전 라운드의 Rocket.Chat 평가(`maya/docs/ROCKETCHAT_EVAL.md`)
> 한 줄 결론: **방향은 맞지만, 전제 3개가 우리 환경에서 이미 틀렸다. 그대로 가면 3~4개월 중 2개월을 "이미 고친 문제"에 쓴다.**

---

## 0. 결론 요약

| 계층                          | 플랜 주장                                        | 실측 판정                                                                | 근거                                                                        |
| ----------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| Rocket.Chat 코어              | "기존 유지, Maya 음성 붙이면 됨"                 | ⚠️ **절반만 맞음** — 채팅은 가능, **음성방은 OOM 위험**                  | 110 서버 free 9GB, WebRTC는 Jitsi+NATS+Mongo RS 필요 (`ROCKETCHAT_EVAL.md`) |
| rust-anything                 | "인증/메시지큐/파일업로드 점진 이전"             | ❌ **지금 할 일 아님** — 4개월 안에 diff 검증까지 불가                   | 우리 병목은 Rust 재작성이 아니라 **TTS/LLM 지연**                           |
| fast-anything                 | "VAD·노이즈제거·벡터·LLM버퍼 Rust 포팅"          | ✅ **1개만 유효** (audio 전처리). 나머지는 이미 해결됨 또는 병목 아님    | §2.3                                                                        |
| Maya STT                      | "faster-whisper small/medium"                    | ❌ **이미 SenseVoice 로 교체됨**                                         | `app/stt.py:139` — SenseVoice 우선 → Whisper 폴백                           |
| Maya TTS                      | "Chatterbox / XTTS v2"                           | ❌ **둘 다 이 맥에서 못 돈다** (torch wheel 부재)                        | 이미 piper/voxcpm-api/hybrid 로 확정                                        |
| LLM                           | "OpenRouter / 110 freellmapi, llama3.1:8b는 5분" | ⚠️ **5분은 이미 틀린 수치** — 현재 로컬 qwen2.5-coder:1.5b, 타임아웃 90s | `scripts/run_maya_backend.sh`                                               |
| Tauri                         | "Electron 대체, 음성 비서 내장"                  | ✅ **가치 있음** — 단 Phase 4, 마지막                                    | —                                                                           |
| KPI: STT 9~18초 → <1초        | —                                                | ❌ **현재 이미 수 초** (SenseVoice 로컬)                                 | `stt.py`                                                                    |
| KPI: "Electron + Node 메모리" | —                                                | ❌ **우린 Electron/Node 를 안 쓴다** (SvelteKit 정적 빌드 + FastAPI)     | `maya-ui/app/src/lib/mayaClient.js`                                         |

**핵심**: 플랜의 절반은 **우리가 이미 지나온 단계**를 계획하고 있다. 남는 진짜 과제는 3개다 —
① barge-in(끼어들기) ② Rocket.Chat 안에 음성을 얹을 때의 채널 프로토콜 ③ 데스크톱 패키징.

---

## 1. 전제 오류 3개 (먼저 고쳐야 함)

### 1.1 "Maya = faster-whisper + Chatterbox/XTTS"

지금 코드는 그렇지 않다.

```
app/stt.py:139   우선순위: SenseVoice 워커 → Whisper → 빈 문자열
app/tts.py        integration/tts_local.synthesize() → piper → voxcpm-api → say
```

- **STT**: SenseVoiceSmall 이 기본. Whisper 는 폴백 경로다. `faster-whisper small/medium` 로
  가면 **한국어 정확도가 오히려 내려간다**(이전 라운드 NAMO 54문장 실측 기준).
- **TTS**: Chatterbox/XTTS 는 **torch ≥ 2.5 wheel 이 이 Intel Mac 에 없다** → 설치 단계에서 막힌다.
  실사용 가능 조합은 이미 확정돼 있다:
  | 경로 | 실측 | 용도 |
  |------|------|------|
  | piper | RTF 0.08~0.17 (2.6s 오디오) | 실시간 기본 |
  | voxcpm-api (Space) | 첫 문장 10~24s, 캐시 0s | 클론 품질 |
  | cosyvoice3 GGUF (CPU) | RTF 14.66 | 오프라인 복제 검증 |
  | say | 2.7s | 최후 폴백 |

  → 플랜 Phase 1 의 "TTS: Chatterbox/XTTS" 항목은 **삭제**해야 한다.

### 1.2 "LLM 응답 5분 (llama3.1:8b)"

현재 기본은 `qwen2.5-coder:1.5b` + `MAYA_LLM_TIMEOUT_S=90`. 5분은 과거 측정값이지 현재가 아니다.
그리고 파이프라인은 이미 end-to-end 로 `agent.get_system_response` → `reply` (<0.001s, 라우팅) 까지 검증됐다.
**병목은 LLM 이 아니라 TTS 첫 청크다** — 여기에 개발 시간을 써야 한다.

### 1.3 "Electron + Node.js 메모리 → Tauri 로 50% 절감"

우리 UI 는 Electron 도 Node 런타임도 쓰지 않는다. SvelteKit 정적 빌드 + FastAPI(`/ws`) 다.
따라서 **"50% 절감"의 기준선이 존재하지 않는다.** Tauri 를 도입할 때의 실제 이득은
메모리가 아니라 **오프라인 STT/TTS 내장**과 **마이크 권한·장치 열거를 네이티브로** 얻는 것이다.
KPI 를 그렇게 바꿔야 측정이 된다.

---

## 2. 시너지 재평가

### 2.1 ✅ 진짜 시너지 (지금 진행해도 됨)

| 시너지                                     | 이유                                                                                                    | 비용      |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------- | --------- |
| **Rocket.Chat 채팅 + Maya 음성 명령**      | 이미 `chat.text` 메시지 타입이 존재(`protocol.py`, `session.py:497`). 봇 계정 하나로 웹훅만 붙이면 된다 | 1~2일     |
| **메시지 → TTS 재생 (읽어주기)**           | `audio.out` 경로가 그대로 재사용됨. 채팅 메시지 id ↔ 오디오 캐시                                        | 2~3일     |
| **Rust 오디오 전처리 (VAD/리샘플/정규화)** | Intel Mac 에서 Python 루프 RMS 계산이 청크마다 병목. `pcm_turn_engine` 의 rms 판정이 대상               | 1주 (PoC) |
| **Tauri 데스크톱 패키징**                  | 터널(autossh) 의존 없이 로컬 데몬과 직접 통신 → 사내망에서 안정                                         | 2~3주     |

### 2.2 ⚠️ 조건부 (전제가 바뀌면 가능)

| 시너지                             | 조건                                                                                                                                                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **다자간 음성방 (Discord 스타일)** | Rocket.Chat 자체 WebRTC 는 **Jitsi + NATS + MongoDB replica set** 필요 → 110 서버 9GB 로는 OOM 위험. 별도 SFU(LiveKit) 노드가 있어야 한다. "Maya 로 대체 가능"은 **아니다** — Maya 는 1:1 대화 파이프라인이다 |
| **Rocket.Chat 음성봇**             | DDP/Meteor 봇 API 로 메시지 수신 → Maya `/ws` 로 릴레이하는 **브리지 프로세스**가 필수. Rocket.Chat 은 `session.start`/`audio.chunk` 를 모른다                                                                |
| **NATS 이벤트 버스**               | 이미 110 에 NATS 가 있다면 무료. 새로 띄우면 메모리 +200MB                                                                                                                                                    |

### 2.3 ❌ 병목이 아닌 것 / 이미 해결된 것

| 플랜 항목                                      | 판정                                                                                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| "벡터 검색 RAG 500ms → 50ms"                   | **RAG 를 아직 쓰지 않는다.** 검색 계층이 없는데 최적화 대상이 없다                                                                   |
| "LLM 토큰 버퍼 Rust 포팅, 첫 토큰 30~50% 감소" | 스트리밍은 이미 async generator 로 흐른다. 남은 지연은 **모델 자체 생성 속도**지 Python 오버헤드가 아니다. Rust 로 바꿔도 개선 폭 ~0 |
| "TTS PCM 인코딩/볼륨 정규화 Rust"              | piper 는 모델 내부에서 정규화까지 끝내고 WAV 로 준다. 후처리할 게 없다                                                               |
| "인증/세션 JWT Rust 재작성"                    | Maya 는 세션당 단일 WS 연결 + 페르소나 로드가 전부다. 인증 계층이 두껍지 않다                                                        |
| "파일 업로드 파이프라인 Rust"                  | 우리에게 파일 업로드 기능이 없다                                                                                                     |

---

## 3. rust-anything / fast-anything vs 우리 상황

두 라이브러리의 공통 전제는 **"크고 느린 기존 코드베이스가 이미 있고, 그 위에 안전하게 얹는다"** 이다.

```
rust-anything : 녹화(record) → Rust 포팅 → 재생(replay) → 원본과 diff → 교체
fast-anything : 프로파일링 → 느린 5% 함수만 추출 → PyO3/napi 로 포팅
```

우리 코드베이스에서 이 전제가 성립하는 곳은 **딱 한 군데**다:

| 대상                                                | 성립? | 이유                                                                             |
| --------------------------------------------------- | ----- | -------------------------------------------------------------------------------- |
| `integration/pcm_turn_engine.py` (rms 판정, VAD 컷) | ✅    | 순수 함수 + 결정적 출력 → diff 검증이 쉬움. 청크마다 호출되어 실제 CPU 를 먹음   |
| `integration/split_speakers.py` (화자 분리)         | ⚠️    | 배치성. 대화 경로가 아니라 우선순위 낮음                                         |
| `maya-ui/backend/app/session.py` (오케스트레이션)   | ❌    | I/O + 상태기계. Rust 로 옮겨서 얻을 게 없음                                      |
| Rocket.Chat 코어 (Meteor)                           | ❌    | **우리 코드가 아니다.** 남의 프로젝트를 점진 재작성하는 건 3~4개월이 아니라 1년+ |

**권고**: fast-anything 의 **방법론(프로파일 → 핫패스만 → diff 검증)** 은 채택하되,
**대상은 `pcm_turn_engine` 의 rms/VAD 하나로 한정**한다. Rust 모듈 1개, 1주 PoC 로 측정.
`rust-anything` 은 **당분간 도입하지 않는다** — diff 검증 인프라 구축 비용이 이득보다 크다.

---

## 4. 수정된 로드맵 (3개월 → 6주로 압축)

플랜의 Phase 0~4 는 3~4개월이다. 전제 오류를 제거하면 **핵심 경로는 6주**다.

| 주     | 작업                                                         | 산출물                     | 검증                                     |
| ------ | ------------------------------------------------------------ | -------------------------- | ---------------------------------------- |
| **W1** | 보안 정리 (API 키 재발급, plist/autossh 점검) + Rust 툴체인  | `security_audit.md`        | 키 노출 0건                              |
| **W2** | **Rust audio 전처리 PoC** (`pcm_turn_engine` rms 만)         | `maya_aio` (PyO3)          | 동일 오디오 A/B — 턴 개수 ±0             |
| **W3** | **Rocket.Chat 브리지** (DDP 수신 → Maya `/ws` 릴레이 → 답장) | `integration/rc_bridge.py` | 채팅방에서 "마야, 오늘 일정" → 음성 답장 |
| **W4** | 메시지 읽어주기 (TTS 캐시 + 재생 버튼) + barge-in 옵트인     | `audio.out` 재사용         | 재생 지연 p95 < 800ms                    |
| **W5** | Tauri 래핑 (SvelteKit 빌드 + 마이크 네이티브)                | `src-tauri/`               | macOS 바이너리 실행                      |
| **W6** | 회귀 검증 (NAMO 54문장 100%, 턴 판정 회귀 0) + 문서화        | `docs/`                    | 회귀 0건 이면 릴리스                     |

**삭제한 항목**: "Maya 마이크로서비스 분리(FastAPI 화)" — 이미 FastAPI 다.
"인증/메시지큐/파일업로드 Rust 이전" — 이득 없음.

---

## 5. KPI 표 재작성 (측정 가능한 것만)

| 지표                    | 플랜 값           | 수정된 기준                                                               | 측정 방법                    |
| ----------------------- | ----------------- | ------------------------------------------------------------------------- | ---------------------------- |
| STT 전사 지연           | 9~18s → <1s       | **현재값 실측 후** 목표 설정 (`chunk_ms_p95`, `proc_total_s` 이미 계측됨) | `bench_vad_firstcut.py` 로그 |
| LLM 첫 토큰             | 5분 → 1~3s        | **로컬 qwen2.5-coder:1.5b 실측값** 기준. 5분은 폐기                       | `MAYA_LLM_TIMEOUT_S` 로그    |
| TTS 첫 청크             | (없음)            | **p95 < 400ms** (piper 기준)                                              | `tts_local` 계측             |
| barge-in false-positive | (없음)            | **< 5%**                                                                  | TTS 재생 중 발화 20회        |
| 메모리                  | Electron 대비 50% | **오프라인 동작 여부**로 대체 (Tauri 의 실제 이득)                        | 인터넷 차단 후 대화          |
| 한국어 STT 정확도       | > 85%             | **NAMO 54문장 100% 유지** (이미 달성)                                     | 회귀 테스트                  |

---

## 6. 최종 판정

**"매우 큰 시너지"라는 결론에는 동의하지 않는다. 시너지는 실재하지만 범위가 훨씬 좁다.**

- **실재하는 시너지**: Rocket.Chat 의 채팅 UX + Maya 의 음성 + Tauri 의 데스크톱 배포. 이 3개는 진짜다.
- **과장된 시너지**: rust-anything 의 Meteor 재작성, fast-anything 의 벡터/LLM 버퍼 최적화. 우리 병목이 아니다.
- **위험한 전제**: Chatterbox/XTTS, faster-whisper small, "다자간 음성방 = Maya". 셋 다 이 환경에서 실패한다.
- **가장 큰 실무 리스크**: 3~4개월 중 상당 부분을 **이미 해결된 문제**(STT 교체, TTS 엔진 선택, FastAPI 화)에 재투자하게 된다.

**권고 실행 순서**: W1 보안 → W2 Rust audio PoC(1주, 성공/실패 즉시 판정) → W3 Rocket.Chat 브리지 → W5 Tauri.
**W2 의 A/B 결과가 "턴 개수 불일치" 또는 "처리시간 개선 10% 미만"이면 Rust 경로는 즉시 폐기**하고
Python 유지 + Tauri/Rocket.Chat 통합에 집중한다. (폐기 조건을 미리 정해두는 것이 이 플랜의 가장 큰 결함을 막는다.)

---

## 7. 참고

- [`ROCKETCHAT_EVAL.md`](../maya/docs/ROCKETCHAT_EVAL.md) — Rocket.Chat 단독 평가 (음성 대체 불가, OOM 위험)
- [`MAYA_WEBRTC_FIREREDVAD_ROADMAP.md`](MAYA_WEBRTC_FIREREDVAD_ROADMAP.md) — barge-in 은 WebRTC 또는 옵트인으로
- [`KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md`](KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md)
- 코드 근거: `maya-ui/backend/app/{protocol.py,session.py,stt.py,tts.py}`, `integration/pcm_turn_engine.py`
