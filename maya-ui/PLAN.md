# Maya 음성 비서 프로젝트 플랜

> Mizu 디자인 시스템 (SvelteKit + Svelte 5 + Tailwind v4) 기반 AI 음성 비서 구현 계획  
> 기존 STT/TTS 작업 (Kitten TTS, Chatterbox, Whisper 등) 연동 포함

## 1. 개요

- **프로젝트명**: Maya 음성 비서 (Maya Voice Assistant)
- **UI 프레임워크**: [Mizu](https://github.com/dyascj/mizu) — AI 제품용 디자인 시스템
- **프론트엔드**: SvelteKit, Svelte 5, Tailwind CSS v4
- **백엔드**: Python (FastAPI 또는 aiohttp 기반 WebSocket 서버)
- **음성 처리**: STT (Whisper), LLM (OpenRouter 또는 로컬 모델), TTS (Kitten TTS / Chatterbox / XTTS)
- **목표**: 웹 기반 음성 인터페이스 + 음성 오브(Voice Orb) 시각화 + 실시간 스트리밍 대화

## 2. 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│  Maya Frontend (SvelteKit + Mizu)                       │
│  ├── Voice Orb (mizu voice component)                   │
│  ├── Waveform (실시간 오디오 시각화)                    │
│  ├── Chat UI (streaming text, reasoning, tool calls)    │
│  └── Mobile Assistant Block (PWA 설치 가능)             │
└──────────────┬──────────────────────────────────────────┘
               │ WebSocket / HTTP
┌──────────────▼──────────────────────────────────────────┐
│  Maya Backend (Python)                                  │
│  ├── STT 서버 (Whisper small/medium)                    │
│  ├── LLM 라우터 (OpenRouter / 로컬 LLM)                 │
│  ├── TTS 엔진 (Kitten / Chatterbox / XTTS v2)           │
│  └── 세션 관리 (대화 상태, 컨텍스트 윈도우)             │
└─────────────────────────────────────────────────────────┘
```

### 2.1 데이터 흐름

1. 사용자가 Voice Orb 클릭 → 마이크 권한 요청 → 오디오 캡처 시작
2. 프론트엔드: 오디오 청크 → 백엔드 WebSocket 전송
3. 백엔드: STT (Whisper) → 텍스트 변환 → LLM 처리 → 스트리밍 응답
4. 백엔드: LLM 토큰 스트리밍 → TTS (실시간 음성 생성) → 오디오 스트림 반환
5. 프론트엔드: 오디오 재생 + Waveform 시각화 + 텍스트 스트리밍 표시
## 3. 기술 스택

| 계층 | 기술 | 용도 |
|------|------|------|
| 프레임워크 | SvelteKit 2.x | 서버사이드 렌더링, API 라우트 |
| UI 라이브러리 | Mizu (dyascj/mizu) | Voice Orb, Chat, Presence, Waveform 등 |
| 스타일 | Tailwind CSS v4 | 디자인 토큰, 다크모드 |
| 반응성 | Svelte 5 (runes) | 상태 관리, 이벤트 처리 |
| 백엔드 | Python 3.11+, FastAPI | WebSocket, REST API |
| STT | Whisper (small/medium) | 음성 → 텍스트 |
| LLM | OpenRouter API / 로컬 vLLM | 대화 생성, 함수 호출 |
| TTS | Kitten TTS / Chatterbox / XTTS v2 | 텍스트 → 음성 (한국어 지원 검토) |
| 오디오 | Web Audio API, opus | 브라우저 오디오 캡처/재생 |
| 배포 | Docker, Vercel / Railway | 프론트엔드 + 백엔드 배포 |

## 4. 파일 구조 (제안)

```
maya-voice-assistant/
├── apps/
│   ├── web/
│   │   ├── src/
│   │   │   ├── lib/
│   │   │   │   ├── components/
│   │   │   │   │   ├── VoiceOrb.svelte
│   │   │   │   │   ├── Waveform.svelte
│   │   │   │   │   └── Chat.svelte
│   │   │   │   ├── stores/
│   │   │   │   └── utils/
│   │   │   ├── routes/
│   │   │   │   ├── +page.svelte
│   │   │   │   └── api/
│   │   │   └── app.html
│   │   ├── package.json
│   │   └── svelte.config.js
│   └── backend/
│       ├── app/
│       │   ├── stt/
│       │   ├── tts/
│       │   ├── llm/
│       │   ├── audio/
│       │   └── session/
│       ├── main.py
│       ├── requirements.txt
│       └── .env
├── packages/
│   └── shared/
├── docs/
│   ├── CUSTOM_VOICE_INTEGRATION_ISSUES.md
│   └── mizu-integration.md
├── tests/
│   ├── e2e/
│   └── unit/
├── docker-compose.yml
├── README.md
└── AGENTS.md
```

## 5. 구현 단계 (마일스톤)

### Phase 1: 기반 구축 (1~2주)

| 작업 | 내용 | Mizu 연동 |
|------|------|------------|
| 1.1 | 저장소 초기화, SvelteKit + Tailwind v4 설정 | `pnpm create svelte` |
| 1.2 | Mizu 설치 및 컴포넌트 복사 | `mizu add voice-orb waveform chat` |
| 1.3 | 백엔드 FastAPI 프로젝트 생성 | Python 가상환경, 의존성 설치 |
| 1.4 | WebSocket 연결 테스트 | 프론트엔드 ↔ 백엔드 연결 확인 |
| 1.5 | 기존 TTS/STT 코드 연동 검토 | `maya-2/docs/CUSTOM_VOICE_INTEGRATION_ISSUES.md` 참조 |

### Phase 2: 음성 입출력 파이프라인 (2~3주)

| 작업 | 내용 |
|------|------|
| 2.1 | 브라우저 마이크 캡처 (MediaRecorder 또는 Web Audio API) |
| 2.2 | 오디오 청크 인코딩 → WebSocket 전송 |
| 2.3 | 백엔드 STT (Whisper) 구현 |
| 2.4 | LLM 스트리밍 응답 (OpenRouter 또는 로컬) |
| 2.5 | TTS 음성 생성 (Kitten TTS 버그 수정 후, 또는 Chatterbox 대체) |
| 2.6 | 오디오 스트림 → 프론트엔드 재생 |
| 2.7 | Voice Orb 상태 연동 (듣기 / 생각하는 중 / 말하기) |

### Phase 3: UI/UX 및 음성 오브 커스터마이징 (2주)

| 작업 | 내용 |
|------|------|
| 3.1 | Mizu Voice Orb 커스텀 컬러/애니메이션 (Maya 브랜드 컬러) |
| 3.2 | Waveform 실시간 시각화 (입력/출력 오디오 구분) |
| 3.3 | Presence 컴포넌트 (어시스턴트 상태 표시: "듣고 있어요", "생각 중") |
| 3.4 | 채팅 기록 표시 (streaming text, reasoning, sources) |
| 3.5 | 모바일 대응 (Mizu Mobile Assistant Block 적용) |
| 3.6 | 웨이크 워드 (선택: Porcupine 또는 간단한 에너지 기반 트리거) |

### Phase 4: 한국어 음성 복제 및 품질 개선 (2주)

| 작업 | 내용 |
|------|------|
| 4.1 | 참조 오디오 재녹음 (15~30초, -20dB~ -10dB) |
| 4.2 | 한국어 TTS 엔진 검토 (Chatterbox / XTTS v2 / Coqui) |
| 4.3 | TTS 품질 평가 및 하이퍼파라미터 조정 |
| 4.4 | 전체 엔드투엔드 지연 시간 측정 및 최적화 |

### Phase 5: 배포 및 운영 (1주)

| 작업 | 내용 |
|------|------|
| 5.1 | Docker 이미지 구성 (프론트엔드 + 백엔드) |
| 5.2 | 환경 변수 관리 (.env, API 키 보안) |
| 5.3 | Vercel / Railway / Fly.io 배포 |
| 5.4 | 모니터링 및 로깅 |
| 5.5 | 사용자 테스트 및 피드백 반영 |

## 6. Mizu 주요 컴포넌트 활용 계획

| Mizu 컴포넌트 | Maya에서의 역할 | 구현 우선순위 |
|--------------|----------------|-------------|
| Voice Orb | 음성 상태 시각화 (중앙 UI) | 🔴 높음 |
| Waveform | 마이크 입력 / TTS 출력 파형 | 🔴 높음 |
| Chat Input | 텍스트 입력 대체/보완 | 🟡 중간 |
| Streaming Text | LLM 응답 실시간 표시 | 🔴 높음 |
| Reasoning | LLM 생각 과정 표시 (선택) | 🟡 중간 |
| Presence | 어시스턴트 상태 (듣기/생각/말하기) | 🔴 높음 |
| Mobile Assistant Block | PWA 설치 가능 모바일 앱 | 🟡 중간 |
| Greeting | 초기 인사 메시지 | 🟢 낮음 |
| Prompt Suggestions | 추천 질문 칩 | 🟢 낮음 |

## 7. 기존 작업 연동 사항

이전 `maya-2` 프로젝트의 결과물을 재사용합니다.

| 기존 항목 | 경로 | 활용 방안 |
|----------|------|----------|
| 참조 오디오 | `maya/models/tts/reference/new_recording_16k.wav` | 음성 복제용 참조 클립 |
| TTS 이슈 문서 | `maya-2/docs/CUSTOM_VOICE_INTEGRATION_ISSUES.md` | Kitten TTS 버그 수정 참고 |
| Whisper 전사 | 기존 전사 결과 | 한국어 STT 품질 기준선 |
| espeak-ng 환경 | 기존 env 설정 | TTS 전처리 파이프라인 참고 |

### Kitten TTS 해결 방안 (연동 시)

```python
# voices.npz 스타일 shape 수정
style = voice.mean(axis=0, keepdims=True)  # (400, 256) → (1, 256)

# input_ids 2차원 변환
input_ids = np.array([[0] + token_ids + [0]], dtype=np.int64)  # (1, seq_len)
```

한국어 미지원 시 **Chatterbox TTS** 또는 **XTTS v2**로 대체 검토.

## 8. 보안 및 운영 고려사항

| 항목 | 조치 |
|------|------|
| API 키 | `.env` 파일 관리, Git 커밋 금지 (OpenRouter 키 유출 경험 반영) |
| 오디오 데이터 | 녹음 파일 암호화 저장, 보관 기간 설정 |
| CORS | 프론트엔드 도메인만 허용 |
| Rate Limiting | STT/TTS API 호출 제한 |
| HTTPS | 프로덕션에서 WSS 필수 (마이크 접근 보안) |

## 9. 위험 요소 및 완화 방안

| 위험 | 영향도 | 완화 방안 |
|------|--------|----------|
| Kitten TTS 한국어 미지원 | 높음 | Chatterbox / XTTS v2 대체 검토 |
| STT 전사 품질 낮음 (조용한 오디오) | 중간 | 참조 오디오 재녹음, 노이즈 제거 |
| 엔드투엔드 지연 시간 | 중간 | 스트리밍 STT/TTS 적용, 에지 캐싱 |
| Mizu 컴포넌트 커스텀 한계 | 낮음 | 소스 코드 복사 후 직접 수정 (Mizu 원칙) |
| OpenRouter 키 재유출 | 높음 | 키 즉시 폐기, 환경 변수 관리, 사용량 알림 설정 |

## 10. 성공 지표 (KPI)

- [ ] 음성 입력 → 응답까지 엔드투엔드 지연 < 3초
- [ ] 한국어 STT 정확도 > 85% (Whisper small 기준)
- [ ] TTS 음성 자연도 주관적 평가 4.0/5.0 이상
- [ ] 모바일 PWA 설치 및 오프라인 동작 확인
- [ ] Voice Orb 상태 전환 부드럽지 않음 (60fps)

## 11. 향후 확장 아이디어

- 웨이크 워드 ("헤이 마야") 상시 대기
- 다중 사용자 세션 분리
- 함수 호출 (Function Calling) 연동 (일정, 메모, 스마트홈)
- 음성 명령으로 로컬 파일 제어
- 오프라인 모드 (로컬 LLM + 로컬 TTS)

## 📌 즉시 실행할 작업

1. `maya-voice-assistant/` 디렉토리 생성
2. SvelteKit 프로젝트 초기화 (`pnpm create svelte@latest apps/web`)
3. Mizu 컴포넌트 설치 (`mizu add voice-orb waveform chat presence`)
4. Python 백엔드 뼈대 생성 (`fastapi + websockets`)
5. 기존 TTS/STT 코드 연동 테스트