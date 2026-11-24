# privseai.com Maya UI 점검 (2026-11-24)

> **결론**: `privseai.com/maya` 도 `privseai.com/privseai/` 도 **502**. 원인은 Cloudflare/nginx 가 아니라
> **업스트림(7684 → 맥북)이 응답하지 않는 것**이며, 코드상 원인도 찾았다(맥북이 `127.0.0.1` 에 바인딩).
> 게다가 UI 의 WebSocket 주소가 **`maya.privseai.com`** 을 가리키는데 그 호스트는 nginx·DNS 어디에도 없다.

---

## 1. 실제 점검 결과 (하드코딩된 문서와 다른 부분)

| URL                                           | 상태                                                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `https://privseai.com/maya/`                  | 지시하신 경로. **라우트가 없다** → `location /` 의 `try_files $uri $uri/ /index.html` 로 SPA index 반환(200 이지만 Maya UI 아님) |
| `https://privseai.com/privseai/`              | **502 Bad Gateway** — 7684 블록의 업스트림 `http://127.0.0.1:18000` 에 **리스너가 없다** |
| `https://privseai.com/privseai/llm/v1/models` | **200** (게이트웨이 경로는 살아 있음)                                                                |
| `https://privseai.com/aivoice/`               | 502                                                                                                  |
| 서버 리스너            | `:3000` LISTEN, `:5173` LISTEN, **`:18000` 없음**(이전 라운드에서 확인된 프로세스가 지금 없음) |

브라우저(헤드리스)로 열어 본 결과:

```
http       502
title      "privseai.com | 502: Bad gateway"
bodyChars  382
console    Failed to load resource: the server responded with a status of 502
```

`/var/www/privseai` 에는 `index.html`·`assets`·`build` 만 있고 **`maya/` 디렉터리가 없다** —
정적 배포가 아니라 프록시로 붙어야 하는 구조다.

## 2. 근본 원인 (코드에서 확인)

`maya-ui/static/app.js`:

```js
const BACKEND_WS =
  (location.protocol === "https:" ? "wss://" : "ws://") +
  (location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "127.0.0.1:8787"
    : "maya.privseai.com") +
  "/ws";
```

1. **공개 호스트가 `maya.privseai.com`** — nginx 설정에도, `letsencrypt/live` 에도 그 이름이 없다.
   즉 UI 를 `/privseai/`(또는 `/maya/`)에 올리면 WS 는 **존재하지 않는 호스트**로 연결을 시도해 실패한다.
2. **백엔드가 `127.0.0.1:8787`(기본)** 에만 바인딩 — 맥북에서 실행하면 7684 로 들어온 프록시가
   맥북의 loopback 에 닿지 못해 502.
3. **502 의 직접 원인**: 7684 블록의 `/privseai/` 는 `proxy_pass http://127.0.0.1:18000`(aivoice 백엔드)인데,
   **지금 18000 에 리스너가 없다**. 즉 맥북→서버 역터널이 끊긴 상태다(맥북 쪽 FastAPI 도 지금 미실행).

## 3. 필요한 결정 (제가 임의로 정하지 않은 부분)

**공개 URL 을 무엇으로 할지**에 따라 수정 범위가 달라진다.

| 옵션                                     | nginx                                                                     | 프론트                                  | 장점                                          |
| ---------------------------------------- | ------------------------------------------------------------------------- | --------------------------------------- | --------------------------------------------- |
| A. `/privseai/` 유지 (현 WS 설정과 일치) | 이미 있음                                                                 | `app.js` 그대로                         | 손이 가장 적게 감. 단 `/maya` 로는 접근 못 함 |
| B. `/maya/` 로 통일                      | `location ^~ /maya/ { proxy_pass http://127.0.0.1:7684/privseai/; }` 추가 | `app.js` 의 WS 경로 `/privseai/ws` 사용 | 지시하신 URL 로 접근 가능                     |
| C. `maya.privseai.com` 서브도메인        | 새 server 블록 + 인증서 + DNS                                             | 수정 없음                               | `app.js` 그대로. 인증서/DNS 작업 필요         |
| **D. (권장) 서버에서 직접 서빙** | 정적 빌드(`dist`) → `/var/www/privseai/maya/` + `location ^~ /maya/` | `app.js` 오리진 상대 WS | 맥북 의존·터널 의존 제거. 502 원인이 사라짐. 단 LLM/STT/TTS도 서버에서 돌아야 함 |

## 4. 코드 수정안 (옵션 B 기준, 아직 적용 전)

**프론트 — 호스트를 하드코딩하지 않고 같은 오리진 기준 상대경로로**

