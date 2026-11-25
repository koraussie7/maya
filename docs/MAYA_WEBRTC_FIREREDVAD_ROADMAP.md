# Maya WebRTC + FireRedVAD 통합 로드맵

> **대상**: maya-2 음성 파이프라인에 WebRTC Transport 와 FireRedVAD 를 통합하기 위한 검토·PoC·채택 문서.
> **작성**: 2026-11-25
> **상태**: 검토 단계. 채택 결정은 PoC 결과에 따름.
> **연결**: [`MAYA_VOICE_CONSOLIDATION_PLAN.md` §13](MAYA_VOICE_CONSOLIDATION_PLAN.md)

---

## 1. 배경

maya는 현재 **FastAPI WebSocket + JSON + base64 오디오** 구조로 동작한다 ([`maya-ui/backend/app/server.py`](maya-ui/backend/app/server.py), [`app/session.py`](maya-ui/backend/app/session.py), [`app/protocol.py`](maya-ui/backend/app/protocol.py)). 동시에 1차 silence 판정은 **RMS 임계값 250 + 적응형 노이즈 바닥 + NAMO 54문장 100%** 의 3단 판정으로 안정화되어 있다 ([`integration/pcm_turn_engine.py`](integration/pcm_turn_engine.py)).

운영하면서 다음 두 가지 한계가 드러났다.

- **barge-in(끼어들기)**: TTS 재생 중 사용자 발화를 깨끗하게 캡처하지 못함. AEC 부재.
- **다국어 VAD**: silero 대비 한국어 미세 음절 종결에서 energy floor 가 흔들리고, 영어/일본어/중국어 세션은 같은 모델로 처리 불가.

각각을 **WebRTC Transport**(barge-in + 네트워크 회복력)와 **FireRedVAD**(다국어 VAD + AED)로 해결할 수 있는지 검토한다.

---

## 2. WebRTC Transport 도입

### 2.1 무엇을 얹는가

현재:

```
브라우저(SvelteKit, getUserMedia) ──── WS(JSON+base64) ──── maya-ui/backend/app/server.py
                                                              │
                                                              ├── STT (faster-whisper / Qwen3-ASR)
                                                              ├── LLM (keirouter :20128)
                                                              └── TTS (piper_worker / voxcpm_worker)
```

WebRTC 도입 후 (격리 PoC):

```
브라우저                              WSS 시그널링                      RTP/Opus 미디어
│                                      │                                  │
├─ RTCPeerConnection ────────────────►/ws (기존 시그널링 채널) ◄────────────┤
│  + mic (getUserMedia)               │                                  │
│  + opus 인코드                       │                                  │
│  + AEC                              │                                  │
└──────────────────────────────────►aiortc PeerConnection ◄────RTP/Opus────┘
                                      │
                                      └── 기존 STT/LLM/TTS 파이프라인 재사용
```

핵심은 **시그널링은 기존 `/ws` 위에 datachannel**, **미디어는 별도 `/rtc` 엔드포인트**로 분리하는 것이다. WS 라이브 경로는 그대로 보존된다.

### 2.2 비교 기준

| 차원             | 현재 (WS + JSON)                                               | WebRTC 도입 후                        |
| ---------------- | -------------------------------------------------------------- | ------------------------------------- |
| 오디오 운반      | base64 WAV/Opus envelope                                       | RTP/Opus (저수준 스트림)              |
| 클라이언트 SDK   | `lib/mayaClient.js` 직접 작성                                  | 브라우저 native + `RTCPeerConnection` |
| opus 인코딩 위치 | 서버 ([`OpusStreamCodec`](maya/voicemem/codec/opus_stream.py)) | 클라이언트 mic 또는 SFU               |
| 양방향 동시성    | half-duplexish (TTS 재생 중 mute)                              | full-duplex (AEC + barge-in)          |
| 대역폭           | 10초 WAV ~427KB base64                                         | Opus 10초 ~33KB (이미 측정)           |
| 지터/패킷 손실   | 무관 (TCP)                                                     | NACK/FEC/jitter buffer 내장           |
| 라이브 패치      | sed 한 줄                                                      | 통계·pcap·Wireshark 의존              |

### 2.3 긍정 효과

1. **barge-in 자연스러움** — AEC가 스피커 재생음을 마이크 입력에서 빼줌. TTS 중 사용자 발화 깨끗 캡처.
2. **저지연 첫 청크** — 20ms Opus 패킷 직접 송출. piper 응답 0.15~0.34s 의 안정화.
3. **네트워크 변동 회복력** — UDP+RTP+jitter buffer. autossh 터널 흔들리는 환경에서 의미 큼.
4. **opus 인코딩 위치를 클라이언트로 이동** — 서버 CPU 부하 ↓. Intel Mac 에서 가치 큼.
5. **클라이언트 확장** — 모바일(iOS/Android native), ESP32, SIP 전화까지 같은 엔드포인트.

