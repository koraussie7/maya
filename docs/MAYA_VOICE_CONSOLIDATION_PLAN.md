# Maya 음성 비서 코드 통합 및 최적화 플랜

> 분산된 `/Users/brianyeon/CodeGPT/maya/tools/` 및 `maya-2/` 코드를 단일 폴더에 통합하고, 보안·정합성·성능을 최적화하는 실행 계획서.

---

## 1. 현황 분석 (As-Is)

### 1.1 코드 분산 현황

| 위치 | 주요 파일 | 문제점 |
|------|----------|--------|
| `maya/tools/` | `mac_voice_chat.py`, `bitterbot_duplex_voice.py`, `mac_talk_dadachat.py`, `realtimestt_server.py`, `webapp_server.py`, `piper_worker.py` 등 | 동일 기능 스크립트 난립, 정본 불명 |
| `maya/tools/local_voice/` | `stt_service.py`, `tts_service.py`, `llm_service.py`, `agent.py` | 로컬 파이프라인이지만 한국어 미지원 |
| `maya/tools/duplex_s2s/` | `agent.py`, `s2s_client.py`, `audio_io.py`, `interrupter.py` | 실시간 S2S이지만 인터페이스 비표준 |
| `maya-2/maya-ui/backend/app/` | `server.py`, `llm.py`, `stt.py`, `tts.py`, `session.py`, `protocol.py`, `config.py` | FastAPI 백엔드이나 프론트엔드와 연결 미완료 |
| `maya-2/integration/` | `vm_ko_model_compare.py`, `gguf_weight_check.py`, `vm_ko_check.py`, `patch_webapp_server.py` | 검증/패치 스크립트, 운영 코드 아님 |

### 1.2 치명적 문제 요약

1. **보안**: `KILOCODE_API_KEY` 등 API 키가 `~/Library/LaunchAgents/*.plist`에 평문 저장. OpenRouter 키 유출로 $1,504 초과 요금 발생 전례 있음.
2. **중복 구현**: STT/TTS/LLM/Agent가 3~4개 버전으로 중복 존재. 데이터 포맷(샘플레이트, JSON 스키마)이 일치하지 않음.
3. **한국어 미지원**: Whisper는 중국어 전용, Kitten TTS는 영어 전용, 로컬 LLM은 속도·정확도 부적합.
4. **하드코딩**: 절대 경로(`/Users/brianyeon/CodeGPT/maya`), 기본 포트, 모델명 등이 코드 내 직접 작성됨.
5. **실행 환경 불명확**: `.venv-stt`, `.venv-voicemem` 등 서로 다른 가상환경을 가정.

---

## 2. 통합 아키텍처 (To-B)

### 2.1 단일 폴더 구조 제안

모든 운영 코드를 `/Users/brianyeon/pythiamoney/maya-voice/` 단일 루트로 통합한다.

