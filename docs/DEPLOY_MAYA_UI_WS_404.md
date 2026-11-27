# Maya UI 배포 인수인계 — WebSocket 404 (2026-11-27)

> **읽는 사람**: 이어받는 에이전트/엔지니어
> **작성 근거**: 서버 `110`(185.55.240.110) 실측. 추정은 명시함.
> **작업 중단 시점 상태**: 아래 §1 그대로. **WebSocket 공개 경로만 404**.

---

## 0. 한 줄 요약

`https://maya.privseai.com/maya-app/` 는 **동작**한다(페이지 200, 대화 REST 200, TTS 200).
**WebSocket(`/maya-app/ws`)만 공개 경로에서 404**.
서버 안쪽(nginx 8433·8443 직결)은 **둘 다 101** — 즉 원인은 **공개 트래픽이 그 블록에 도달하지 않는 것**.

---

## 1. 현재 상태 (2026-11-27 실측)

| 대상 | 결과 | 확인 명령 |
| --- | --- | --- |
| `nginx` | **active** | `systemctl is-active nginx` |
| `maya-voice` (uvicorn :8799) | **active** | `systemctl is-active maya-voice` |
| 백엔드 health | **200** `{"llm_provider":"cloud","llm_model":"openai/gpt-4o-mini","tts_provider":"piper"}` | `curl 127.0.0.1:8799/api/health` |
| 공개 `/maya-app/` | **200** | `curl -o /dev/null -w '%{http_code}' https://maya.privseai.com/maya-app/` |
| 공개 `/api/health` | **200** | 동일 |
| 공개 `/maya-app/ws` | **404** ❌ | WS 핸드셰이크로 curl |
| **직결 `127.0.0.1:8433/maya-app/ws`** | **101** ✅ | `-H "Host: maya.privseai.com"` + WS 헤더 |
| **직결 `127.0.0.1:8443/maya-app/ws`** | **101** ✅ | `-k -H "Host: maya.privseai.com"` |
| 터널(로컬 8433) | **LISTEN 중** (pid 84225) | `ssh -L 8433:127.0.0.1:8433 110` |

### 1.1 공개 404 의 정체

```bash
curl -s -D- -o /dev/null https://maya.privseai.com/maya-app/ws \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="
```

```
HTTP/2 404
content-type: application/json      ← FastAPI 형식
server: cloudflare
via: 1.1 Caddy
cf-cache-status: DYNAMIC            ← 캐시 아님
cf-ray: a6111e0ca9576e67-HKG        ← Cloudflare PoP = 홍콩
(없음) x-debug-*                    ← ★ 8433/8443 maya 블록이 아님
```

**`x-debug` 헤더가 없다** = `maya-8433-block`(add_header 설정)도, maya 전용 8443 vhost 도
아닌 **다른 origin** 이 응답했다는 뜻. 서버 안쪽은 101 이므로 **공개 경로만 다른 곳으로 간다.**

---

## 2. 트래픽 경로 (실측)

```
클라이언트
  │ https://maya.privseai.com   (Cloudflare Proxied, DNS: 185.55.240.110)
  ▼
Cloudflare (HKG PoP)
  ▼
:443 IPv4 → docker-proxy  ← Charon(Caddy) 컨테이너가 점유 (0.0.0.0:443)
  │                          nginx 는 [::]:443 (IPv6) 만
  │ Charon route: maya.privseai.com → http://172.23.0.1:8433  (DB id=34)
  ▼
nginx :8433  ─┬─ location /maya-app/ws → 127.0.0.1:8799/ws   (직결 시 101 확인)
              ├─ location /maya-app/api/ → 127.0.0.1:8799/api/
              └─ location /maya-app/ → alias /var/www/privseai/maya-app/
  ▼
maya-voice (uvicorn 127.0.0.1:8799)
```

> ⚠️ **Charon은 41개 도메인을 서비스한다.** 절대 내리면 안 된다.
> nginx를 `listen 443`으로 바인딩하면 Charon과 충돌해 **nginx 전체가 죽는다**(이번에 실제 발생 → 복구함).

---

## 3. 해결된 것 (다시 건드리지 말 것)

| 항목 | 조치 |
| --- | --- |
| UI 정적 | `/var/www/privseai/maya-app/` (SvelteKit base `/maya-app`, 빌드 시 `MAYA_APP_BASE=/maya-app`) |
| 백엔드 | `/opt/maya/ui/backend/` → `maya-voice.service` |
| 대화 자산 | `/opt/maya/assets/{personas,dialog}` |
| 환경변수 | `/opt/maya/.env` (chmod 600). `MAYA_LLM_PROVIDER=cloud`, OpenRouter `openai/gpt-4o-mini`, `TTS_ENGINE=piper` |
| **piper 모델** | `/opt/maya/maya/models/tts/piper/ko_KR-kss-medium.onnx` (63MB, 업로드 완료) |
| **tts_local.py** | `/opt/maya/integration/tts_local.py`, `piper_worker.py` |
| **PYTHONPATH** | systemd `Environment=PYTHONPATH=/opt/maya/ui/backend:/opt/maya/integration:/opt/maya` |
| **piper-tts 패키지** | `pip3 install piper-tts` |
| TLS 인증서 | `/etc/letsencrypt/live/maya.privseai.com/` (만료 2027-02-25, certbot webroot `/var/www/acme-maya`) |

