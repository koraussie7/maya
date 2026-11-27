# Digital Twin → Maya 통합 코드플랜 (Scope C)

> **목표**: [ShlokShah01/digital-twin](https://github.com/ShlokShah01/digital-twin)의 GraphRAG 파이프라인, Laya 로컬 reranker, Pocket TTS, 그리고 React 화면(Knowledge/Profile/Graph) 을 Maya에 흡수 통합한다.
>
> **방식**: 디지털 트윈을 별도 서비스로 띄우지 않는다. 핵심 모듈을 **Maya 백엔드로 포팅**하고, UI는 Maya SvelteKit 톤으로 재작성한다. 기존 Maya 코드는 손대지 않는다(추가만).
>
> **진행 방식**: 아래 3개 작업을 **3개 에이전트가 병렬로** 수행한다. 충돌을 막기 위해 사전에 API 계약, 디렉토리 구조, 환경변수명을 모두 고정한다.

---

## 0. 사전 고정 (모든 에이전트가 공유)

### 0.1 디렉토리 추가 (사전 생성 완료)

```
maya-ui/backend/app/rag/                 ← Agent 1
maya-ui/backend/app/tts_pocket.py        ← Agent 2
maya-ui/app/src/routes/knowledge/        ← Agent 3
maya-ui/app/src/lib/api/knowledge.js     ← Agent 3
maya-ui/app/src/lib/stores/voice.svelte.js   ← Agent 2
maya-ui/app/src/lib/stores/knowledge.svelte.js  ← Agent 3
maya-ui/app/src/lib/components/knowledge/   ← Agent 3
```

### 0.2 환경변수 (Agent 1 + Agent 2 가 `config.py` 의 `Settings.from_env()` 에 같은 블록으로 추가)

```bash
# ── RAG / Knowledge (Agent 1) ─────────────────────────────────────
MAYA_RAG_ENABLED=1                   # 0이면 rag 모듈이 no-op
MAYA_RAG_DATA_DIR=~/.cache/maya/rag  # LanceDB 저장 디렉토리
MAYA_RAG_EMBED_MODEL=BAAI/bge-m3    # HF 임베딩 모델 슬러그
MAYA_RAG_CHUNK_TOKENS=400
MAYA_RAG_CHUNK_OVERLAP=80
MAYA_RAG_TOPK_VEC=20                 # 벡터 검색 top-k
MAYA_RAG_TOPK_FINAL=5                # Laya rerank 후 최종 개수
MAYA_RAG_GRAPH_ENABLED=1             # 엔티/커뮤니티 그래프 사용 여부

# ── Laya reranker (Agent 1) ───────────────────────────────────────
MAYA_LAYA_ENABLED=1
MAYA_LAYA_CHECKPOINT=shah-ai/laya-reranker-typed-decisions
MAYA_LAYA_DEVICE=cpu                 # cpu | cuda | mps
MAYA_LAYA_MAX_LENGTH=512
MAYA_LAYA_THRESHOLD=0.55             # Jev 통과선 (router.py 가 이미 사용하던 값과 통일)

# ── Pocket TTS (Agent 2) ──────────────────────────────────────────
MAYA_TTS_PROVIDER=pocket             # piper | voxcpm | pocket
MAYA_POCKET_VOICE=alba               # alba | marius | anna
MAYA_POCKET_DEVICE=cpu
MAYA_POCKET_REPO=kyutai/pocket-tts   # HF repo id
```

### 0.3 공용 API 계약 (Agent 1 ↔ Agent 3 인터페이스)

> Agent 3 은 Agent 1 이 만들기 전이라도 **이 명세만 보고** UI를 만든다. Agent 1 도 같은 명세로 구현한다. 스키마 어긋남 방지.

#### REST 엔드포인트 (prefix `/knowledge`)

| Method | Path                            | 요청                                                  | 응답                                                                                       |
| ------ | ------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| GET    | `/knowledge/health`             | —                                                     | `{status, laya_loaded, doc_count, graph_entity_count}`                                     |
| POST   | `/knowledge/ingest`             | `{profile_id, files: [{name, content_base64, mime}]}` | `{doc_id, chunks_added, entities_added}`                                                   |
| POST   | `/knowledge/search`             | `{query, top_k?, profile_id?}`                        | `{hits: [{chunk_id, text, score, source, profile_id, doc_name, entity_ids, laya_score?}]}` |
| POST   | `/knowledge/delete`             | `{profile_id}`                                        | `{deleted: int}`                                                                           |
| GET    | `/knowledge/profiles`           | —                                                     | `{profiles: [{id, name, doc_count, created_at}]}`                                          |
| GET    | `/knowledge/graph?profile_id=X` | —                                                     | `{nodes: [{id, label, type}], edges: [{src, dst, weight, rel}]}`                           |
| GET    | `/knowledge/profile/{id}`       | —                                                     | `{id, name, doc_count, top_entities: [{id, label, count}]}`                                |

#### 통합 진입점: `/api/chat` (기존)

- 기존 Maya 라우팅은 그대로 둔다. **Jev + Laya 가 통과해 `route_id=null`(abstain) 인 경우에만** RAG 검색을 자동으로 트리거한다.
- 구현 위치: `maya-ui/backend/app/session.py` 의 `_finalize_assistant_text()` 직전 — Agent 1 이 session.py 에 hook 한 줄을 추가한다(전체 로직 X, `from .rag.pipeline import answer_with_rag` 호출만).
- 새 WS 메시지 타입 `citation.stream` (payload `{chunk_id, doc_name, score}`) — UI가 답변 끝난 뒤 각주 카드를 그릴 때 사용.

### 0.4 충돌 방지 규칙

| 파일                                  | 책임 에이전트                                                                                                                                                                                       | 다른 에이전트 금지 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `maya-ui/backend/app/config.py`       | **공유**: Agent 1이 RAG/Laya 필드 추가, Agent 2가 Pocket 필드 추가. **두 에이전트가 동시에 수정하면 안 된다** → Agent 1 이 먼저, Agent 2 가 나중 (또는 사전에 두 블록을 합친 패치를 한 사람이 적용) |
| `maya-ui/backend/app/server.py`       | Agent 1 만 (`include_router(knowledge_router)` 한 줄 추가)                                                                                                                                          |
| `maya-ui/backend/app/session.py`      | Agent 1 만 (`_maybe_rag()` 헬퍼 한 개 추가)                                                                                                                                                         |
| `maya-ui/backend/app/rag/`            | Agent 1 전용 (신규)                                                                                                                                                                                 |
| `maya-ui/backend/app/tts_pocket.py`   | Agent 2 전용 (신규)                                                                                                                                                                                 |
| `maya-ui/backend/app/tts*.py`         | Agent 2 만 (디스패치 추가)                                                                                                                                                                          |
| `maya-ui/app/src/routes/+page.svelte` | Agent 2 만 (음성 메뉴 항목 추가)                                                                                                                                                                    |
| `maya-ui/app/src/routes/knowledge/**` | Agent 3 전용                                                                                                                                                                                        |
| `maya-ui/app/src/lib/mayaClient.js`   | **어느 에이전트도 수정 금지** — REST API는 fetch 로 직접 호출                                                                                                                                       |

---

## 1. 에이전트 분할

### Agent 1 — Backend: GraphRAG + Laya Reranker + Knowledge API

**산출물**:

- `maya-ui/backend/app/rag/loader.py` — `.md/.txt/.pdf` 로드, base64 디코드
- `maya-ui/backend/app/rag/chunker.py` — 토큰 청크 (tiktoken cl100k)
- `maya-ui/backend/app/rag/embedder.py` — `sentence-transformers` 로 BGE 임베딩 (없으면 `hashlib.md5` 폴백)
- `maya-ui/backend/app/rag/store.py` — LanceDB 래퍼 (`MAYA_RAG_DATA_DIR/lance/{profile_id}`)
- `maya-ui/backend/app/rag/graph.py` — 엔티/커뮤니티 추출 (regex + 동시출현 네트워크) — 디지털 트윈 `src/digital_twin/graph/` 포팅
- `maya-ui/backend/app/rag/laya_reranker.py` — 디지털 트윈 `src/digital_twin/laya/inference.py` 포팅 (`typed-decisions` 체크포인트, CPU 디바이스)
- `maya-ui/backend/app/rag/pipeline.py` — `answer_with_rag(query, profile_id)` → 검색→rerank→컨텍스트 문자열
- `maya-ui/backend/app/knowledge_router.py` — §0.3 명세 그대로 7개 엔드포인트
- `maya-ui/backend/app/config.py` — §0.2 의 RAG/Laya 블록 추가
- `maya-ui/backend/app/server.py` — `app.include_router(knowledge_router.app, prefix="/knowledge")` 한 줄
- `maya-ui/backend/app/session.py` — `_maybe_rag()` 훅 한 개 추가 (Jev abstain 시 자동 호출, citation.stream WS 메시지 emit)

**수용 기준**:

1. `curl /knowledge/health` 가 `laya_loaded` 를 `true` 로 반환 (체크포인트 다운로드는 lazy load, 첫 요청 시)
2. `curl -X POST /knowledge/ingest -d '{"profile_id":"p1","files":[…]}'` 후 `/knowledge/search` 가 인용 가능한 텍스트를 반환
3. Laya 점수가 `MAYA_LAYA_THRESHOLD` 미만이면 abstain 처리 (디지털 트윈의 `laya.inference.classify_with_threshold` 그대로)
4. 기존 `/api/chat` 와 `/decisions` 페이지가 그대로 동작 — 회귀 없음
5. 단위 테스트 2개: `tests/test_rag_chunk.py`, `tests/test_laya_threshold.py`

**참조 소스**:

- https://github.com/ShlokShah01/digital-twin/tree/main/src/digital_twin/retrieval
- https://github.com/ShlokShah01/digital-twin/tree/main/src/digital_twin/laya
- https://github.com/ShlokShah01/digital-twin/tree/main/src/digital_twin/graph

---

### Agent 2 — Backend: Pocket TTS Provider + Voice Menu

**산출물**:

- `maya-ui/backend/app/tts_pocket.py` — Pocket TTS 래퍼 (디지털 트윈 `src/digital_twin/tts/pocket.py` 포팅)
  - `class PocketTTS` 로 `synth(text, voice) -> bytes(WAV)` 구현
  - `MAYA_POCKET_DEVICE=cpu` 기본, mps/cuda 시도 후 실패 시 cpu 폴백
  - 모델 로드는 lazy, 첫 호출 시 HF 캐시
- `maya-ui/backend/app/tts.py` (존재 시) 또는 `session.py` 의 TTS 디스패치 지점에 `if SETTINGS.tts_provider == "pocket": return await _pocket_synth(text, voice)` 한 줄
- `maya-ui/backend/app/config.py` — §0.2 의 Pocket 블록 추가 (Agent 1 작업 완료 후)
- `maya-ui/app/src/lib/stores/voice.svelte.js` — 신규 (`voices` 배열, `selectedVoice`, `setVoice()`)
- `maya-ui/app/src/routes/+page.svelte` — 기존 VOICES 배열에 `{id: 'pocket_alba', label: 'Pocket · Alba'}` 등 3개 추가, `pickVoice()` 가 서버 `session.start` 시 `tts_provider` 와 `tts_voice` 를 함께 전송하도록 확장

**수용 기준**:

1. `MAYA_TTS_PROVIDER=pocket MAYA_POCKET_VOICE=alba` 로 백엔드 기동 후 음성 합성 호출 시 WAV 바이트 반환
2. `MAYA_TTS_PROVIDER=piper` (기본값) 으로 두면 기존 동작 그대로 유지 — 회귀 없음
3. UI 의 음성 메뉴에서 Pocket 3종 선택 시 WS `session.start` 페이로드에 `{tts_provider:"pocket", tts_voice:"alba"}` 포함
4. 단위 테스트 1개: `tests/test_tts_pocket_smoke.py` — 모델 mock 으로 synth 호출이 0.5s 안에 bytes 를 반환하는지

**참조 소스**:

- https://github.com/ShlokShah01/digital-twin/tree/main/src/digital_twin/tts

---

### Agent 3 — Frontend: Knowledge + Profile + Graph 페이지

**산출물**:

- `maya-ui/app/src/routes/knowledge/+page.svelte` — Knowledge 메인 (좌: 프로필 목록 + 업로드, 우: 최근 검색 결과 + 인용 카드)
- `maya-ui/app/src/routes/knowledge/profile/[id]/+page.svelte` — 프로필 상세 (문서 목록, 청크 미리보기, 상위 엔티)
- `maya-ui/app/src/routes/knowledge/graph/+page.svelte` — 엔티/커뮤니티 그래프 시각화 (D3 force layout, SVG)
- `maya-ui/app/src/lib/api/knowledge.js` — §0.3 명세 그대로 fetch 래퍼
- `maya-ui/app/src/lib/stores/knowledge.svelte.js` — Svelte 5 runes 모드 (기존 `chat.svelte.js` 패턴 따라감)
- `maya-ui/app/src/lib/components/knowledge/CitationCard.svelte` — 인용 카드 (chunk 텍스트 + doc_name + score)
- `maya-ui/app/src/lib/components/knowledge/EntityGraph.svelte` — D3 force 그래프 컴포넌트
- `maya-ui/app/src/lib/components/knowledge/IngestDropzone.svelte` — 드래그앤드롭 업로드
- `maya-ui/app/src/routes/+page.svelte` 의 NAV 배열에 `{label: '지식', href: '${base}/knowledge'}` 추가

**수용 기준**:

1. `/knowledge` 페이지에서 파일 업로드 → 5초 안에 프로필 목록에 반영
2. 검색창에 질문 입력 → CitationCard 5개 이내 표시, 점수 내림차순
3. `/knowledge/graph` 페이지에서 노드 20개 이상이면 force layout 이 60fps 유지 (requestAnimationFrame)
4. 기존 `/` (대화) 와 `/decisions` 페이지가 그대로 동작 — 회귀 없음
5. CSS 토큰은 기존 `app.css` 의 `--bg-*`, `--fg-*`, `--accent-*` 변수를 그대로 사용

- 디지털 트윈의 `frontend/src/pages/Chat.tsx`, `Profile.tsx`, `Graph.tsx` 를 **시각적 참고만** 한다 — SvelteKit + Svelte 5 runes 로 재작성 (React 코드 복붙 금지)
- https://github.com/ShlokShah01/digital-twin/tree/main/frontend/src/pages

---

## 2. 병렬 실행 순서

```
T0: Agent 1, 2, 3 동시 시작
  - Agent 1: rag/ + knowledge_router.py + config.py(RAG블록) + server.py + session.py 훅 **착수 완료**
  - Agent 2: tts_pocket.py + config.py(Pocket블록, Agent1 완료 대기) + voice.svelte.js +page.svelte 음성 메뉴 **착수 완료**
  - Agent 3: knowledge/+page.svelte + profile/[id]+page.svelte + graph/+page.svelte
             + api/knowledge.js + stores + components (Agent 1 API 와 독립 — §0.3 계약만 사용) **착수 완료**
           - Agent 1: rag/ + knowledge_router.py + config.py(RAG블록) + server.py + session.py 훅
           - Agent 2: tts_pocket.py + config.py(Pocket블록, Agent1 완료 대기) + voice.svelte.js +page.svelte 음성 메뉴
           - Agent 3: knowledge/+page.svelte + profile/[id]/+page.svelte + graph/+page.svelte
                      + api/knowledge.js + stores + components (Agent 1 API 와 독립 — §0.3 계약만 사용)
T1 (Agent 1 종료 후):
           Agent 2 가 config.py 의 Pocket 블록을 추가한다 (충돌 없음, Agent1 작업 끝남)
T2 (모든 에이전트 종료):
           통합 테스트:
             1) 백엔드: `pytest maya-ui/backend/tests -q`
             2) E2E: `run_maya_backend.sh` 기동 → Playwright 스크립트로 /knowledge 업로드+검색 → 스크린샷
             3) 음성: Pocket 으로 전환 후 한국어 한 문장 합성 → WAV 저장 → ffprobe 로 길이 확인
           회귀 테스트:
             1) 기존 /decisions 페이지 — Laya reranker 포팅이 결정 로그 출력을 깨지 않는지
             2) 기존 / 페이지 — 음성 합성 정상
```

## 3. 회귀 방지를 위한 절대 규칙

1. **기존 파일 import 추가만, 함수 본문 수정 금지** (예외: `session.py` 의 `_maybe_rag()` 한 줄 hook 은 허용)
2. **새 WS 메시지 타입은 `ServerMsgType` enum 에 추가만** (`maya-ui/backend/app/protocol.py`)
3. **`MAYA_RAG_ENABLED=0`** 또는 **`MAYA_TTS_PROVIDER=piper`** 면 모든 신규 모듈이 no-op — 기본값으로 두어 기존 사용자 환경에 영향 없도록
4. **LanceDB / Pocket 모델 미설치 시** 에러 메시지를 명확히 (예: "`pip install lancedb sentence-transformers` 또는 `MAYA_RAG_ENABLED=0` 으로 비활성화")
5. **모델 다운로드 위치**: `HF_HOME=~/.cache/maya/hf` 기본 (이미 환경에 있을 가능성 높음)

## 4. 검증 (Done 정의)

| Done         | 검증 방법                                                                              |
| ------------ | -------------------------------------------------------------------------------------- |
| Agent 1 완료 | `curl http://127.0.0.1:8765/knowledge/health` → `{"laya_loaded":true,...}`             |
| Agent 2 완료 | `MAYA_TTS_PROVIDER=pocket` 으로 서버 기동 → 음성 합성 1회 → ffprobe 로 0.5~3s 사이 WAV |
| Agent 3 완료 | Playwright 스크린샷 `/tmp/maya-knowledge.png` 가 프로필 목록 + CitationCard 표시       |
| 통합 완료    | 위 3개 모두 + 기존 `/decisions`, `/` 회귀 테스트 통과                                  |