```
maya-voice/
├── .env                      # 환경 변수 (API 키, 엔드포인트, 포트) — Git 추적 금지
├── .env.example              # 예시 템플릿 (실제 값 없음)
├── pyproject.toml            # 단일 Python 의존성 관리 (poetry 또는 pip)
├── requirements.txt
├── README.md
├── AGENTS.md                 # AI 코딩 에이전트용 규칙
│
├── src/
│   ├── __init__.py
│   ├── config.py             # 환경 변수 로드 및 검증 (pydantic-settings 권장)
│   ├── security.py           # API 키 관리 (Keychain/환경 변수 연동), 검증 유틸
│   │
│   ├── stt/                  # Speech-To-Text (표준 인터페이스)
│   │   ├── __init__.py
│   │   ├── base.py           # STTBase (추상 클래스)
│   │   ├── whisper_local.py  # 로컬 Whisper small/medium (한국어 지원)
│   │   └── factory.py        # STT 팩토리 (환경 변수로 선택)
│   │
│   ├── llm/                  # Large Language Model (표준 인터페이스)
│   │   ├── __init__.py
│   │   ├── base.py           # LLMBase (추상 클래스)
│   │   ├── openrouter.py     # OpenRouter 클라우드 API (권장)
│   │   ├── freellmapi.py     # 110 서버 freellmapi/smartrouter 연동 (옵션)
│   │   └── factory.py        # LLM 팩토리
│   │
│   ├── tts/                  # Text-To-Speech (표준 인터페이스)
│   │   ├── __init__.py
│   │   ├── base.py           # TTSBase (추상 클래스)
│   │   ├── chatterbox.py     # Chatterbox TTS (한국어 음성 복제)
│   │   ├── xtts.py           # XTTS v2 (다국어)
│   │   └── factory.py        # TTS 팩토리
│   │
│   ├── agent/                # 음성 비서 에이전트 (오케스트레이터)
│   │   ├── __init__.py
│   │   ├── pipeline.py       # STT → LLM → TTS 파이프라인
│   │   ├── session.py        # 대화 세션 관리
│   │   └── turn.py           # 턴 판정 및 인터럽트 처리
│   │
│   ├── audio/                # 오디오 입출력 (PCM/WAV, 샘플레이트 통일)
│   │   ├── __init__.py
│   │   ├── capture.py        # 마이크 캡처 (Web Audio 또는 PyAudio)
│   │   ├── playback.py       # 오디오 재생
│   │   └── utils.py          # 리샘플링, 인코딩 (base64, raw PCM)
│   │
│   ├── server/               # 백엔드 서버 (FastAPI 또는 aiohttp)
│   │   ├── __init__.py
│   │   ├── main.py           # 진입점
│   │   ├── websocket.py      # WebSocket 핸들러 (실시간 스트리밍)
│   │   ├── routes.py         # REST API 엔드포인트
│   │   └── middleware.py     # CORS, 인증, 로깅
│   │
│   └── cli/                  # CLI 도구 (디버깅, 테스트)
│       ├── __init__.py
│       └── test_pipeline.py  # 엔드투엔드 파이프라인 테스트
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/             # 테스트용 참조 오디오 (16kHz mono WAV)
│
├── scripts/
│   ├── setup.sh              # 의존성 설치, 모델 다운로드
│   ├── migrate.sh            # 기존 코드 마이그레이션 도우미
│   └── security-audit.sh     # API 키 노출 점검 스크립트
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
│
└── docs/
    ├── ARCHITECTURE.md
    ├── API_REFERENCE.md
    ├── KOREAN_VOICE.md       # 한국어 STT/TTS 설정 가이드
    └── SECURITY.md           # 보안 운영 가이드
```

### 2.2 인터페이스 표준화

모든 STT/LLM/TTS 모듈은 다음 추상 인터페이스를 준수해야 한다.

#### STT 인터페이스
```python
class STTBase(ABC):
    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000) -> str: ...
    async def stream(self, audio_queue: AsyncQueue) -> AsyncIterator[str]: ...
```

#### LLM 인터페이스
```python
class LLMBase(ABC):
    async def generate(self, messages: list[dict], stream: bool = False) -> AsyncIterator[str]: ...
    async def extract_facts(self, text: str) -> list[dict]: ...  # 좌뇌 사실 추출 (옵션)
```

#### TTS 인터페이스
```python
class TTSBase(ABC):
    async def synthesize(self, text: str, sample_rate: int = 16000) -> bytes: ...  # PCM 반환
    async def stream(self, text_queue: AsyncQueue) -> AsyncIterator[bytes]: ...
```

#### 오디오 포맷 통일
- **샘플레이트**: 16000 Hz (STT/TTS 공통)
- **인코딩**: S16LE (서명 16비트 리틀 엔디안), 모노
- **WebSocket 메시지**: `{"type": "stt_final|tts_audio|turn_start|turn_end", "data": "..."}`

---

## 3. 모듈 맵핑 및 폐기 대상

### 3.1 통합 맵핑 표