### 3.1 이번에 고친 TTS 버그 (중요)

`/api/speech` 가 503 이었고 WS 응답이 `tts 0.00s (0B)` 였다. 원인 3개 연쇄:

1. piper 모델이 서버에 없었다 → 63MB 업로드
2. `tts_local.py` 미배포 + `PYTHONPATH` 누락 → `LocalTTS` 로드 실패
3. `piper-tts` 미설치 → 최종적으로 `say`(Linux에 없음)로 떨어져 0바이트

조치 후: `/api/speech` **200 60,930 bytes**, WS `audio.out` 정상.

---

## 4. nginx 현황 (수정 위치)

파일: `/etc/nginx/sites-enabled/privseai.com.conf`

```nginx
# (a) 8433 catch-all — Charon 업스트림. 디버그 헤더 있음
server {
    listen 8433 default_server;
    server_name _;
    add_header X-Debug-Host $host always;        # ← 진단용, 제거 예정
    add_header X-Debug-Uri $uri always;
    add_header X-Debug-ServerName $server_name always;
    add_header X-Debug-Port $server_port always;

    location /maya-app/ws {                      # ★ = 가 아니라 prefix
        proxy_pass http://127.0.0.1:8799/ws;     # ★ URI 필수
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s; proxy_send_timeout 3600s;
    }
    location = /ws { proxy_pass http://127.0.0.1:8799/ws; ... }
    location ^~ /maya-app/api/ { ... }
    location ^~ /maya-app/ { alias /var/www/privseai/maya-app/; try_files $uri $uri/ /maya-app/index.html; }
    location / { root /var/www/privseai/maya-app; ... }
}

# (b) maya 전용 vhost — 8443(TLS) + 8433(평문) 둘 다 listen
server {
    listen 8433;
    listen 8443 ssl;
    server_name maya.privseai.com;
    location /maya-app/ws { proxy_pass http://127.0.0.1:8799/ws; ... }   # ★ 추가함
    location = /ws { proxy_pass http://127.0.0.1:8799/ws; ... }
    ...
}
```

### 4.1 nginx에서 배운 함정 (반복 금지)

| 함정 | 사실 |
| --- | --- |
| `location = /maya-app/ws` + `proxy_pass http://x:8799` | 정확 매칭에서는 **URI가 그대로** 전달 → 백엔드 404 |
| `rewrite ... break` | location 재탐색 안 함 → `/maya-app/ws` 그대로 전달 → 404 |
| `rewrite ... last` + `^~` | `^~`가 재탐색에서도 우선해 같은 블록에 재진입 → 실패 |
| **정답** | `location /maya-app/ws { proxy_pass http://127.0.0.1:8799/ws; }` (prefix + URI) |
| `systemctl reload nginx` | 일부 변경이 **반영되지 않았다.** `restart` 사용 |
| `add_header` | **location으로 상속되지 않는다** → 하위 location에서 안 보임 |
| `log_format` 을 conf 최상단에 | nginx 문법 깨짐 (`http` 블록 안에만 허용) |
| `listen 80` maya 블록 추가 | 호스트 80은 다른 nginx 점유 → **nginx 전체 다운** |
| `listen 443` / `listen IP:443` | docker-proxy(Charon)와 충돌 → **nginx 전체 다운** |

---

## 5. 다음 에이전트가 할 일 (여기서 멈춤)

### 5.1 첫 확인 — Charon이 실제로 어디로 보내는가

```bash
# DB (원본)
sqlite3 /opt/charon/charon-data/charon.db \
  "select id,domain_names,forward_scheme,forward_host,forward_port from proxy_hosts where id=34;"
# 현재: maya.privseai.com | http | 172.23.0.1 | 8433

# 생성된 Caddy config 의 실제 dial
python3 - <<'PY'
import json,glob,os
f=sorted(glob.glob("/opt/charon/charon-data/caddy/config-*.json"), key=os.path.getmtime)[-1]
d=json.load(open(f)); v=d["apps"]["http"]["servers"]["charon_server"]
for r in v["routes"]:
    h=(r.get("match") or [{}])[0].get("host") or []
    if any("maya" in x for x in h):
        for hd in r["handle"]:
            if hd.get("handler")=="reverse_proxy": print("DIAL:", hd.get("upstreams"))
PY
```

### 5.2 컨테이너에서 도달 확인 (이게 되면 Charon 설정 문제)

```bash
docker exec charon wget -q -S -O - --timeout=4 \
  --header="Host: maya.privseai.com" http://172.23.0.1:8433/api/health
# → 200 이 나와야 정상. 404/타임아웃이면 Charon → 호스트 경로 문제.
```

### 5.3 공개 요청이 어느 origin 에 닿는지 구분

