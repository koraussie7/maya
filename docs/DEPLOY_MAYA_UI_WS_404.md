# Maya UI 배포 — WebSocket 404 인수인계 (2026-11-27)

> **읽는 사람**: 이어받는 에이전트/엔지니어
> **작성 근거**: 서버 `110`(185.55.240.110)에서의 실측 로그·응답 헤더. 추정은 "추정"으로 표시.
> **현재 상태**: 서버 내부 정상, **Cloudflare 경유 WebSocket 만 404**. UI 대화는 REST 로 동작.

---

## 0. 한 줄 요약

`maya.privseai.com` 은 페이지·API 가 **200**, `/maya-app/ws` 만 **404**.
**서버(nginx·uvicorn·TTS)는 완전히 정상** — `127.0.0.1:8433/maya-app/ws` 는 101.
문제는 **Charon(Caddy) 이 443 을 점유하고 maya 도메인을 제대로 넘기지 않는 것**.

### 0.1 2026-11-27 최종 상태 (실측)

| 항목 | 상태 |
| --- | --- |
| UI 페이지 / `/decisions` / `/knowledge` | 200 ✅ |
| `/api/health`, `/api/ask` (한국어 대화) | 200 ✅ |
| `/api/speech` (piper TTS) | 200, 60,930 bytes ✅ |
| `/maya-app/ws` 공개 경유 | **404 ❌** |
| `/maya-app/ws` 서버 직결(8433) | **101 ✅** |
| DNS | `maya.privseai.com` → 185.55.240.110 **(DNS only)** |
| TLS 인증서 | Let's Encrypt 발급 완료 (`/etc/letsencrypt/live/maya.privseai.com/`) 만료 2027-02-25 |

### 0.2 확정된 근본 원인

```
IPv4 443 : docker-proxy(Charon 컨테이너) 가 0.0.0.0:443 점유
IPv6 443 : nginx [::]:443
```

- 클라이언트(IPv4) → **Charon** 이 받음
- Charon 은 41개 도메인을 서비스 중이라 **내리면 전부 중단된다**
- Charon 이 maya 트래픽을 `172.23.0.1:8433` 으로 보내도록 설정했고 **컨테이너 안에서는 200/101 확인**
- 그러나 nginx access 로그에 **Charon 발 요청이 도달하지 않는다** (rewrite 후 `x-debug-uri: /ws` 는 찍힘)

> ⚠️ **nginx 를 `listen 443` 로 바인딩하지 말 것** — Charon 과 충돌해 `bind() failed`,
> **nginx 전체가 내려가 기존 서비스 41개 도메인이 중단된다** (이번 세션 실제 발생 → 즉시 복구).
> `listen 185.55.240.110:443` 도 같은 이유로 실패한다(0.0.0.0:443 이 이미 잡혀 있음).

---

## 1. 확정된 사실 (실측)

| #   | 항목                   | 결과                                                          | 근거                                                                  |
| --- | ---------------------- | ------------------------------------------------------------- | --------------------------------------------------------------------- |
| 1   | DNS                    | `maya.privseai.com` A → `185.55.240.110` (Cloudflare Proxied) | `dig +short`                                                          |
| 2   | UI 페이지              | **200** — 정상 렌더, SvelteKit base `/maya-app`               | 브라우저 스크린샷                                                     |
| 3   | API `/api/health`      | **200**                                                       | `curl`                                                                |
| 4   | `/api/ask` 한국어 대화 | **정상** (OpenRouter `openai/gpt-4o-mini`)                    | `curl` 응답                                                           |
| 5   | 서버 직결 WS           | **101 Switching Protocols**                                   | `curl -H "Host: maya.privseai.com" http://127.0.0.1:8433/maya-app/ws` |
| 6   | Cloudflare 경유 WS     | **404**                                                       | `curl https://maya.privseai.com/maya-app/ws`                          |
| 7   | 404 응답 헤더          | `via: 1.1 Caddy`, 본문 22B                                    | `curl -i`                                                             |
| 8   | 404 본문 형식          | `{"detail":"Not Found"}`                                      | FastAPI 형식이나 실제로는 nginx 404                                   |

### 1.1 결정적 증거 — 같은 경로, 다른 응답

`/var/log/nginx/access.log` (2026-11-27 19:55~20:00):

