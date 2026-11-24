# Maya 대화 경로 LLM — 자체 서버(110) 게이트웨이로 전환

**날짜**: 2026-11-24
**결론**: `auto/best-fast` 같은 라우팅 별칭은 `/opt/keirouter`(`:20128`)에만 있다.
`/opt/smart-router`(`:20180`)는 경로가 자체 정의(`smart/best-fast`, `smart/gapfill`)이고 `auto/*`가 없다.
→ **keirouter 를 대화 경로로 쓰고**, 외부에는 nginx `/llm/` 로만 노출한다.

---

## 1. 서버 실사 (185.55.240.110, 본인 소유 · privseai.com 동일 서버)

| 항목                | 확인 결과                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| `/opt/keirouter`    | **keirouter v0.0.1**, `:20128` (127.0.0.1/사설망만 리스닝)                                        |
| API                 | OpenAI 호환 — `GET /v1/models`, `POST /v1/chat/completions` (+ `/health`)                         |
| 인증                | **필수** — 키 없이 `/v1/models` 조차 `401`. `Authorization: Bearer <api_key>`                     |
| 키 저장소           | `/root/.keirouter/keirouter.db` → `api_keys(name, hash, display, scopes, disabled, last_used_at)` |
| 계정/체인           | accounts 4, chains 23, usage_records 19,185 — 이미 운영 중인 라우터                               |
| `/opt/smart-router` | `:20180`, 별도 포크. `/health` 만 인증 없이 응답. **`auto/*` 별칭 없음**                          |
| `:4002`             | 리스닝만 되고 HTTP 무응답 → 사용 안 함                                                            |
| 외부 공개           | 20128 은 외부 비노출 → 공개하려면 nginx 프록시 필요                                               |

**중요**: `auto/*` 는 keirouter 의 합성 별칭이고, 20180 으로 `auto/best-fast` 를 보내면 모델을 못 찾는다.
(질문에서 나온 "smartrouter-9" 같은 이름은 이 서버에 없다.)

## 2. 코드 반영 (이 맥 저장소)

| 파일                                        | 변경                                                                                                                                                                              |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `maya-ui/backend/app/config.py`             | `MAYA_LLM_PROVIDER=private\|cloud\|local` — **기본 private**. private → `https://privseai.com/llm`, `auto/best-fast`                                                              |
| `maya-ui/backend/app/llm_gateway.py` (신규) | `normalize_base_url()`(`/v1` 중복 방지), `needs_api_key()`, `health_url()`, 서버 실제 경로 상수                                                                                   |
| `maya-ui/backend/app/llm.py`                | base URL 정규화 적용, private 에서 `reasoning.enabled=false`·`chat_template_kwargs.enable_thinking=false`(사고 모델 첫 토큰 지연 방지), 타임아웃 기본 20s, **빈 응답을 ERROR 로** |
| `maya-ui/docs/DEPLOY_privseai_maya.md`      | systemd 예시를 private로 갱신, 키는 환경변수만                                                                                                                                    |

```
기존: 음성 → 로컬 LLM (9s~5분+, 품질 불안정)
변경: 음성 → 로컬 STT → privseai.com/llm (keirouter → 무료 제공자 라우팅) → TTS
```

## 3. 남은 서버측 작업 (키를 내가 다루지 않음)

1. **전용 키 발급** (레포/문서에 값 금지)
   ```bash
   ssh 110
   sqlite3 /root/.keirouter/keirouter.db "select id, name, scopes, disabled from api_keys;"
   # UI/CLI 가 있으면 거기서 발급, 없으면 키 발급 절차 확인 후 새 키 생성
   ```
2. **systemd 환경변수** — `/etc/systemd/system/maya-backend.service`
   ```ini
   Environment=MAYA_LLM_PROVIDER=private
   Environment=OPENAI_BASE=https://privseai.com/llm
   Environment=OPENAI_MODEL=auto/best-fast
   Environment=OPENAI_API_KEY=<발급 키>
   Environment=MAYA_LLM_TIMEOUT_S=20
   ```