### 2.4 부정 효과

1. **운영 복잡도** — UDP 포트(10000+), ICE 후보, DTLS 인증서, STUN/TURN 운영.
2. **저수준 디버깅 비용** — 9번 패치(autossh -R 8001→8000) 같은 sed 라이브 복구가 어려워짐.
3. **서버 라이브러리 종속** — aiortc 는 Python 3.12+ 호환 이슈 보고 있음. mediasoup/Janus/LiveKit 은 다른 런타임.
4. **barge-in false-positive 리스크** — NAMO 100% 회귀 보존이 우선이라면 barge-in은 옵트인으로.
5. **TURN 자급자족 비용** — coturn 운영 또는 SaaS 의존 둘 다 비용 발생.
6. **상태 모델 변화** — WS+JPEG/JSON은 stateless 메시지 스트림. WebRTC 는 sessionful 미디어 연결. voicemem 트랜스크립션 commit 정책까지 손댈 수 있음.

### 2.5 영향받는 코드

| 파일/모듈                                                                  | 영향                                                   |
| -------------------------------------------------------------------------- | ------------------------------------------------------ |
| [`maya-ui/backend/app/server.py`](maya-ui/backend/app/server.py)           | `/ws` 외에 `/rtc` 엔드포인트 추가 또는 교체            |
| [`maya-ui/backend/app/session.py`](maya-ui/backend/app/session.py)         | WS 메시지 루프 → RTP 수신 + datachannel 핸들러         |
| [`maya-ui/backend/app/protocol.py`](maya-ui/backend/app/protocol.py)       | WS 메시지 타입 일부를 datachannel 또는 RTP 헤더로 이관 |
| [`maya/voicemem/codec/opus_stream.py`](maya/voicemem/codec/opus_stream.py) | 서버 인코딩 → 디코딩만으로 축소 또는 제거              |
| [`maya-ui/lib/mayaClient.js`](maya-ui/lib/)                                | `getUserMedia` + `RTCPeerConnection` 재작성            |
| [`integration/pcm_turn_engine.py`](integration/pcm_turn_engine.py)         | AEC 추가 시 `silencedetect` 단계 조정                  |
| nginx `/maya/ws`                                                           | UDP 릴레이 추가 또는 별도 도메인                       |
| `com.maya.tunnel.plist`                                                    | UDP 포트 autossh `-R` 검토 (TCP만 터널)                |

### 2.6 PoC 설계

#### 2.6.1 격리 원칙

- 기존 `/ws` 경로 **변경 없음** — 라이브 회귀 안전
- `/rtc` 신규 엔드포인트 — SDP 시그널링만 기존 `/ws` 와 공유
- 환경변수 `MAYA_RTC=on` 으로 게이팅

#### 2.6.2 단계

1. **의존성 설치**: `pip install aiortc` (CPU 추론만으로도 시작 가능)
2. **`/rtc` 엔드포인트 스켈레톤**: SDP 교환 핸들러만 작성
3. **프론트 PoC**: 별도 HTML (`/maya-rtc`) 에서 `RTCPeerConnection` 작성, `getUserMedia({audio: {echoCancellation: true}})` 활성화
4. **스트림 수신 → `OpusStreamCodec` 디코드 → PCM 버퍼** — 기존 `pcm_turn_engine.py` 입력 경로 그대로 사용
5. **A/B 측정**: WS 경로와 RTC 경로의 첫 청크 지연, STT 호출 빈도, barge-in false-positive 카운트 비교

#### 2.6.3 회귀 안전장치

- `/ws` 와 `/rtc` 가 **같은 STT/LLM/TTS 인스턴스**를 공유하도록 의도적으로 설계
- 한쪽이 죽으면 다른 쪽은 영향 없음
- 통계 뷰: `RTCPeerConnection.getStats()` 를 5초 간격으로 로그

#### 2.6.4 1주일 A/B 종료 조건

- 첫 청크 지연 **15% 이상 개선** → 채택 후보
- barge-in false-positive **5% 이상** → 옵트인으로 강등
- ICE 협상 실패율 **5% 이상** → TURN 추가 검토
- 운영 복잡도 점수(체크리스트 기준)가 12 이상 → 보류

---

## 3. FireRedVAD 도입