```
127.0.0.1    … "GET /maya-app/ws HTTP/1.1" 101 607  "-"  "curl/7.81.0"   ← Host 지정
172.23.0.2   … "GET /maya-app/ws HTTP/1.1" 404  22  "-"  "curl/8.7.1"    ← Charon
172.23.0.2   … "GET /maya-app/ws HTTP/1.1" 404  22  "-"  "curl/7.81.0"   ← Charon
127.0.0.1    … "GET /maya-app/ws HTTP/1.1" 101 607  "-"  "curl/7.81.0"   ← Host 지정
```

- `172.23.0.2` = **Charon 컨테이너 IP**
- 같은 nginx, 같은 포트(8433), 같은 경로인데 **client 가 Host 를 안 보내면 404**

### 1.2 컨테이너 → 호스트 접근은 정상

```bash
docker exec charon sh -c 'wget -S -O - --header="Host: maya.privseai.com" \
  http://host.docker.internal:8433/api/health'
# → 200 OK  (Host 를 넣으면 정상)
```

즉 **네트워크 경로는 살아 있고, Host 헤더만 문제**다.

---

## 2. 인프라 구성 (실측)

```
클라이언트
   │  https://maya.privseai.com   (Cloudflare Proxied)
   ▼
Cloudflare  ──  TLS 종료
   ▼
Charon (Caddy, Docker 컨테이너 charon, IP 172.23.0.2)
   │  reverse_proxy → host.docker.internal:8433   (평문 http)
   ▼
호스트 nginx  :8433  (평문 전용 server 블록)
   ├─ location ^~ /maya-app/ws   → 127.0.0.1:8799/ws   (FastAPI WS)
   ├─ location ^~ /maya-app/api/ → 127.0.0.1:8799/api/
   └─ location ^~ /maya-app/     → alias /var/www/privseai/maya-app/
   ▼
maya-voice.service (uvicorn, 127.0.0.1:8799)
```

### 2.1 Charon 라우트 (DB 원본)

Charon 설정은 **파일이 아니라 SQLite** 에서 생성된다.

- DB: `/opt/charon/charon-data/charon.db` → 테이블 `proxy_hosts`, **id=34**
- 생성물: `/opt/charon/charon-data/caddy/config-*.json` (재시작 시 재생성)
- 반영: `docker restart charon` (admin API `:2019` 는 **비활성**)

id=34 현재 값:

```
domain_names     = maya.privseai.com
forward_scheme   = http
forward_host     = host.docker.internal
forward_port     = 8433
websocket_support= 1
```

생성된 Caddy 라우트(`charon_server` routes[1], `terminal: true`):

```json
{
  "match": [{ "host": ["maya.privseai.com"] }],
  "handle": [
    {
      "handler": "reverse_proxy",
      "upstreams": [{ "dial": "host.docker.internal:8433" }]
    }
  ],
  "terminal": true
}
```

---

## 3. 이미 시도했고 **효과 없었던** 것

인수인계 시 **반복하지 말 것**.

| 시도                                           | 결과                                  | 비고                                                       |
| ---------------------------------------------- | ------------------------------------- | ---------------------------------------------------------- |
| `server_name maya.privseai.com _;` → `_` 제거  | 변화 없음                             | 와일드카드는 원인이 아니었음 **(이전 진단이 틀렸던 지점)** |
| `listen 8433;` → `listen 8433 default_server;` | 변화 없음                             |                                                            |
| `location ^~ /maya-app/ws` → `= /maya-app/ws`  | 변화 없음                             | 설정 순서·매칭은 정상                                      |
| **`systemctl reload nginx` 만 함**             | **변화 없음**                         | ⚠️ **`restart` 해야 반영됐다** (reload 로는 왜인지 반영 안 됨) |
| `server_name _`(catch-all) 적용                | 서버 직결 101 로 통과                 | Host 무관 수신은 해결됨                                    |
| Caddy upstream `:8443`(TLS)                    | 400                                   | nginx 8443 은 TLS 전용, Charon 은 평문만                   |
| Caddy upstream `:80`                           | 431                                   | 호스트 80 은 **다른 nginx** 가 점유 (`bind() failed`)      |
| `listen 80` maya server 블록 추가              | **nginx 전체 다운** → 즉시 제거·복구  | 절대 재시도 금지                                           |
| `advanced_config` 에 `insecure_skip_verify`    | 효과 없음, 오히려 `:80` 으로 되돌아감 | 제거함(현재 NULL)                                          |
| `log_format` 을 conf **최상단**에 삽입          | **nginx 문법 깨짐** → 즉시 복구       | `log_format` 은 `http` 블록 안에만 허용                    |

> ⚠️ nginx 백업: `/etc/nginx/sites-enabled/privseai.com.conf.bak-*`,
> `/root/nginx-privseai.bak-*.conf`, `/root/charon.db.bak-*`

