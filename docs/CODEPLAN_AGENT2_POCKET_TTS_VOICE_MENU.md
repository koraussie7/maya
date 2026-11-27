# Agent 2 — Pocket TTS 통합 + 음성 메뉴 작업 플랜

**상태**: 진행 시작 (`pocket_tts` 통합은 외부 의존 없이 진행 가능 — 디지털 트윈
코드를 가져오지 않고도, 같은 gradio HTTP 스펙을 직접 호출한다)

**범위**: `pocket_tts` (HF Space `pocket-tts`) 백엔드를 Maya TTS 라우터에 합류시키고,
프론트엔드에 음성 메뉴(여성 / 남성 / 시스템 기본)를 노출한다.

## 0. 핵심 결정

- **적분 방식 — "Adapter over HTTP"**: `digital-twin/src/digital_twin/tts/pocket.py` 를
  가져오는 대신, Pocket-TTS 가 노출하는 gradio HTTP 엔드포인트(`/pocket_tts`)를 직접
  호출하는 얇은 `MayaPocketTTS` 클라이언트를 Maya 안에 둔다. 이유:

1.  디지털 트윈은 HF Space 와 1:1 매핑이라, 어댑터를 한 번만 만들면 된다.
2.  디지털 트윈은 0.5GB+ 의 torch 의존성을 끌어오므로 Maya 가 부풀지 않게 막는다.
3.  `gr.Client` 만 사용하면 import 도 가볍다 (`gradio-client`).

- **가용성 감지**: 부팅 시 `gr.Client.duplicate(src).view_api(return_format="dict")` 로
  엔드포인트 노출 여부를 확인. 살아있으면 active, 죽으면 `piper/voxcpm` 로 graceful
  fallback (실측: Pocket은 GPU 자원이 줄면 자주 죽는다).

- **메뉴는 두 곳**:
  - 백엔드: TTS 프로바이더 목록을 `settings.tts_providers` 로 노출
  - 프론트엔드: `+page.svelte` 의 VoiceOrb 옆 드롭다운 → WS `chat.text` 에 `voice_pref`
    페이로드로 전달 → 백엔드 `TTSRouter.synthesize()` 가 즉시 분기

## 1. 변경 파일 (작은 변경)

| 파일                                                | 변경                                                                     |
| --------------------------------------------------- | ------------------------------------------------------------------------ |
| `maya-ui/backend/app/tts_pocket.py` _(신규)_        | `MayaPocketTTS` — `gr.Client` 어댑터, `synthesize(text)` → `bytes` (WAV) |
| `maya-ui/backend/app/tts_router.py` _(신규)_        | `TTSRouter` — 우선순위 + 가용성 자동감지 + fallback 체인                 |
| `maya-ui/backend/app/config.py`                     | `tts_providers`, `pocket_tts_url`, `voice_default` 추가                  |
| `maya-ui/backend/app/session.py`                    | `session.start` payload 에 `tts_providers`/`tts_active` 실어 보내기      |
| `maya-ui/backend/app/persona.py`                    | `voice_pref` → Persona.voice_ref_wav 오버라이드                          |
| `maya-ui/app/src/lib/stores.ts` _(신규)_            | `voiceStore` (선택 음성 상태)                                            |
| `maya-ui/app/src/routes/+page.svelte`               | 음성 메뉴 드롭다운 + Persona 변경 WS 송출                                |
| `maya-ui/backend/tests/test_tts_pocket.py` _(신규)_ | 4 케이스 (가용성 / 합성 / 라우팅 / fallback)                             |
| `maya-ui/backend/tests/test_tts_e2e.py` _(신규)_    | WS E2E 1 케이스 (audio.out mime=pocket 인지)                             |

## 2. 작업 단계

### Phase A — Pocket TTS 어댑터 + 가용성 감지