| 기존 파일 | 신규 위치 | 상태 | 비고 |
|-----------|-----------|------|------|
| `maya/tools/local_voice/stt_service.py` | `src/stt/whisper_local.py` | ✅ 마이그레이션 | 한국어 모델 교체 필요 |
| `maya/tools/local_voice/tts_service.py` | `src/tts/chatterbox.py` | ✅ 마이그레이션 | 한국어 지원 TTS로 교체 |
| `maya/tools/local_voice/llm_service.py` | `src/llm/openrouter.py` | ✅ 마이그레이션 | 로컬 LLM → OpenRouter 전환 |
| `maya/tools/local_voice/agent.py` | `src/agent/pipeline.py` | ✅ 마이그레이션 | 오케스트레이터로 재구성 |
| `maya/tools/duplex_s2s/agent.py` | `src/agent/turn.py` | ✅ 마이그레이션 | 턴 판정·인터럽트 로직 이관 |
| `maya/tools/duplex_s2s/audio_io.py` | `src/audio/capture.py` | ✅ 마이그레이션 | 오디오 포맷 통일 |
| `maya/tools/mac_voice_chat.py` | `src/cli/mac_voice_chat.py` | ⚠️ 검토 후 결정 | 단일 진입점으로 통합 가능 |
| `maya/tools/bitterbot_duplex_voice.py` | `src/cli/bitterbot_duplex.py` | ⚠️ 검토 후 결정 | 보안 점검 후 필요 여부 판단 |
| `maya/tools/mac_talk_dadachat.py` | 폐기 또는 아카이브 | ❌ 폐기 후보 | 중복 및 불명확 의존성 |
| `maya/tools/realtimestt_server.py` | `src/server/websocket.py` | ✅ 마이그레이션 | FastAPI WebSocket으로 재구현 |
| `maya/tools/webapp_server.py` | `src/server/main.py` + `patch_webapp_server.py` 참조 | ✅ 마이그레이션 | 스트리밍 TTS 패치 포함 |
| `maya/tools/piper_worker.py` | `src/tts/piper.py` (옵션) | ⚠️ 검토 | Piper TTS가 필요하면만 |
| `maya-2/maya-ui/backend/app/*` | `src/server/*` | ✅ 마이그레이션 | FastAPI 백엔드 코드 이관 |

### 3.2 폐기/아카이브 대상

- `mac_talk_dadachat.py`: 문서화되지 않은 의존성, 중복 기능
- `tools/` 내 동일 기능 스크립트 중 미사용 확인본
- `.bak-*`, `prev-*` 백업 파일 (마이그레이션 후 `archive/`로 이동)

---

## 4. 보안 강화 계획 (P0 - 즉시)

### 4.1 API 키 관리

| 항목 | 조치 |
|------|------|
| OpenRouter 키 | 즉시 폐기 및 재발급. `.env`에만 저장, 코드 내 하드코딩 금지 |
| Kilocode API 키 | `~/Library/LaunchAgents/ai.tiermux.proxy.plist`에서 삭제. 새로운 키는 Keychain 또는 `.env` 관리 |
| 110 서버 SSH 키 | `~/.ssh/id_ed25519_mac` 권한 확인, `authorized_keys`에 모르는 키가 있는지 점검 |
| 환경 변수 | `python-dotenv`로 `.env` 로드, `pyproject.toml`에 `python-dotenv` 의존성 추가 |

### 4.2 보안 스크립트 (`scripts/security-audit.sh`)

```bash
#!/bin/bash
# API 키가 평문으로 노출된 파일 검색
echo "=== API 키 노출 검색 ==="
grep -rn "sk-\|OPENAI_API_KEY\|KILOCODE_API_KEY" --include="*.py" --include="*.plist" --include="*.env" --include="*.json" . 2>/dev/null | grep -v ".env.example" || echo "없음"

# suspicious Launch Agent 확인
echo "=== suspicious Launch Agents ==="
ls -la ~/Library/LaunchAgents/ 2>/dev/null

# autossh 터널 프로세스 확인
echo "=== autossh 프로세스 ==="
ps aux | grep autossh | grep -v grep || echo "없음"
```

### 4.3 서버 점검 (110 서버 / privseai.com/maya)