---

## 4. 남은 진단 (다음 에이전트가 할 일)

### 4.0 이미 적용된 변경 (서버 110, 2026-11-27 20:2x)

```nginx
# /etc/nginx/sites-enabled/privseai.com.conf, "maya-8433-block"
server {
    listen 8433 default_server;
    server_name _;            # Host 무관 catch-all (Charon 이 Host 를 안 보냄)

    location = /maya-app/ws {  # 정확 매칭
        proxy_pass http://127.0.0.1:8799/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    location /maya-app/api/ { proxy_pass http://127.0.0.1:8799/api/; ... }
    location /maya-app/     { alias /var/www/privseai/maya-app/; ... }
    location /              { root  /var/www/privseai/maya-app; ... }
}
```

**적용 후 상태**

| 경로 | 결과 |
| --- | --- |
| `http://127.0.0.1:8433/maya-app/ws` (Host 없이) | **101** ✅ |
| `http://127.0.0.1:8433/maya-app/ws` (Host 지정) | **101** ✅ |
| `https://maya.privseai.com/maya-app/` | **200** ✅ |
| `https://maya.privseai.com/maya-app/ws` | **404** ❌ |

> ⚠️ **nginx 변경은 `systemctl reload` 로 반영되지 않았다. `systemctl restart nginx` 를 쓸 것.**
> (reload 후에도 404 였는데 restart 후 101 로 바뀜 — 이번 세션 실측)

### 4.1 다음 단계 — Charon 이 실제로 어디로 보내는지 확정

`/var/log/nginx/access.log` 실측 (client IP 로 주체 구분):

```
127.0.0.1   "GET /maya-app/ws"  101    ← Host 지정해 직접 호출
172.23.0.2  "GET /maya-app/ws"  404    ← Charon(컨테이너) 경유
14.231.x.x  "GET /maya-app/ws"  101    ← Cloudflare 엣지가 직접 받은 경우
```

→ **Charon 이 nginx 로 넘길 때 404** 가 나지만, 같은 컨테이너에서 `nc` 로 직접
`GET /maya-app/ws` 를 보내면 **101** 이 온다. 즉 문제는 **Charon 의 프록시 계층**.

다음 확인 순서:

```bash
# 1) Charon 내부에서 nginx 로 직접 요청 (이미 101 확인됨)
docker exec charon sh -c 'printf "GET /maya-app/ws HTTP/1.1\r\nHost: maya.privseai.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n" | nc -w3 host.docker.internal 8433 | head -1'

# 2) Charon → Cloudflare 경유 요청의 차이 확인
#    Cloudflare 가 보내는 헤더를 Caddy 가 어떻게 변환하는지
#    → Charon 이 "Nginx Proxy Manager" 호환인지 확인:
#       docker exec charon caddy list-modules 2>/dev/null | head

# 3) Cloudflare 엣지 차이인지 확인 — PC 명맥을 바꾼다
curl -s -o /dev/null -w "%{http_code}\n" \
  --resolve maya.privseai.com:443:185.55.240.110 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://maya.privseai.com/maya-app/ws
```

### 4.2 해결 후보 (권장 순)

**A. Charon 에 custom header 추가 (가장 깔끔)**
Charon/NPM 계열 UI 의 proxy host → **Advanced / Custom Nginx Configuration** 에 해당:

```nginx
proxy_set_header Host maya.privseai.com;
proxy_set_header X-Forwarded-Host $host;
```

> Nginx Proxy Manager 계열이면 `advanced_config` 가 아니라 **"Custom Nginx Configuration"**
> 필드에 넣어야 생성되는 `proxy_set_header` 보다 앞에 온다. 필드명은 Charon 판본 확인 필요.

**B. nginx 에서 Host 무관하게 처리 (즉시 가능, 서버만 수정)**

`8433` 블록을 host 기반에서 **경로 기반 단일 vhost** 로 바꾼다.
이미 `default_server` 이므로, `server_name` 을 지우고 `_` 기본 서버 하나로 두면
Host 가 무엇이든 같은 location 이 매칭된다.

```nginx
server {
    listen 8433 default_server;
    server_name _;                 # ← 어떤 Host 든 받는다

    location ^~ /maya-app/ws {
        proxy_pass http://127.0.0.1:8799/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host maya.privseai.com;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    # ... /maya-app/api/ , /maya-app/ 동일
}
```

> 현재 404 가 나는 이유가 "default server 에 location 이 없어서"라면 이 방법으로 바로 해결된다.
> **단, 8433 을 다른 서비스가 공유하지 않는 것을 먼저 확인할 것** (`ss -lntp | grep 8433`).