1. `tts_pocket.py` 작성 — `gr.Client` 기반 `MayaPocketTTS`
2. 가용성 체크 함수 `is_alive(timeout=2.0)` — `view_api` 호출 + 예외 시 False
3. `tts_router.py` 작성 — `TTSRouter(providers=[pocket, piper, voxcpm])`
4. `MAYA_TTS_PROVIDERS` (csv) 환경변수로 우선순위 지정 (기본 `pocket,piper,voxcpm`)

### Phase B — 백엔드 와이어업

1. `config.py` 에 Pocket URL 추가 (`POCKET_TTS_URL=https://kyutai-pocket-tts.hf.space`)
2. `session.start` 핸들러에서 `tts_providers=[...]` 실어 보내기
3. `chat.text` payload 의 `voice_pref` 필드 → `TTSRouter.synthesize(text, voice_pref)`
4. `persona.changed` 흐름과 호환 (음성 메뉴 ↔ 페르소나 메뉴가 같은 인터페이스 사용)

### Phase C — 프론트엔드 음성 메뉴

1. `+page.svelte` — `<select>` 바인딩, options = `session.tts_providers`
2. `voiceStore` — 선택된 음성을 `chat.text` 의 `voice_pref` 에 자동 첨부
3. 음성 변경 시 즉시 `persona.changed` 또는 새로 추가하는 `voice.change` 메시지로 송출

### Phase D — 테스트

1. 유닛:
   - `test_pocket_tts_adapter_alive` — mock `gr.Client`
   - `test_pocket_tts_adapter_synthesize` — WAV bytes 반환
   - `test_tts_router_fallback` — pocket 죽으면 piper로
   - `test_tts_router_voice_pref` — voice_pref 로 분기
2. E2E: `test_tts_e2e` — `pytest-asyncio` 로 `WS /ws` 열고 `chat.text` → `audio.out`
   의 `mime` 에 `pocket` 또는 `piper` 가 들어오는지 검증
3. 환경변수 매트릭스:
   - `MAYA_TTS_PROVIDERS=voxcpm,pocket,piper` → 우선순위 변경 동작
   - `POCKET_TTS_URL=`(빈값) → 자동 제외

## 3. 외부 의존

- `gradio-client` (이미 `requirements.txt` 에 있는지 확인 필요 — 없으면 추가)
- HF Space `pocket-tts` — 무료 GPU Space 이므로 변동 가능 → fallback 필수

## 4. 검증 체크리스트

- [ ] 부팅 로그에 `tts_router active=pocket piper=ok voxcpm=ok` 가 찍힌다
- [ ] `session.ready` 의 `tts_providers` 가 프론트로 전달된다
- [ ] 음성 메뉴에서 Pocket 선택 → 응답의 `audio.out` mime 이 `pocket` 이다
- [ ] Pocket 죽이면(pocket_tts_url=invalid) piper 로 자동 폴백
- [ ] 테스트 4 + E2E 1 모두 통과
- [ ] 기존 piper-only 세션 회귀 없음

## 5. 위험 요소

- **HF Space cold start** — 30s+ 걸리면 첫 음성이 늦다 → 합성 직전 미리 warm-up
- **gradio API 변경** — HF Space 업그레이드 시 엔드포인트명 깨질 수 있음
  → 어댑터에 `endpoint_name` 환경변수로 노출
- **메모리** — Pocket은 ONNX 모델이라 가볍지만, 동시 요청이 폭주하면 OOM 가능
  → 라우터에 동시성 제한(`asyncio.Semaphore`) 추가

## 6. 진행 순서

1. (지금) 본 문서 작성 ✅
2. `requirements.txt` 확인 → `gradio-client` 추가
3. `tts_pocket.py` + 가용성 체크 (Phase A.1~3)
4. `tts_router.py` + fallback (Phase A.4)
5. `config.py` / `session.py` / `persona.py` 와이어업 (Phase B)
6. 프론트 음성 메뉴 (Phase C)
7. 유닛 + E2E 테스트 (Phase D)
8. 음성 메뉴 회귀 테스트
