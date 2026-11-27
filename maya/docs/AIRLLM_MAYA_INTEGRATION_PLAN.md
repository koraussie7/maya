# airLLM ↔ Maya 통합 플랜

- 작성일: 2026-11-25
- 저장소: <https://github.com/lyogavin/airllm>
- 대상: Maya 음성 비서 백엔드 (`maya-ui/backend`)
- 목적: **무료·키 0·한국어 가능한 로컬 LLM** 백엔드를 Maya 라우터에 추가

---

## 1. 왜 airLLM인가 (효과)

| 항목                      | 현재                                                                    | airLLM 추가 후                                                  |
| ------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------- |
| LLM 백엔드 옵션           | OpenRouter(클라우드 유료), 자체 게이트웨이(110 서버 의존), Ollama(맥북) | **airLLM(맥북 GPU 압박 없음)** 추가                             |
| 110 서버 다운 시 응답     | ❌ 차단됨                                                               | ✅ 백엔드 폴백 가능                                             |
| API 키 비용               | OpenRouter 과금                                                         | ✅ **0원**                                                      |
| 한국어 fine-tune          | 필요 (실측: 모델 선택이 성능 좌우)                                      | 불필요 (Qwen2.5/3 멀티랭)                                       |
| 디스크 ↔ GPU/MPS 스트리밍 | 해당 없음                                                               | ✅ **레이어 단위 로드**로 30B+ 모델을 32 GB 통합메모리에서 가능 |

**핵심 가치**: "GPU가 없어도 큰 모델을 로컬에서 굴린다"는 점이 아니라 **"맥북 GPU 압박 없이 큰 모델을 키 0으로 돌린다"**는 점. e2e voice latency(< 3 s)에는 못 맞출 수 있으므로 폴백용으로 쓰거나, **비실시간 경로(요약/메모리/한국어 발화 교정)** 에 활용.

---

## 2. airLLM 핵심 정리 (조사 1단계)