- `ssh 110` 후 `cat ~/.ssh/authorized_keys` 점검
- `crontab -l`로 예약 작업 확인
- `last -20`로 최근 로그인 기록 확인
- 불필요한 포트(8000, 18000 등) 방화벽 차단 고려

---

## 5. 한국어 음성 파이프라인 최적화 (P1)

### 5.1 STT (음성 → 텍스트)

- **문제**: 기존 Whisper는 `paraformer-zh-streaming` 중국어 전용
- **해결**:
  - `faster-whisper` (small 또는 medium) + 한국어 언어 코드 `ko` 사용
  - 또는 Korean-optimized STT (KETI, Clova Speech 등) 검토

### 5.2 LLM (텍스트 → 응답)

- **문제**: 로컬 LLM(qwen2.5-coder:1.5b, llama3.1:8b)는 실시간 대화에 너무 느림(9초~5분)
- **해결**:
  - 1차: OpenRouter (`google/gemini-2.0-flash-exp`, `meta-llama/llama-3.1-8b-instruct`) — 응답 1~3초
  - 2차 옵션: 110 서버 `freellmapi/smartrouter` 연동 (서버 확인 후)
  - 로컬 LLM은 사실 추출(좌뇌) 등 비실시간 작업에만 사용

### 5.3 TTS (텍스트 → 음성)

- **문제**: Kitten TTS는 영어 전용, 한국어 음성 생성 불가
- **해결**:
  - Chatterbox TTS (다국어, 참조 오디오 기반 음성 복제)
  - 또는 XTTS v2 (한국어 지원, 음성 클로닝)
  - 참조 오디오: `maya/models/tts/reference/new_recording_16k.wav` 재녹음 권장 (-20dB~ -10dB, 15~30초)

---

## 6. 성능 최적화 (P2)

### 6.1 스트리밍 파이프라인

```
STT 스트리밍 → 부분 전사 → LLM 스트리밍 → 토큰 단위 TTS → 오디오 스트리밍 재생
```

- `patch_webapp_server.py`의 `stream_tts` 로직을 `src/server/websocket.py`에 통합
- 문장 단위 TTS로 첫 소리 지연 최소화
- `interrupter.py` 로직을 `src/agent/turn.py`에 이관 (사용자 발화 시 TTS 중단)

### 6.2 지연 시간 목표

| 지표 | 목표값 |
|------|--------|
| STT 엔드포인팅 | < 500ms |
| LLM 첫 토큰 | < 1500ms (클라우드 기준) |
| TTS 첫 오디오 | < 800ms |
| 엔드투엔드 (음성→음성) | < 3초 |

---

## 7. 마이그레이션 단계 (실행 순서)

### Phase 0: 보안 및 기반 준비 (1~2일)

| 작업 | 내용 | 검증 |
|------|------|------|
| 0.1 | 모든 API 키 폐기 및 재발급 | OpenRouter 대시보드에서 사용량 0 확인 |
| 0.2 | `~/Library/LaunchAgents/*.plist`에서 키 삭제 | `grep KILOCODE ~/Library/LaunchAgents/` 빈 결과 |
| 0.3 | `com.maya.tunnel.plist` 중지/삭제 | `launchctl list \| grep maya` 빈 결과 |
| 0.4 | `.env.example` 작성 및 `.env` 생성 | `cp .env.example .env` |
| 0.5 | 110 서버 보안 점검 | `authorized_keys`, `crontab`, `last` 확인 |

### Phase 1: 단일 폴더 구조 생성 및 코드 이관 (3~5일)

| 작업 | 내용 | 검증 |
|------|------|------|
| 1.1 | `maya-voice/` 디렉토리 생성 및 skeleton 작성 | `tree maya-voice/` |
| 1.2 | `src/config.py` 작성 (pydantic-settings) | `python -c "from src.config import settings; print(settings)"` |
| 1.3 | `src/stt/`, `src/llm/`, `src/tts/` 추상 베이스 클래스 작성 | 인터페이스 정의 완료 |
| 1.4 | 기존 STT/TTS/LLM/Agent 코드 이관 및 리팩토링 | import 오류 없음 |
| 1.5 | 중복 스크립트 `archive/`로 이동 | `tools/` 내 동일 기능 파일 1개만 남음 |