**C. Cloudflare 경유 자체를 우회** — 새 서브도메인이 아니라 **다른 노출 경로** 사용
SSH 터널(아래 6절)로는 이미 정상 동작한다.

---

## 5. 서버 배포 현황 (재현용)

| 대상      | 경로                                                             |
| --------- | ---------------------------------------------------------------- |
| UI 정적   | `/var/www/privseai/maya-app/` (296K, SvelteKit base `/maya-app`) |
| 백엔드    | `/opt/maya/ui/backend/` (uvicorn `app.server:app`)               |
| 대화 자산 | `/opt/maya/assets/{personas,dialog}`                             |
| 환경변수  | `/opt/maya/.env` (chmod 600, `.gitignore` 대상)                  |
| systemd   | `/etc/systemd/system/maya-voice.service` → `127.0.0.1:8799`      |
| 로그      | `/var/log/maya-voice.log`                                        |

```bash
systemctl status maya-voice
curl -s http://127.0.0.1:8799/api/health
```

### 5.1 LLM 설정 (`/opt/maya/.env`)

```
MAYA_LLM_PROVIDER=cloud
OPENAI_BASE=https://openrouter.ai/api/v1
OPENAI_MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=sk-or-v1-***
TTS_ENGINE=piper
```

> `google/gemini-2.5-flash` 는 OpenRouter **ZDR 가드레일**로 403. `openai/gpt-4o-mini` 사용 중.

---

## 6. 지금 바로 쓸 수 있는 우회 (검증 완료)

```bash
# 로컬에서
ssh -L 8433:127.0.0.1:8433 110
# 브라우저
http://127.0.0.1:8433/maya-app/
```

실측: UI 200 · API 200 · **WS 101** · `/api/ask` 한국어 응답 정상.

> Chrome 의 Local Network Access 검사로 자동화 브라우저에서는 WS 가 막힐 수 있다
> (`ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`). 일반 브라우저 또는 `127.0.0.1` 사용.

---

## 7. 검증 체크리스트 (수정 후 반드시)

```bash
# 1) 서버 내부 (101 이어야 함)
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Host: maya.privseai.com" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://127.0.0.1:8433/maya-app/ws

# 2) Cloudflare 경유 (101 이어야 함 — 현재 404)
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://maya.privseai.com/maya-app/ws

# 3) 페이지/API (200)
for p in /maya-app/ /maya-app/decisions /maya-app/knowledge /api/health; do
  printf "%-22s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' https://maya.privseai.com$p)"
done

# 4) 브라우저 실측 (콘솔 에러 0, WS OPEN 이어야 함)
node ~/.codegpt/skills/browser-automation/browser.mjs \
  https://maya.privseai.com/maya-app/ --screenshot /tmp/maya.png
```

---

## 8. 되돌리기

```bash
# nginx
cp /root/nginx-privseai.bak-<TS>.conf /etc/nginx/sites-enabled/privseai.com.conf
nginx -t && systemctl reload nginx

# Charon 라우트 제거
sqlite3 /opt/charon/charon-data/charon.db "DELETE FROM proxy_hosts WHERE id=34;"
docker restart charon

# 백엔드 중지
systemctl disable --now maya-voice
```

---

## 9. 관련 파일 (저장소)

| 파일                                  | 내용                                                       |
| ------------------------------------- | ---------------------------------------------------------- |
| `deploy/nginx-maya.conf.example`      | 배포용 nginx 예시 (`/maya-app/`, `/ws`, `/api/`)           |
| `maya-ui/backend/app/rest_chat.py`    | REST 브리지 (`/api/ask`, `/api/ask/stream`, `/api/speech`) |
| `maya-ui/backend/app/llm_fallback.py` | tier 체인 + 로컬 모델 자동 감지                            |
| `maya-ui/app/svelte.config.js`        | `MAYA_APP_BASE` 로 base 지정 (`/maya-app`)                 |
| `.env.example`                        | LLM/TTS 환경변수 문서                                      |

### 9.1 참고 — digital-twin UI (`/maya/`)는 별개 경로

`https://privseai.com/maya/` 는 **다른 UI**(digital-twin React 앱)다. `/maya-app/` 과 섞지 말 것.
그쪽은 `/api/ask` 를 어댑터(`127.0.0.1:18005`)로 받고, 음성은 브라우저 Web Speech API 다.
(2026-11-27 에 스트림 폴백 누락 수정 + voice 언어 `hi-IN` → `ko-KR` 패치 완료)
