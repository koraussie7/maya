# privseai.com/maya 배포 가이드

> 대상: `privseai.com/maya` 경로에 SvelteKit SPA + Python WebSocket 백엔드 배포

## 1. 구조

```
privseai.com/
├── (루트)               ← 기존 정적 사이트 (그대로 유지)
└── maya/                ← 우리가 새로 추가
    ├── index.html        ← SvelteKit adapter-static 산출물 진입점 (SPA)
    ├── _app/             ← SvelteKit JS/CSS 번들
    ├── assets/           ← 빌드된 정적 자산
    └── api/healthz       ← (선택) 서버리스 헬스체크
```

백엔드(`/ws`, `/session/*`, `/audio/*`)는 별도 도메인(`ws.privseai.com`)에 두고,
프론트는 `lib/config.js`로 자동 분기합니다.

## 2. SvelteKit 빌드 (adapter-static)

`/Users/brianyeon/CodeGPT/maya-2/maya-ui/app/svelte.config.js`에 `adapter-static`이 이미 설정되어 있고, `base`가 `maya`인 경우 빌드 출력의 모든 경로는 `/maya/...`가 됩니다. SPA fallback(`200.html`)을 함께 출력하기 위해:

```bash
cd /Users/brianyeon/CodeGPT/maya-2/maya-ui/app
npm install
npm run build
# 출력: app/build/  →  이 디렉토리의 내용을 privseai.com/maya/ 에 업로드
```

`200.html`은 SvelteKit이 라우터에 의해 클라이언트에서 마운트되도록 SPA fallback을 제공합니다.
서버 없이 정적 호스팅만 가능합니다(API는 별도 도메인).

## 3. 환경 변수 주입

빌드 시 백엔드 URL을 하드코딩하지 않고, 런타임에 결정합니다.

### 방법 A. nginx로 주입 (권장)

nginx가 `/maya/` 하위에 SPA 정적 파일을 서빙하면서, HTML 응답의 첫 줄에 다음을 삽입:

```nginx
location = /maya/ {
    sub_filter '</head>' '<script>window.__MAYA_WS_URL__="wss://ws.privseai.com/ws";window.__MAYA_HTTP_BASE__="https://ws.privseai.com";</script></head>';
    sub_filter_types text/html;
    try_files /maya/index.html =404;
}
```

### 방법 B. 환경별 `_config.js` 분리

```bash
# 빌드 산출물 privseai.com/maya/_config.js
window.__MAYA_WS_URL__ = "wss://ws.privseai.com/ws";
window.__MAYA_HTTP_BASE__ = "https://ws.privseai.com";
```

`app.html`에 `<script src="/maya/_config.js"></script>` 추가.

## 4. 백엔드 배포 (Python)

### 권장

- 도메인: `ws.privseai.com`
- HTTPS 종료: Caddy / nginx (Let's Encrypt)
- 프로세스: systemd 또는 tmux
- 포트: `127.0.0.1:8765` (내부) → nginx `proxy_pass` → `wss://`

### systemd 단위 예시

```ini
[Unit]
Description=Maya voice backend
After=network.target

[Service]
Type=simple
User=maya
WorkingDirectory=/srv/maya/backend
Environment=MAYA_WS_PORT=8765
# ── 대화 경로 LLM ─────────────────────────────────────────────
# 기본(주석 처리 상태) = **같은 서버의 smart-router(keirouter)**.
#   · OPENAI_BASE  = http://127.0.0.1:20128   (외부에는 nginx 의 /llm/ 로만 노출)
#   · OPENAI_MODEL = auto/best-fast           (무료 제공자 라우팅 별칭)
#   · 키는 110 서버 ~/.keirouter/keirouter.db 에서 발급한 뒤 환경변수로만 넣는다.
#     (레포/문서/로그에 키를 옮기지 말 것 — 이전 OpenRouter 키 유출 재발 방지)
Environment=MAYA_LLM_PROVIDER=private
Environment=OPENAI_BASE=https://privseai.com/llm
Environment=OPENAI_MODEL=auto/best-fast
Environment=OPENAI_API_KEY=<keirouter 에서 발급한 키>
Environment=MAYA_LLM_TIMEOUT_S=20
# 대안 1) OpenRouter:            MAYA_LLM_PROVIDER=cloud  OPENAI_BASE=https://openrouter.ai/api/v1
# 대안 2) 로컬(대화 부적합):      MAYA_LLM_PROVIDER=local  OPENAI_BASE=http://127.0.0.1:11434/v1  OPENAI_MODEL=gemma3:4b
# ⚠️ 운영 배포에서는 MAYA_LLM_PROVIDER=private 를 쓰지 말 것 (아래 3.2/3.3)
Environment=HF_TOKEN=hf_...
Environment=VOXCPM_API_URL=https://openbmb-voxcpm-tts.hf.space
ExecStart=/srv/maya/.venv/bin/python -m app.server
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### nginx 설정 예시

```nginx
# WebSocket
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    server_name ws.privseai.com;
    listen 443 ssl http2;
    ssl_certificate     /etc/letsencrypt/live/ws.privseai.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ws.privseai.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        client_max_body_size 64m;
    }
}
```

## 5. CORS

`privseai.com ↔ ws.privseai.com`은 다른 origin이므로 백엔드에서 CORS를 허용해야 합니다.
FastAPI의 경우 `CORSMiddleware`로 `https://privseai.com`을 `allow_origins`에 추가.

## 6. 도메인 / DNS

| 호스트                | 타입             | 값               |
| --------------------- | ---------------- | ---------------- |
| `privseai.com`        | A/CNAME          | 기존             |
| `ws.privseai.com`     | A                | 백엔드 서버 IP   |
| `privseai.com/maya/*` | (nginx location) | SvelteKit static |

## 7. 점검

1. `https://privseai.com/maya/` → SPA 부팅, VoiceOrb 렌더 확인
2. 마이크 권한 요청 → 녹음 시작 → 마이크 레벨 표시 변화
3. 발화 → STT 텍스트 표시 → LLM 응답 텍스트/오디오 재생
4. devtools Network → `wss://ws.privseai.com/ws` 101 Switching Protocols
5. Console 에러 없음

## 8. 비용 / 운영 주의

- OpenRouter API 키는 **서버 환경변수**에만 보관, Git 커밋 금지
- HF Space 호출은 큐 지연(0~20s) → `tts_local.py`의 재시도 로직이 이미 적용됨
- VoxCPM API worker가 HF Space 큐에 갇히지 않도록 `timeout=60s` + 폴링 사용
- 1일 TTS 호출량 모니터링 (HF 무료 등급 한도)
- LLM 토큰 비용은 OpenRouter 대시보드에서 알림 설정

## 9. 업로드 명령 (예: SvelteKit SPA)

```bash
cd /Users/brianyeon/CodeGPT/maya-2/maya-ui/app
npm run build
rsync -avz --delete build/ deploy@privseai.com:/srv/www/privseai.com/maya/
ssh deploy@privseai.com "sudo systemctl reload nginx"
```