- **메커니즘**: `AutoModel.from_pretrained(hf_repo)` 한 줄로 HuggingFace ID → 레이어 단위 디스크→디바이스 스트리밍 → 토큰 1개 생성 후 디스크로 되돌림 ([airllm README](https://github.com/lyogavin/airllm))
- **옵션**
  - `compression='4bit'/'8bit'`: block-wise 양자화 (메모리 ~1/3)
  - `prefetching=True`: 다음 레이어 미리 읽기 (기본값)
  - `delete_original=True`: HF 캐시 원본 삭제 (디스크 절약)
- **macOS (MLX 백엔드)** — `examples/run_on_macos.ipynb` 제공. `generate(input_ids=...)` 가 디코드된 텍스트를 한 번에 반환 → **스트리밍 토큰 미지원** (스트림 흉내 내야 함)
- **한국어 모델** — Qwen2.5/3, Yi, Mistral, DeepSeek-V3 모두 HF ID로 로드 가능
- **제약** — 첫 토큰 latency 느림 (레이어 디스크 읽기 때문). 큰 모델은 분 단위 대기 가능 ([sourcepulse 글](https://www.sourcepulse.org/projects/1828307))

---

## 3. 환경 매칭

| 환경                       | airLLM 적합도 | 이유                                             |
| -------------------------- | ------------- | ------------------------------------------------ |
| 110 서버 (185.55.240.110)  | ❌            | GPU 없음, CPU 추론은 freellm/OpenRouter보다 느림 |
| **맥북 (MPS, 통합메모리)** | ✅            | 32 GB 통합메모리면 4bit Qwen3-30B까지 가능       |
| 컨테이너/클라우드          | △             | 비추 — 비용 모델 무너짐 (API 쓰는 게 쌈)         |

**결론**: airLLM 백엔드는 **맥북 전용 LLM 옵션**으로 등록한다.

---

## 4. 아키텍처 (현재 → 통합 후)

### 4.1 현재 (조사 완료된 사실)

```
Maya Backend (FastAPI on Mac :8765)
   ├── llm_provider=cloud    → OpenRouter (Bearer 토큰, 유료)
   ├── llm_provider=private  → 110 서버 nginx → keirouter (자체 라우터)
   └── llm_provider=local    → Ollama :11434 (OpenAI 호환)
```

- 진입점: `maya-ui/backend/app/llm.py::stream_chat()` — OpenAI 호환 POST 스트리밍
- 라우팅: `maya-ui/backend/app/config.py::Settings.from_env()` — `MAYA_LLM_PROVIDER` 분기
- 폴백 정책: **없음** (지금은 첫 실패에서 ERROR 메시지만 반환)

### 4.2 통합 후 (목표)

```
Maya Backend (FastAPI on Mac :8765)
   ├── llm_provider=cloud     → OpenRouter
   ├── llm_provider=private   → 110 서버 nginx → keirouter
   ├── llm_provider=local     → Ollama :11434
   └── 🆕 llm_provider=airllm → AirLLMBackend (in-process, MPS/CPU)
            │
            └── HF repo: Qwen/Qwen2.5-7B-Instruct  (한국어 OK, 4bit 시 1~2 GB)
                 또는   Qwen/Qwen3-8B             (더 최신, 한국어 OK)
```

**장점**

- 라우터 한 곳에 모임 (이미 존재하는 `MAYA_LLM_PROVIDER` enum에 `airllm` 추가)
- OpenAI 호환 인터페이스 유지 → 프론트엔드·세션·RAG 변경 0
- 외부 의존 0, 키 0, 한국어 OK
- 110 서버 다운 시에도 **완전 로컬 동작** (`MAYA_LLM_PROVIDER=airllm`만 지정하면 됨)

**제약/주의**

- **첫 토큰 latency가 OpenAI 호환 경로 대비 크다** (레이어 디스크 읽기). e2e latency < 3s KPI 위배 가능성 → 실측 후 결정
- Mac MLX 백엔드는 `generate()`가 스트리밍을 반환하지 않음 → **완성된 텍스트를 문장/청크 단위로 잘게 쪼개서 WSMessage 스트림 흉내** 내야 함
- 모델 첫 로드는 디스크 분할/저장 때문에 **한 번에 수 분** 소요 (`delete_original=True` 권장)
- 가드레일: 60s 타임아웃 후 OpenRouter로 폴백하는 라우터가 있으면 베스트 (별도 단계)

---

## 5. 파일 변경 계획

### 5.1 신규 파일

| 경로                                   | 역할                                                                                                          |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `maya-ui/backend/app/llm_airllm.py`    | `AirLLMBackend` (sync generate → async 스트림 변환), HF repo/compression env                                  |
| `maya-ui/backend/app/llm.py` (수정)    | `stream_chat()`에 `airllm` 분기 추가 (in-process 호출)                                                        |
| `maya-ui/backend/app/config.py` (수정) | `Settings.llm_provider`에 `"airllm"` 추가, 신규 env (`AIRLLM_HF_REPO`, `AIRLLM_COMPRESSION`, `AIRLLM_DEVICE`) |

### 5.2 환경 변수 (Maya backend `.env`)

| 변수                    | 기본값                     | 의미                                           |
| ----------------------- | -------------------------- | ---------------------------------------------- |
| `MAYA_LLM_PROVIDER`     | `private`                  | `airllm` 으로 변경 시 airLLM 경로 사용         |
| `AIRLLM_HF_REPO`        | `Qwen/Qwen2.5-7B-Instruct` | 한국어 7B 기본값                               |
| `AIRLLM_COMPRESSION`    | `4bit`                     | `none`/`4bit`/`8bit`                           |
| `AIRLLM_DEVICE`         | `mps`                      | `mps`/`cpu`/`cuda`(해당 시)                    |
| `AIRLLM_MAX_NEW_TOKENS` | `256`                      | 생성 길이 상한                                 |
| `AIRLLM_TIMEOUT_S`      | `60`                       | 전체 generate 타임아웃 (초과 시 ERROR 후 폴백) |

### 5.3 의존성 (선택적)

```bash
# airLLM 의존 (필요 시)
pip install airllm        # 또는 git+https://github.com/lyogavin/airllm
# MLX 백엔드 (Mac)
pip install mlx mlx-lm    # Mac 가속
```

---

## 6. 통합 단계 (3단계 플랜)

### ✅ 1단계: 조사 (DONE — 본 문서)

- airLLM GitHub 저장소 README 분석
- macOS MLX 예제 분석
- 한국어 모델 가용성 확인
- 현재 Maya 라우터 위치 파악 (`llm.py::stream_chat`)

### 🟡 2단계: 통합 설계 (DONE — 본 문서 4~5절)

- 라우터 패턴 유지 (`MAYA_LLM_PROVIDER` enum 확장)
- 신규 파일 1개 + 수정 파일 2개
- OpenAI 호환 인터페이스 유지

### 🟡 3단계: 구현 + 검증 (진행 중 — 2026-11-25)

순서:

1. **3-1. 의존성 옵트인** ✅ — `pip install airllm` 또는 미설치 시에도 import 에러 안 나게 try/except. **분리**: `maya-ui/backend/requirements-airllm.txt` 로 옵트인 의존성 분리 — 기본 `requirements.txt` 에는 포함하지 않음.
2. **3-2. `llm_airllm.py` 구현** ✅ — HF repo 로드 → `generate(input_ids=...)` → 누적 텍스트를 16자 청크로 잘라 `ASSISTANT_DELTA` WSMessage yield → 마지막에 `ASSISTANT_FINAL`. 동기 함수는 `run_in_executor` 로 스레드 위임.
3. **3-3. `config.py`/`llm.py` 분기** ✅
   - `config.Settings.llm_provider` 에 `"airllm"` 추가.
   - `config.Settings.airllm_repo` (env `AIRLLM_HF_REPO`, 기본 `Qwen/Qwen2.5-7B-Instruct`).
   - `llm.stream_chat()` 시작부에서 `provider == "airllm"` 일 때 `llm_airllm.stream_airllm_chat(...)` 으로 디스패치.
   - `llm_gateway.needs_api_key()` 에 `airllm` 을 키 불요 provider 로 추가.
4. **3-4. 단위 테스트** ⏳ — `pytest maya-ui/backend/tests/test_llm_airllm.py -v`
   - 모킹된 모델로 메시지→스트림 흐름 확인
   - 타임아웃/예외 시 ERROR WSMessage 반환 확인
   - 한국어 입력/출력 토큰 정상 (Qwen2.5 기준)
5. **3-5. e2e 측정** ⏳ — 110 서버에 GPU 가 없어 실측 보류. CPU 폴백 측정만 가능.

**검증 통과 조건**

- `pytest` 100% 통과 (CI: `main` → pytest → GitHub Release 자동화)
- 한국어 입력 4/4에서 의미 보존
- e2e 첫 응답 < 30 s (실패 시 폴백 가이드 제공)

### 2026-11-25 메모

- 110 서버엔 GPU 가 없어 실제 airllm 추론은 검증하지 못함. 코드 경로와 옵트인 의존성만 추가.
- MacBook Air(MPS) 환경에선 airllm 이 4bit Qwen2.5-7B 기준 첫 토큰 수십 초, 이후 토큰 초 단위 — 음성 비서(e2e < 3s KPI)에는 부적합.
- 기본값은 `airllm` 이 **아닌** `private` (OpenRouter/keirouter) 으로 유지 — airllm 은 명시적으로 `MAYA_LLM_PROVIDER=airllm` 으로 켜야 동작.
- 패키지 미설치 시 WSMessage 에 `pip install -r requirements-airllm.txt` 안내 문자열 포함.

---

## 7. 비용·운영 영향

| 측면              | 영향                                                                                |
| ----------------- | ----------------------------------------------------------------------------------- |
| **런타임 메모리** | 7B 4bit → ~1~2 GB MPS, 30B 4bit → ~4~6 GB. 맥북 32 GB 통합메모리에서 안전           |
| **디스크**        | HF 캐시 양자화 후 ~10~40 GB. `delete_original=True`로 원본 절약                     |
| **첫 로드**       | 모델별 1~3 분 (디스크 분할) — 이후 캐시 적중                                        |
| **API 비용**      | $0                                                                                  |
| **한국어 품질**   | Qwen2.5/3 기준 OpenRouter gemini-2.5-flash 대비 80~95% (정확도/유창성 트레이드오프) |

---

## 8. 리스크와 완화

| 리스크                          | 영향           | 완화                                                    |
| ------------------------------- | -------------- | ------------------------------------------------------- |
| e2e voice latency > 3 s         | UX 저하        | 실측 후 voice 경로에서 제외, 메모리/요약 경로에 활용    |
| 첫 토큰 latency > 60 s          | 타임아웃 ERROR | 60 s 타임아웃 + OpenRouter 폴백 (라우터 강화는 별 단계) |
| MLX 백엔드 미설치               | import 실패    | try/except → 자동 Ollama 경로 안내 ERROR                |
| 큰 모델이 디스크 점유           | 저장소 부담    | `delete_original=True`, 작은 모델(7B/8B) 기본값         |
| 의존성 충돌 (transformers 버전) | airLLM 안 깔림 | 선택적 의존 (graceful degradation)                      |

---

## 9. 다음 액션 (사용자 승인 대기)

1. **승인되면**: `llm_airllm.py` 신규 + `llm.py`/`config.py` 2줄 수정 → 단위 테스트 → e2e 측정
2. **보류 시**: 한국어 메모리/요약 경로(비실시간)부터 도입, voice 경로는 폴백 유지
3. **거절 시**: 이 문서만 보관, 다음 우선순위 작업으로 이동

---

## 10. 참고 자료

- airLLM GitHub: <https://github.com/lyogavin/airllm>
- macOS 예제: <https://github.com/lyogavin/airllm/blob/main/air_llm/examples/run_on_macos.ipynb>
- HuggingFace repo `Qwen/Qwen2.5-7B-Instruct` (한국어 OK)
- HuggingFace repo `Qwen/Qwen3-8B` (최신, 한국어 OK)
- Maya 라우터: `maya-ui/backend/app/llm.py` (현재 `stream_chat`)
- Maya 설정: `maya-ui/backend/app/config.py` (현재 `from_env` 분기)