### Phase 2: 한국어 파이프라인 연동 및 스트리밍 (5~7일)

| 작업 | 내용 | 검증 |
|------|------|------|
| 2.1 | `faster-whisper` 한국어 STT 연동 | 한국어 음성 → 텍스트 정확도 > 85% |
| 2.2 | Chatterbox 또는 XTTS v2 TTS 연동 | 한국어 음성 생성 성공 |
| 2.3 | OpenRouter LLM 연동 (스트리밍) | 첫 토큰 < 1500ms |
| 2.4 | WebSocket 스트리밍 파이프라인 완성 | 엔드투엔드 < 3초 |

### Phase 3: 테스트, 배포, 문서화 (3~5일)

| 작업 | 내용 | 검증 |
|------|------|------|
| 3.1 | 단위 테스트 작성 (`tests/unit/`) | pytest 통과 |
| 3.2 | 엔드투엔드 테스트 (`tests/integration/`) | 파이프라인 전체 동작 |
| 3.3 | Docker 이미지 구성 및 배포 스크립트 | `docker-compose up` 성공 |
| 3.4 | `docs/` 작성 (아키텍처, API, 보안) | 문서 완성 |
| 3.5 | 보안 감사 스크립트 실행 | `scripts/security-audit.sh` 깨끗함 |

---

## 8. 검증 및 테스트 계획

### 8.1 무결성 검증 체크리스트

- [ ] 모든 Python 파일이 `compile()`로 문법 검사 통과
- [ ] `src/stt/`, `src/llm/`, `src/tts/`가 추상 인터페이스 구현
- [ ] 오디오 포맷이 16kHz 모노 S16LE로 통일
- [ ] 환경 변수가 `.env`에서만 읽히고 코드에 하드코딩되지 않음
- [ ] Git에 `.env`가 커밋되지 않음 (`.gitignore` 확인)

### 8.2 정합성 검증 체크리스트

- [ ] STT 출력 텍스트가 LLM 입력으로 정상 전달
- [ ] LLM 응답이 TTS 입력으로 정상 전달
- [ ] TTS 오디오가 WebSocket 클라이언트로 정상 스트리밍
- [ ] 세션 ID가 STT→LLM→TTS 전체에서 일관됨
- [ ] 한국어 텍스트가 영어로 번역되지 않고 원문 유지

### 8.3 보안 검증 체크리스트

- [ ] `grep -rn "sk-\|API_KEY" src/` 결과 없음
- [ ] `~/Library/LaunchAgents/`에 suspicious plist 없음
- [ ] `autossh` 프로세스 실행 안 함
- [ ] 110 서버 `authorized_keys`에 등록된 키가 본인 소유만임

---

## 9. 롤백 계획

문제 발생 시 이전 상태로 되돌릴 수 있도록 다음을 준비한다.

| 단계 | 롤백 대상 | 방법 |
|------|----------|------|
| Phase 1 실패 | `maya-voice/src/` 코드 | `git checkout` 또는 `archive/`에서 기존 파일 복원 |
| Phase 2 실패 | STT/TTS/LLM 모델 | `factory.py`에서 이전 엔진으로 스위칭 (환경 변수 1줄 변경) |
| 보안 사고 발생 | API 키 | OpenRouter/서비스 제공자에서 즉시 키 폐기, 새 키 발급 |
| 서버 연동 실패 | 110 서버 freellmapi | 로컬 OpenRouter 또는 로컬 LLM으로 폴백 (`fallback_llm.py` 참조) |

---

## 10. 성공 지표 (KPI)