`x-debug-*` 헤더의 **유무**가 핵심이다.
- 있음 → 8433 catch-all 도달 (그 안에서 location 문제)
- **없음 → 완전히 다른 origin** (Charon이 8433이 아닌 곳으로 보냄)

```bash
curl -s -D- -o /dev/null https://maya.privseai.com/maya-app/ws \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" | grep -iE "^HTTP|x-debug|content-type|^via"
```

### 5.4 유력 해결안 (권장 순)

**A. Charon 의 `advanced_config` 로 Host/Upgrade 보존 명시**
Charon UI(또는 `proxy_hosts.advanced_config`)에서:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_http_version 1.1;
```

> 이전에 `advanced_config`에 Caddy `https_upstream`을 넣었다가 업스트림이 `:80`으로
> 되돌아간 사례가 있다. **Caddy JSON 구조가 아니라 NPM 호환 nginx 스니펫 필드**여야 한다.

**B. 새 서브도메인 + Cloudflare(Proxied) + 원본 포트 직접**

`maya-app.privseai.com` 등 **Charon 라우트가 없는 새 이름**을 만들어
Cloudflare → origin `:8443`(nginx maya vhost, TLS)로 보낸다.
Cloudflare Origin Certificate를 nginx 8443에 걸면 SSL 경고도 없다.

**C. 지금 즉시 사용 (검증 완료)**

```bash
ssh -L 8433:127.0.0.1:8433 110
# 브라우저: http://127.0.0.1:8433/maya-app/
```
UI·API·WS·TTS·한국어 대화 전부 동작.

---

## 6. 되돌리기

```bash
# nginx (가장 최근 백업 사용)
ls -t /root/privseai.conf.bak* | head -1
cp $(ls -t /root/privseai.conf.bak* | head -1) /etc/nginx/sites-enabled/privseai.com.conf
nginx -t && systemctl restart nginx

# Charon 라우트 제거
sqlite3 /opt/charon/charon-data/charon.db "DELETE FROM proxy_hosts WHERE id=34;"
docker restart charon

# 백엔드 중지
systemctl disable --now maya-voice
```

백업 목록: `/root/privseai.conf.bak*`, `/root/nginx-privseai.bak*.conf`,
`/root/charon.db.bak-*`

---

## 7. 검증 체크리스트 (수정 후)

```bash
# 1) 서버 직결 (101 이어야 함)
for u in "http://127.0.0.1:8433/maya-app/ws"; do
  curl -s -o /dev/null -w "%{http_code}\n" -m 6 \
    -H "Host: maya.privseai.com" -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" "$u"
done
curl -sk -o /dev/null -w "%{http_code}\n" -m 6 \
  -H "Host: maya.privseai.com" -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" https://127.0.0.1:8443/maya-app/ws

# 2) 공개 (101 이어야 함 — 현재 404)
curl -s -o /dev/null -w "%{http_code}\n" -m 12 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://maya.privseai.com/maya-app/ws

# 3) 페이지/API (200)
for p in /maya-app/ /maya-app/decisions /maya-app/knowledge /api/health; do
  printf "%-22s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' https://maya.privseai.com$p)"
done

# 4) 브라우저
node ~/.codegpt/skills/browser-automation/browser.mjs \
  https://maya.privseai.com/maya-app/ --screenshot /tmp/maya.png
```

---

## 8. 자주 쓰는 진단

```bash
# nginx 가 로드하는 전체 설정
nginx -T 2>/dev/null | grep -nE "listen |server_name|location"

# 8433/8443 에 maya 블록이 몇 개인가
nginx -T 2>/dev/null | grep -c "listen 8433"
nginx -T 2>/dev/null | awk '/listen 8443/{f=1} f&&/server_name/{print; f=0}'

# 최근 요청이 어느 client(Charon=172.23.0.2) 인지
tail -20 /var/log/nginx/access.log

# Charon 컨테이너 네트워크/게이트웨이
docker inspect charon --format '{{.HostConfig.NetworkMode}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
docker network inspect charon_default --format '{{range .IPAM.Config}}gw={{.Gateway}}{{end}}'

# 백엔드 WS 직접 확인 (101)
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" http://127.0.0.1:8799/ws
```

---

## 9. 저장소 관련 파일

| 파일 | 내용 |
| --- | --- |
| `deploy/nginx-maya.conf.example` | 배포 nginx 예시 |
| `maya-ui/backend/app/rest_chat.py` | REST 브리지 (`/api/ask`, `/api/ask/stream`, `/api/speech`, `/api/health`) |
| `maya-ui/app/svelte.config.js` | `MAYA_APP_BASE` 로 base 지정 |
| `.env.example` | LLM/TTS 환경변수 |

### 9.1 별개 UI — `/maya/` (digital-twin)

`https://privseai.com/maya/` 는 **다른 UI**(React)다. `/maya-app/` 과 섞지 말 것.
어댑터 `127.0.0.1:18005`, 음성은 브라우저 Web Speech API.
(2026-11-27: 스트림 폴백 누락 수정, voice 언어 `hi-IN` → `ko-KR` 패치 완료)