3. **nginx** — `/llm/` → `127.0.0.1:20128`, 스트리밍 필수 설정
   ```nginx
   location /llm/ {
       proxy_pass http://127.0.0.1:20128/;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header Authorization $http_authorization;
       proxy_buffering off;          # SSE 스트리밍이 뭉치지 않게
       proxy_read_timeout 120s;
   }
   ```
4. **확인**
   ```bash
   curl -s https://privseai.com/llm/v1/models -H "Authorization: Bearer <key>" | head -c 300
   curl -s -N https://privseai.com/llm/v1/chat/completions -H "Authorization: Bearer <key>" \
     -H 'Content-Type: application/json' \
     -d '{"model":"auto/best-fast","messages":[{"role":"user","content":"안녕"}],"stream":true}'
   ```
5. 맥북에서도 같은 값을 쓸 때는 **SSH 터널**(`ssh -L 20128:127.0.0.1:20128 110`) 또는 nginx 경로 사용.
   20128 을 공인망에 직접 열지 않는다.

### 3.1 ⚠️ nginx 를 만지기 전에 반드시 확인할 것 (2026-11-24 실사)

`/etc/nginx/sites-enabled/privseai.com.conf` 의 구조가 일반적이지 않다. **`listen 443 ssl` 은 꺼져 있고
`listen 8443 ssl` 이다** — 실제 공인 443 은 **docker-proxy** 가 잡고 있어서, nginx 에 `/llm/` 만 추가하면
`privseai.com`(443) 에는 반영되지 않는다.

| 항목 | 실제 값 |
|------|---------|
| 443 | `docker-proxy` (컨테이너 내부 nginx?) |
| 8443 | 호스트 nginx (`privseai.com.conf`, `proxy_pass http://127.0.0.1:4004`) |
| 4004 | `python3` 프로세스(pid 4607), `/` → **501** (경로별만 응답) |
| 18000 | aivoice 백엔드(`/aivoice/`, `/live/` 등) — **자동 생성된 역터널의 목적지로 보임** |
| maya 관련 nginx 설정 | **없음** (`grep maya` 결과 0줄) — `privseai.com/maya` 는 프론트 정적 배포만 있는 상태 |

→ `/llm/` 을 넣을 위치는 **443 을 받는 쪽(컨테이너)** 이다. 호스트 `privseai.com.conf` 에 넣으면
443 사용자에게는 효과가 없다. 그리고 **18000 포트는 자동 터널과 연결돼 있을 수 있으므로 손대지 말고,
터널을 먼저 끊은 뒤 aivoice 백엔드를 localhost 전용으로 바꾼다.**

## 4. 보안 체크리스트 (이전 사고 재발 방지)

- [x] `~/.ssh/authorized_keys` 검토 — **`macbook-UAE` 키 제거 완료**(2026-11-24 15:18, 18행→17행, 백업 `~/.ssh/authorized_keys.bak-20261124151831`). 재접속 정상
- [ ] **네트워크 출처 재확인 필요**: sshd 성공 접속 IP 가 `14.231.220.225`(256회) · `113.190.202.40`(26회) 두 개뿐이고,
      실패 시도 IP 도 `14.231.220.225`(950회) 에 집중돼 있다. 제거한 `macbook-UAE` 키가 어느 IP 에서 쓰였는지,
      `14.231.220.225` 가 본인 맥/회선인지 확인할 것
- [ ] `crontab -l`, `/etc/cron.d/` 에 모르는 작업 없는지
- [ ] `autossh` 역터널(`-R 18000:127.0.0.1:8000`) 이 남아 있으면 `launchctl bootout gui/$UID/com.maya.tunnel` 로 중지
- [ ] keirouter 키는 **파일이 아니라 환경변수**, 로그에 찍히지 않게(`llm.py` 는 상태코드만 로깅)
- [ ] 무료 제공자 라우팅은 속도/가용성 보장이 없다 → `MAYA_LLM_TIMEOUT_S=20` 으로 상한, 실패는 ERROR 이벤트로 사용자에게 표시

## 5. 다음 측정

`integration/llm_chat_bench.py` 에 게이트웨이를 넣어 TTFT/총시간/한국어율을 잰다(키는 환경변수로만).

```bash
OPENAI_API_KEY=<key> .venv-voicemem/bin/python integration/llm_chat_bench.py \
  --base http://127.0.0.1:20128 --models auto/best-fast
```