| 지표 | 목표값 | 측정 방법 |
|------|--------|-----------|
| 코드 중복률 | < 10% | `find src/ -name "*.py"` 중 유사 기능 파일 수 |
| STT 한국어 정확도 | > 85% | 테스트 오디오 세트 전사 비교 |
| TTS 한국어 자연도 | 주관적 4.0/5.0 이상 | 사용자 테스트 |
| 엔드투엔드 지연 | < 3초 | `time.perf_counter()` 측정 |
| 보안 취약점 | 0건 (Critical/High) | `scripts/security-audit.sh` 실행 결과 |
| 단일 진입점 | 1개 | `python -m src.server.main` 또는 `uvicorn src.server.main:app` |

---

## 11. 즉시 실행할 작업 (오늘 할 수 있는 것)

1. **보안 조치**: API 키 폐기/재발급, plist에서 키 삭제, autossh 중지
2. **폴더 생성**: `mkdir -p /Users/brianyeon/pythiamoney/maya-voice/src/{stt,llm,tts,agent,audio,server,cli}`
3. **환경 파일**: `.env.example` 작성 (실제 값은 `.env`에만)
4. **서버 점검**: `ssh 110` 후 `authorized_keys`, `crontab`, `last` 확인
5. **기존 코드 목록화**: `maya/tools/` 내 모든 파일 목록 작성 후 마이그레이션 대상 선정

---

이 플랜을 바탕으로 `src/config.py`부터 순차적으로 구현해 나갑니다. 특정 단계의 상세 코드가 필요하면 알려주세요.
---

## 12. 실측 정정 (2026-11-24, 110 서버 + maya-2 통합 분석 결과)

원안은 `/Users/brianyeon/pythiamoney/maya-voice/`를 단일 폴더로 가정했으나, 실제 인벤토리를 조사한 결과 다음과 같이 정정한다.

### 12.1 As-Is 정정

| 항목 | 원안 | 실측 |
|------|------|------|
| 통합 대상 폴더 | `maya/tools/` | **존재하지 않음**. 실제 통합 풀은 `/Users/brianyeon/CodeGPT/maya-2/integration/` (33개 파일) + `maya-ui/` (SvelteKit 프론트엔드) |
| 작업 디렉토리 | `/Users/brianyeon/pythiamoney/maya-voice/` | `/Users/brianyeon/pythiamoney/`는 워크스페이스 외부라 read/write 툴이 접근 불가. 실제 통합은 **워크스페이스 안**(`/Users/brianyeon/CodeGPT/maya-2/`)에서 수행 |
| LLM 백엔드 | OpenRouter 클라우드 | **OPENROUTER_API_KEY가 비어있어 401**. 실측상 작동하는 백엔드는 macOS 로컬 `127.0.0.1:11434` (ollama, `qwen2.5:1.5b`)만 |
| STT | Whisper small | ✅ `faster-whisper` small, 16kHz 모노로 작동 중. 한국어 정확도는 모델 비교에서 양호 |
| TTS | Chatterbox/XTTS (한국어) | ✅ `Qwen/Qwen3-TTS-1.7B` (4.2GB, 16kHz, ~12s/요청)으로 E2E 검증됨. 폴백 `piper` → `say`. 한국어 음성 복제는 별도 작업 항목 |
| privseai.com/maya | 110 서버 nginx에 /maya 라우트 존재 | ❌ **라우트 없음**. nginx 사이트 설정(`/etc/nginx/sites-enabled/privseai.com.conf`)에 `/maya` location 블록 부재. 현재는 `aivoice.privseai.com` 및 `/privseai/`, `/aivoice/`만 프록시됨 (`proxy_pass http://127.0.0.1:18000`, macOS SSH 역터널 의존) |

### 12.2 마이그레이션 후보 인벤토리 (`maya-2/integration/`)

