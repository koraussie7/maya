<h1 align="center">Maya</h1>
<p align="center">
  <strong>한국어 음성 대화 스택 — VAD → STT → 결정 라우터 → LLM → TTS</strong><br />
  실시간 발화 감지와 문장 단위 스트리밍 합성으로 "말이 끝나면 바로 소리가 나는" 경로를 만든다.
</p>

## 이 저장소가 무엇인가

Maya 는 **음성 대화 본체**입니다. 규칙 기반 결정 라우터(`turn_decision`/`router`)가 정본이고,
LLM 은 자유 대화 구간에서만 붙습니다. TTS 는 piper/VoxCPM/Pocket 을 라우터가 골라 쓰고,
합성은 문장 단위로 병렬화해 첫 소리 지연을 줄입니다.

| 영역      | 경로                   | 내용                                                                |
| --------- | ---------------------- | ------------------------------------------------------------------- |
| 웹 UI     | `maya-ui/app/`         | SvelteKit — 대화 화면, `/decisions` 결정 로그, `/knowledge`         |
| 백엔드    | `maya-ui/backend/app/` | FastAPI + WebSocket — `session.py` 오케스트레이션                   |
| 대화 자산 | `maya/`                | 페르소나(`personas/`), 라우트(`dialog/`), TTS 보이스(`models/tts/`) |
| 실험/벤치 | `integration/`         | FireRedVAD, Lyra/Lyra self-clone, VAD·TTS·STT 벤치                  |
| 문서      | `docs/`, `maya/docs/`  | 로드맵, 인수인계, 통합 계획                                         |

## Git LFS 필수