```js
// before: 원격이면 maya.privseai.com/ws (존재하지 않는 호스트)
// after : 어디에 배포해도 같은 오리진의 경로를 쓴다
const WS_PATH = import.meta?.env?.BASE_PATH
  ? `${import.meta.env.BASE_PATH}ws`
  : `${location.pathname.replace(/\/(index\.html)?$/, "").replace(/\/$/, "")}/ws`;
const BACKEND_WS = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${WS_PATH}`;
```

**백엔드 — 컨테이너/터널 뒤에서도 닿도록 listen 주소 설정화**

```python
# server.py (uvicorn 실행부)
host = os.environ.get("MAYA_WS_HOST", "127.0.0.1")   # 터널/프록시면 0.0.0.0
port = int(os.environ.get("MAYA_WS_PORT", "8765"))   # app.js 의 8787 과 맞출 것
```

**nginx (옵션 B)**

```nginx
location ^~ /maya/ {
    proxy_pass http://127.0.0.1:7684/privseai/;   # 오리진의 UI 경로로 매핑
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_buffering off;
}
```

## 5. ✅ 옵션 D 적용 결과 (2026-11-24, 완료)

**빌드 → 배포 → nginx 까지 끝냈고, 공인 URL 에서 UI 가 렌더되는 것을 스크린샷으로 확인했다.**

| 단계 | 내용 |
|------|------|
| 1. 프론트 수정 | `svelte.config.js`: `kit.paths.base='/maya'`, `prerender.entries=['/']` (기존 `['*']` 는 `/memory`·`/settings` 404 로 빌드 실패). `src/lib/config.js`: 원격일 때 `maya.privseai.com/ws` 하드코딩 → **같은 오리진 `BASE_URL/ws`**. `+page.svelte`: 하드코딩 링크 → `$app/paths.base` 기준 |
| 2. 빌드 | `npx vite build` → `build/` (adapter-static, 1.4M). 탐색/에셋 모두 `/maya/` 접두사 확인 |
| 3. 로컬 검증 | 정적 서버로 `/maya/` 렌더 확인(이때 실제로 `_app` 경로 문제 2건을 잡아냄: 별칭 디렉터리 중첩 누락, `prerender['*']` 404) |
| 4. 서버 배포 | `/var/www/privseai/maya/` (기존 디렉토리는 `maya.bak-<ts>` 로 보관) |
| 5. nginx | 7684 블록에 `location = /maya { 301 /maya/ }`, `location ^~ /maya/`(alias + SPA `try_files`), `location ^~ /maya/_app/immutable/`(30d 캐시), `location = /maya/ws`(→ `127.0.0.1:18000/ws`, Upgrade 헤더) |
| 6. 검증 | 아래 표 |

### 검증 결과

| 확인 | 결과 |
|------|------|
| `GET https://privseai.com/maya` | **301** → `/maya/` |
| `GET https://privseai.com/maya/` | **200**, index.html |
| `_app/immutable/entry/start*.js` | **200** (JS/CCS 정상) |
| 브라우저 렌더 | **✅ Maya UI 정상 표시** — 상단 “Maya” 상태점, 오브(orb), “대화/기억/설정” 버튼, 빈 대화 왼쪽 패널. 502 페이지 사라짐 |
| `GET https://privseai.com/maya/memory` | 200 (SPA 폴백) |
| `WS https://privseai.com/maya/ws` | **502** — nginx 는 정상, **백엔드(18000) 리스너 없음** |

⚠️ **UI 는 띄지만 아직 대화는 안 된다.** WS 대상인 `127.0.0.1:18000` 에 리스너가 없다(맥북 FastAPI 미실행 + 역터널 없음).
UI 는 이 상태를 화면에 “연결 안 됨”으로 표시한다(옵션이전처럼 빈 화면으로 멈추지 않는 것이 이번에 고친 부분).

### 남은 일: 백엔드 위치 결정

서버의 `maya-backend.service` 유닛은 있으나 지금 실행되지 않아이며, 이 저장소 기준 실행 명령은
`python -m app.server` — **`MAYA_WS_PORT` 기본값 8765** 이다. 운영(18000)에 맞추려면:

```ini
Environment=MAYA_WS_PORT=18000           # nginx location = /maya/ws 의 proxy_pass 와 일치
Environment=MAYA_LLM_PROVIDER=private
Environment=OPENAI_BASE=https://privseai.com/privseai/llm/v1
Environment=OPENAI_MODEL=auto/best-fast
Environment=MAYA_LLM_TIMEOUT_S=20
Environment=HF_TOKEN=…                   # STT
Environment=VOXCPM_API_URL=…             # TTS
ExecStart=/srv/maya/.venv/bin/python -m app.server
```

로컬 개발은 `MAYA_WS_PORT=18000 python -m app.server` + 터널(`ssh -R 18000:127.0.0.1:18000 110`)로
같은 URL(`privseai.com/maya/`)에서 바로 검증할 수 있다.

### 롤백
```bash
ssh 110 'ls /var/www/privseai/maya.bak-* /etc/nginx/sites-enabled/privseai.com.conf.bak-*'
ssh 110 'rm -rf /var/www/privseai/maya && mv /var/www/privseai/maya.bak-<ts> /var/www/privseai/maya'
ssh 110 'cp -a /etc/nginx/sites-enabled/privseai.com.conf.bak-<ts> /etc/nginx/sites-enabled/privseai.com.conf && nginx -t && systemctl reload nginx'
```

## 6. 이번 점검에서 얻은 교훈

- **문서를 먼저 믿지 않고 실제 응답을 봤다**: 문서에는 `/privseai/` 회귀 없음이라고 적혀 있었지만,
  확인 시점엔 502 였다(업스트림은 언제든 죽을 수 있다 → 배포 문서에는 항상 **확인 명령**을 같이 남긴다).
- SPA `try_files` 는 **없는 경로도 200** 으로 돌려준다. `/maya/` 가 200 이라고 라우트가 있는 게 아니다 —
  실제 렌더 여부는 브라우저 스크린샷/WS 핸드셰이크로 판정해야 한다.