### 3.1 무엇을 얽는가

[`FireRedTeam/FireRedVAD`](https://github.com/FireRedTeam/FireRedVAD) (2026.03 release, FLEURS-VAD-102 SOTA, 2.2MB DFSMN).

현재 [`pcm_turn_engine.py`](integration/pcm_turn_engine.py) 의 3단 판정:

```
PCM 청크 → RMS 250 silence? → STT 전사 → NAMO(텍스트) → 확정 or 계속
```

FireRedVAD 도입 후:

```
PCM 청크 → FireRedVAD streaming (speech/silence) → STT 전사 (speech 구간만) → NAMO(텍스트) → 확정 or 계속
            ↑ 2.2MB DFSMN, ~2ms/frame           ↑ 호출 횟수 ↓
```

**1차 silence 판정만 교체**. NAMO는 그대로 살아있어 54문장 100% 회귀를 보존한다.

### 3.2 비교 기준

| 차원             | 현재 maya                    | FireRedVAD 도입 후             |
| ---------------- | ---------------------------- | ------------------------------ |
| 1차 silence 판정 | RMS 250 + 적응형 노이즈 바닥 | DFSMN streaming                |
| 다국어           | 한국어 중심                  | **100+ 언어**                  |
| 모델 크기        | 0 (rule-based)               | **2.2 MB**                     |
| 한국어 FLEURS F1 | 측정값 없음                  | **97.57%** (Silero 95.95%)     |
| False Alarm Rate | RMS 오탐 가능                | **2.69%** (vs Silero 9.41%)    |
| Miss Rate        | 노이즈 환경 miss 다발        | **3.62%**                      |
| AED (음악/노래)  | 없음                         | speech/singing/music 분리      |
| NCNN runtime     | 없음                         | ✅ 멀티플랫폼 (Intel Mac 포함) |

### 3.3 긍정 효과

1. **다국어 VAD** — 한국어/영어/일본어/중국어 세션이 같은 모델. 코드스위칭 자연스러움.
2. **STT 호출 60~80% 절감** — silence 구간에서 STT 깨우는 일 감소.
3. **AED로 음악 환경 자동 회피** — 화자 분리(21분 녹음, 192 세그먼트) 전단에서 music/singing 자동 분리.
4. **NAMO 앞단의 견고함** — 오디오 신호 1차 컷 + 텍스트 2차 컷의 안정적 2단.
5. **2.2MB + NCNN** — 110 서버 게이트웨이 옆에 VAD 워커 데몬 상주 또는 Mac CPU 추론 둘 다 가능.
6. **silero VAD 대비 한국어 개선** — FLEURS 한국어 F1 +1.62, FAR -6.72.

### 3.4 부정 효과

1. **의존성 추가** — fireredvad + torch(또는 NCNN). `.venv` 가 한 단계 더 늘어남.
2. **콜드스타트 +20~50ms** — 워커 데몬 상주로 해결 (piper 패턴과 동일).
3. **한국어 fine-tuning 불가** — 가중치는 공개, 학습 파이프라인은 별도.
4. **opus 디코드 hop** — [`OpusStreamCodec`](maya/voicemem/codec/opus_stream.py) 페이로드 → PCM → FireRedVAD.
5. **NAMO 대체 아님** — "음성/비음성" 경계만 잡음. 텍스트 의미 판단은 NAMO 잔존.
6. **3단 명시성 흐려짐** — 디버깅 한 단계가 모델 추론 결과로 가려짐. `raw_prob`, `smoothed_prob` 로그 강제.

### 3.5 영향받는 코드

| 파일/모듈                                                                                          | 영향                                                              |
| -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| [`integration/pcm_turn_engine.py`](integration/pcm_turn_engine.py)                                 | `_maybe_close` 의 첫 silence 판정만 교체                          |
| [`maya-ui/backend/app/session.py`](maya-ui/backend/app/session.py)                                 | opus 디코드 후 PCM 전달 또는 VAD 워커 IPC                         |
| [`integration/split_speakers.py`](integration/split_speakers.py)                                   | 화자 분리 전단이 VAD가 되어 음악/잡음 자동 제거                   |
| [`maya-ui/backend/app/stt.py`](maya-ui/backend/app/stt.py)                                         | silence STT 호출 횟수 ↓                                           |
| [`INTEGRATION_RESULTS.md`](INTEGRATION_RESULTS.md)                                                 | §4 AEC 와 함께 "VAD + AEC + NAMO" 3단 평가 갱신                   |
| [`docs/KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md`](docs/KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md) | 적응형 노이즈 바닥 검토 → "DFSMN streaming VAD 로 대체" 결론 가능 |

### 3.6 PoC 설계

#### 3.6.1 격리 원칙

- 환경변수 `MAYA_VAD_ENGINE=firered` 또는 `silero` 또는 `rms` (기본 rms)
- `_maybe_close` 의 첫 분기만 분기
- NAMO 100% 회귀는 **어느 경로에서도 유지**

#### 3.6.2 단계

1. **설치**: `pip install fireredvad` (CPU 모드)
2. **모델 다운로드**: `huggingface-cli download FireRedTeam/FireRedVAD --local-dir ./pretrained_models/FireRedVAD`
3. **워커 데몬 스켈레톤**: piper_worker.py 와 동일한 IPC 패턴 (stdin/stdout JSON)
4. **`pcm_turn_engine.py:_maybe_close` 분기**:

```python
if os.environ.get("MAYA_VAD_ENGINE") == "firered":
    from fireredvad import FireRedStreamVad, FireRedStreamVadConfig
    cfg = FireRedStreamVadConfig(use_gpu=False, speech_threshold=0.4)
    vad = FireRedStreamVad.from_pretrained("pretrained_models/FireRedVAD/Stream-VAD", cfg)
    frame_results, _ = vad.detect_full(pcm)
    if not any(fr.is_speech_end for fr in frame_results):
        return None  # 계속 듣기
# 기존 NAMO 로직 (모든 경로에서 유지)
```

5. **A/B 측정**: 동일 오디오(21분 녹음, 192 세그먼트)로 STT 호출 횟수, NAMO 확정 latency 비교.

#### 3.6.3 회귀 안전장치

- `MAYA_VAD_ENGINE=rms` (기본) — 100% 기존 동작
- `silero` 경로도 비교군으로 보존 (벤치 결과 직접 확인 가능)
- STT 호출 카운트 로그를 메트릭으로 노출

#### 3.6.4 1주일 A/B 종료 조건

- STT 호출 **50% 이상 절감** → 채택 후보
- NAMO 확정 latency 회귀 **10% 이상** → 보류
- 100개 발화 중 false-positive barge-in **5개 이상** → 옵트인으로 강등
- CPU 부하 (M1/M2 baseline 대비) **+15% 이상** → 보류

---

## 4. 통합 우선순위와 마이그레이션 일정

### 4.1 비교 요약

| 항목           | WebRTC                              | FireRedVAD                   |
| -------------- | ----------------------------------- | ---------------------------- |
| 변경 범위      | 클라이언트 + 서버 + 네트워크 인프라 | 서버 한 함수 + 워커 데몬     |
| 회귀 위험      | 중~상 (전송 계층 교체)              | **하** (1차 컷만, NAMO 보존) |
| 운영 비용 증가 | 중 (UDP/ICE/TURN)                   | 하 (CPU 데몬 1개)            |
| 즉시 효과      | barge-in + 네트워크 회복            | STT 비용 절감 + 다국어       |
| 의존성 추가    | aiortc + (coturn or SaaS)           | fireredvad + torch/NCNN      |
| PoC 난이도     | 중 (SDP/ICE 디버깅)                 | **하** (한 함수 분기)        |

### 4.2 권장 순서

1. **Phase B-1 (1주)**: FireRedVAD 1차 컷 PoC. **먼저** — 회귀 위험 낮고 비용 즉시 절감.
2. **Phase B-2 (1주)**: WebRTC `/rtc` PoC. 두 번째 — 인프라 변경이지만 격리 가능.
3. **Phase C (1주)**: 채택 결정, 메인 통합 또는 폐기.

### 4.3 동시 진행 가능 여부

- FireRedVAD PoC와 WebRTC PoC는 **서로 다른 코드 경로**라 동시 진행 가능
- 단, 둘 다 110 서버 부하 테스트를 요구하면 순차 진행
- barge-in 검증은 WebRTC 측에서만 의미 있고, FireRedVAD 는 무관

---

## 5. 성공 지표

### 5.1 공통 KPI

| 지표                                            | 현재     | 목표 (PoC 채택 시) |
| ----------------------------------------------- | -------- | ------------------ |
| 엔드투엔드 지연                                 | < 2.5s   | < 2.0s             |
| 라이브 검증 통과 (`integration/verify_live.py`) | ✅       | ✅ 유지            |
| WS 101 Switching Protocols                      | ✅       | ✅ 유지            |
| NAMO 54문장 정확도                              | 100%     | **100% 회귀 없음** |
| 운영 복잡도 (체크리스트 점수)                   | baseline | +2 이내            |

### 5.2 WebRTC 추가 KPI

| 지표                    | 목표 |
| ----------------------- | ---- |
| 첫 청크 지연 개선       | 15%+ |
| ICE 협상 실패율         | < 5% |
| barge-in false-positive | < 5% |

### 5.3 FireRedVAD 추가 KPI

| 지표                   | 목표      | 2026-11-25 PoC 실측 (8s 발화, 16kHz PCM 30ms 청크)        |
| ---------------------- | --------- | --------------------------------------------------------- |
| STT 호출 횟수 절감     | 50%+      | 미측정 (다음 단계: WAV + Whisper 1-pass A/B)              |
| 1차 컷 시각 (turn-end) | RMS 대비 -3s | **RMS 8.05s → FireRedVAD 4.0s** (Δ -4.05s, max-duration 가드 회피) |
| NAMO 확정 latency 회귀 | < 10%     | NAMO 시나리오 4/4 통과 (백채널/완결/미완/공백), 회귀 없음 |
| CPU 부가 (M2)          | +15% 이내 | RMS p95 0.05ms/청크, **FireRedVAD p95 6.87ms/청크** (NCNN IPC 왕복) |
| 폴백 안정성            | RMS 자동 복귀 | `_vad_failed=False`, 워커 부재 시 `FireRedVadClient.push()` 무동작 + RMS 경로 유지 |

---

## 6. 폐기 조건 (PoC 결과가 약할 때)

### 6.1 WebRTC

- ICE 협상 실패율 5% 초과
- 1주일 라이브 테스트에서 사용자 회귀 보고 1건 이상
- 운영 복잡도 점수 12 초과 (체크리스트 기준)

→ `/rtc` PoC 코드 `archive/2026-11-25-rtc-poc` 으로 이동, 태그 보관.

### 6.2 FireRedVAD

- STT 호출 절감 30% 미만
- NAMO 확정 latency 회귀 15% 초과
- 한국어 환경에서 silero 대비 FAR 개선 없음
- NCNN 워커 cold-start > 200ms (목표 대비 회귀 시)
- **청크당 IPC p95 > 15ms** (실측 6.87ms, 2배 이상 회귀 시)

→ [`integration/pcm_turn_engine.py`](integration/pcm_turn_engine.py) 의 분기만 유지하되 환경변수 기본값 `rms` 복귀, firered 워커 데몬 비활성.

#### 6.2.1 PoC 검증 이력 (2026-11-25)

- `integration/tools/turn_detector.py` shim 추가 → `from tools.turn_detector import get_detector` 정상
- `integration/test_turn_engine.py` 4/4 시나리오 통과 (NAMO)
- `integration/bench_vad_firstcut.py --no-firered` → 8.05s 가드 종결 확인
- `integration/bench_vad_firstcut.py --engines rms,firered` → FireRedVAD 4.0s vad-end 종결 확인 (Δ -4.05s)
- `_load_firered()` 검증: `vad_engine='firered'`, `firered_threshold=0.5`, `firered_fallback=True` 정상 적용, `_vad` 가 `FireRedVadClient` 인스턴스
- HF FireRedVAD 블롭 부재 상황에서도 RMS 폴백 정상 (회귀 위험 없음 확인)

---

## 7. 참고

- [`MAYA_VOICE_CONSOLIDATION_PLAN.md` §13](MAYA_VOICE_CONSOLIDATION_PLAN.md) — 13장 진척 반영
- [`MAYA_VOICE_WORK_STATUS.md`](maya/docs/MAYA_VOICE_WORK_STATUS.md) — privseai.com/maya 라이브 회귀 이력
- [`INTEGRATION_RESULTS.md`](INTEGRATION_RESULTS.md) — 기존 통합 실험 결과 (NAMO 54/54, Whisper 노이즈 붕괴 등)
- [`KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md`](docs/KO_VOICE_ARCHITECTURE_REFERENCE_REVIEW.md) — 한국어 음성 아키텍처 참조 검토
- [`Pipecat 도입 검토`](https://github.com/pipecat-ai/pipecat) — 비교군 (현재 미사용)
- [`FireRedVAD`](https://github.com/FireRedTeam/FireRedVAD) — 외부 라이브러리

---

## 8. 결론 (단일 문장)

**FireRedVAD부터 PoC** — 회귀 위험이 낮고 비용 절감이 즉시 측정 가능. WebRTC는 **barge-in이 제품 요구로 확정된 후**에 격리 PoC로 진행. 둘 다 1주일 A/B 후 채택/폐기 결정.