| 카테고리 | 파일 | 역할 | 통합처 |
|----------|------|------|--------|
| TTS | `tts_local.py` | Qwen3-TTS-1.7B → piper → say 체인 | `src/tts/local.py` |
| TTS | `piper_worker.py` | Piper 폴백 | `src/tts/piper.py` |
| TTS | `voxcpm_api_worker.py` | HF Space VoxCPM API | `src/tts/voxcpm_api.py` |
| TTS | `voxcpm_worker.py` | 로컬 VoxCPM (선택적) | `src/tts/voxcpm_local.py` |
| STT | `pcm_turn_engine.py` | PCM 턴 감지 | `src/stt/turn_engine.py` |
| STT | `turn_detector.py` | VAD/턴 디텍션 | `src/stt/turn_detector.py` |
| LLM | `bench_llm.py`, `llm_chat_bench.py` | LLM 벤치마크 | `src/llm/bench.py` |
| LLM | `kilo_key_save.sh` | Keychain 키 저장 | `src/security/keychain.sh` |
| 한국어 추출 | `vm_ko_*.py` (10개) | 한국어 사실 추출 평가 | `src/agent/ko_extractor/` (실험 코드) |
| 메모리 | `voicemem_local_embed_shim.py` | 로컬 임베딩 shim | `src/agent/memory/embed.py` |
| 인증 | `etri_gate_ko_probe.py` | ETRI 게이트 프로브 | `src/security/etri_gate.py` |
| 측정 | `verify_pipeline.py`, `verify_live.py` | E2E 검증 | `tests/e2e/` |
| 측정 | `bench_*.py` | STT/LLM/TTS 벤치 | `tests/bench/` |
| UI 검증 | `ui_check.mjs`, `wsfix-check.mjs` | 프론트엔드 검증 | `tests/e2e/web/` |

### 12.3 110 서버 인프라 실태 (보강)

| 항목 | 실태 |
|------|------|
| nginx 사이트 설정 | `/etc/nginx/sites-enabled/privseai.com.conf` (391줄) |
| 활성 프록시 | `aivoice.privseai.com`, `/privseai/`, `/aivoice/` → `127.0.0.1:18000` (macOS SSH 역터널) |
| ollama | `127.0.0.1:11434` 바인딩 OK (loopback drop-in `99-loopback.conf` 적용됨) |
| 보유 모델 | 6개 (`qwen2.5:1.5b`, `qwen3:1.7b`, `bge-m3`, `nomic-embed-text` 등) |
| `omniroute:20128` | ✅ 동작, 단 litellm-fallback.yaml의 OPENROUTER_API_KEY 비어있음 → 401 |
| Maya 자체 보안 | LaunchAgents 정리됨, plaintext 키 없음, autossh 정리됨 |
| 작업 시 권한 | **root 사용 금지**. `tts` 등 전용 비-root 유저 + sudoers 일부 명령만 허용 |

### 12.4 즉시 추가 작업 (원안 보강)

1. **nginx location 블록 추가**: `/etc/nginx/sites-enabled/privseai.com.conf`에 `/maya` location 추가하여 macOS 백엔드 프록시 → `nginx -t && systemctl reload nginx` (사용자 직접 실행 권장)
2. **워크스페이스 안 통합 폴더 생성**: `/Users/brianyeon/CodeGPT/maya-2/maya-voice/src/{stt,llm,tts,agent,audio,server,cli}/` (워크스페이스 외부 폴더 대신)
3. **SvelteKit 프론트엔드 위치**: `maya-ui/`가 이미 채워져 있음 (`+page.svelte`, `VoiceOrb.svelte`, `Waveform.svelte`, `Presence.svelte`, `Chat.svelte`, `stores/`, `utils/`, `app.css`, `config.js`)
4. **백엔드 WebSocket**: `maya-ui/backend/`가 비어있음 — 여기에 FastAPI 또는 aiohttp WSS 서버 신규 작성 필요

### 12.5 미해결 / 보류 항목

- `privseai.com/maya` 도메인 DNS 부재 → 도메인 활성화는 사용자 측 진행
- 한국어 음성 복제 (참조 오디오 재녹음 -20~-10dB) — 사용자 녹음 대기
- `MAYA_OPENROUTER_*` 키 발급/저장 (Keychain 또는 새 .env) — 사용자 권한 작업
- 백엔드 WebSocket 서버 (`maya-ui/backend/`) — 본 작업의 다음 단계