모델 가중치(`*.onnx` `*.gguf` `*.safetensors` `*.pth` `*.pt` `*.ckpt`)는 [Git LFS](https://git-lfs.com)로 저장합니다.
**clone 전에 머신당 1회** 설치하세요.

```bash
# macOS
brew install git-lfs
# Linux (Debian/Ubuntu)
sudo apt install git-lfs

git lfs install
```

설치 없이 clone 하면 가중치가 133바이트 포인터로 남습니다(예: `maya/models/tts/piper/ko_KR-kss-medium.onnx` 가 63MB 대신 포인터).
이미 clone 했다면:

```bash
git lfs install
git lfs pull
```

확인:

```bash
ls -la maya/models/tts/piper/ko_KR-kss-medium.onnx   # ~63MB 여야 정상
shasum -a 256 maya/models/tts/piper/ko_KR-kss-medium.onnx
# 4c734a734ae97d708ef94f147ad920d92b625a8a195b2c045ab3064c29ee0281
```

## 백엔드 실행

```bash
./scripts/run_maya_backend.sh              # 포그라운드
nohup ./scripts/run_maya_backend.sh &      # 백그라운드
```

기본 포트는 `MAYA_WS_PORT`(기본 `8788`). 앱 엔트리는 `maya-ui/backend/app/server.py` 의 `app`.
직접 띄우려면:

```bash
cd maya-ui/backend && PYTHONPATH=. ../../.venv-voicemem/bin/python -m uvicorn app.server:app --port 8788
```

프론트엔드:

```bash
cd maya-ui/app
npm ci
npm run dev      # 개발 서버 (기본 5173)
npm run build    # 프로덕션 빌드
```

### UI ↔ 백엔드 연결

UI 는 `src/lib/config.js` 가 주소를 결정합니다(`resolveWsUrl()` / `resolveHttpBase()`).

| 우선순위 | 값 | 용도 |
| --- | --- | --- |
| 1 | `window.__MAYA_WS_URL__` / `window.__MAYA_HTTP_BASE__` | 호스팅 HTML 인라인 주입 |
| 2 | `VITE_MAYA_WS_URL` / `VITE_MAYA_HTTP_BASE` | 빌드/개발 시 주입 |
| 3 | **현재 오리진** (`wss://host<base>/ws`) | 서버가 UI 를 직접 서빙할 때 |
| 4 | `ws://localhost:8765/ws` | 로컬 폴백 |

개발 서버(5173)와 백엔드(8788)를 따로 띄울 때는:

```bash
cd maya-ui/app
VITE_MAYA_WS_URL=ws://localhost:8788/ws \
VITE_MAYA_HTTP_BASE=http://localhost:8788 \
npm run dev
```

### 화면

| 경로 | 내용 |
| --- | --- |
| `/` | 대화 화면 — 마이크 입력, 실시간 전사, 오디오 재생 |
| `/decisions` | 결정 로그 — 룰/Laya/abstain 라우팅 이벤트 |
| `/knowledge` | 지식 — 프로필 목록, 문서 업로드, 검색, 엔티티 그래프 |

## 테스트

```bash
PYTHONPATH=maya-ui/backend .venv-voicemem/bin/python -m pytest \
  maya-ui/backend/tests/ -q
```

> 주의: `test_session_decision.py` 의 abstain 경로는 LLM 자유 대화 구간으로 빠지는데,
> LLM 엔드포인트가 막혀 있으면 TTS 워커 대기(`session.py:560`)에서 멈출 수 있습니다.
> LLM 백엔드를 살리거나 해당 케이스를 mock 으로 고정한 뒤 실행하세요.

## TTS 경로

```
voxcpm-api (HF Space, 클론 음성)  →  piper (ko_KR-kss-medium, 로컬)  →  say (macOS)
```

- 기본 프로바이더: `MAYA_TTS_PROVIDER=piper`
- piper 모델 위치: `models/tts/piper/ko_KR-kss-medium.onnx` (`PIPER_MODEL_PATH` 로 변경 가능)
- Pocket TTS 사용 시: `MAYA_TTS_PROVIDER=pocket`

## 결정 로그 (`/decisions`)

백엔드는 라우팅 결정을 `decision.log` 메시지로 브로드캐스트합니다.

```
{ type: 'decision.log', text, route_id, source, score, threshold,
  abstained, rule_hit, laya_hit, all_scores, ts }
```

- `source`: `rule` | `laya` | `abstain` | `disabled`
- `all_scores`: `[{ route_id, score, source }]` (배열)
- `ts`: epoch 초 (float)

## 주요 환경변수

| 변수                  | 기본    | 의미                            |
| --------------------- | ------- | ------------------------------- |
| `MAYA_LAYA`           | `0`     | Laya reranker 사용 여부         |
| `MAYA_LAYA_MOCK`      | `0`     | `1` 이면 실모델 없이 mock       |
| `MAYA_LAYA_THRESHOLD` | `0.45`  | abstain 임계값                  |
| `MAYA_TTS_PROVIDER`   | `piper` | `piper` \| `voxcpm` \| `pocket` |
| `MAYA_TTS_WORKERS`    | `2`     | 병렬 TTS 워커 수                |
| `MAYA_CONTEXT_TURNS`  | `8`     | LLM 에 보내는 최근 턴 수        |

## Digital Twin 인터페이스

`docs/integration/digital-twin/` 에 통합 계획이 있습니다. Maya 는 GraphRAG·지식 그래프·Pocket TTS 를
**자체 백엔드로 포팅**해 사용하며(`maya-ui/backend/app/rag/`, `tts_pocket.py`, `knowledge_router.py`,
`maya-ui/app/src/routes/knowledge/`), 참고 구현은 별도 저장소
[ShlokShah01/digital-twin](https://github.com/ShlokShah01/digital-twin) 에 있습니다.
해당 저장소의 소스는 이 저장소에 포함하지 않습니다.

## License

이 저장소의 코드는 **MIT License** 입니다. 전문은 [LICENSE](LICENSE) 를 참고하세요.

`docs/integration/digital-twin/` 의 계획 문서는 위 참고 저장소의 구조를 설명하며,
해당 프로젝트의 코드나 자산은 포함하지 않습니다. 참고 저장소를 직접 사용하려면 원저작자의
라이선스를 별도로 확인하세요(현재 해당 저장소에 라이선스 파일이 없습니다).

## 참고한 프로젝트

이 저장소에는 아래 프로젝트의 **소스를 포함하지 않습니다.** 설계 검토 단계에서 참고만 했습니다.

| 프로젝트 | 라이선스 | 참고 범위 |
| --- | --- | --- |
| [TEN-framework/ten-framework](https://github.com/TEN-framework/ten-framework) | Apache-2.0 **+ 추가 제한**(Agora 경쟁 배포 금지, 파생물 동일 라이선스 적용) | UI 정보 구조(대화/설정 배치, 네트워크 표시, 마이크 토글)만 참고. **코드 복사 없음** — TEN 은 Next.js+React+Agora RTC 기반이고 Maya 는 SvelteKit+자체 WebSocket 이라 직접 사용하지 않았습니다 |
| [ShlokShah01/digital-twin](https://github.com/ShlokShah01/digital-twin) | 라이선스 파일 없음(기본 저작권) | GraphRAG·지식 그래프·Pocket TTS 의 **동작 방식**만 참조해 Maya 백엔드로 재구현. 소스 미포함 |

TEN 의 라이선스는 단순 Apache-2.0 이 아니므로, TEN 을 실제로 배포에 사용하려면
[LICENSE 원문](https://raw.githubusercontent.com/TEN-framework/ten-framework/main/LICENSE)의
추가 조항을 먼저 검토하세요. Maya 는 MIT 이며 위 제한의 적용을 받지 않습니다.
