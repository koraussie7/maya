# 110 서버 — Maya `/maya` 경로 + LLM 게이트웨이 적용 결과 (2026-11-24)

**상태**: nginx 경로 **추가·검증 완료**. 단 **업스트림 모델이 전부 unavailable** 이라
대화는 아직 응답하지 않는다(원인은 게이트웨이 계정/키 자격증명 쪽, 4절).

---

## 1. 적용한 nginx 변경 (되돌릴 수 있음)

| 항목        | 내용                                                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------ |
| 대상 파일   | `/etc/nginx/sites-enabled/privseai.com.conf`                                                                 |
| 백업        | `privseai.com.conf.bak-<HHMMSS>` (변경마다 자동 생성, 2개 보관)                                              |
| 추가한 위치 | **`listen 7684`** 서버 블록 — Maya WebSocket(`/privseai/ws`)·`/privseai/` 브라우저 UI 가 있는 곳             |
| 추가한 경로 | `location ^~ /privseai/llm/` → `proxy_pass http://127.0.0.1:20128/`                                          |
| 설정        | `proxy_buffering off` (SSE), `proxy_read_timeout 120s`, `Authorization` 헤더 전달, `Host/X-Forwarded-*` 설정 |

**왜 `listen 7684` 인가**: 이 파일에는 서버 블록이 2개 있다.

- `listen 8443` — 호스트 nginx(구 `privseai.com`, 100점) → `4004`
- **`listen 7684` — Maya 서버**. 여기에 `/privseai/`·`/privseai/ws` 가 실제로 라우팅된다.

처음엔 8443 블록에 넣었다가 `curl https://privseai.com/privseai/llm/...` 이 **403** 이라
확인 후 7684 블록으로 옮겼다. 공인 443 은 docker-proxy → nginx → 7684 순으로 들어온다.

## 2. 검증 결과 (공인 URL)

| 요청                                                         | 결과                                                        |
| ------------------------------------------------------------ | ----------------------------------------------------------- |
| `GET https://privseai.com/privseai/llm/v1/models`            | **200**, 24개 모델 목록                                     |
| `GET https://privseai.com/privseai/llm/health`               | **200**                                                     |
| `POST …/privseai/llm/v1/chat/completions` (`auto/best-fast`) | **400** `Model is unavailable` (프록시 정상, 업스트림 실패) |
| 기존 `https://privseai.com/privseai/`                        | 회귀 없음                                                   |

→ **경로·프록시는 정상**. 이제 남은 것은 모델 제공자 쪽이다.

## 3. `limit_except POST` 제거 (중요)

처음엔 생성 API 만 허용하려고 `limit_except POST { deny all; }` 를 넣었다. 그런데
`llm.py` 의 base_url 이 `…/v1` 이라 **`GET /v1/models` 가 403** 이 되어 클라이언트 초기화가 막힌다.
그리고 실사 결과 keirouter 는 **키가 없을 때 401 을 돌려주지 않고 그냥 통과**시킨다
(`/v1/models` 무키 200). 그래서 차단은 게이트웨이가 아니라 **nginx 레벨(허용 IP·rate limit)** 에서
하는 것이 맞다 → `limit_except` 제거.

## 4. 업스트림 진단 (여기서 막힘)

keirouter `auto/*` 별칭은 **업스트림 무료 제공자가 전부 실패하면** 400 을 돌려준다.
실측한 응답:

| 모델                                                                                                    | 응답                                                        |
| ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `auto/best-fast`, `auto/cheap`, `auto/chat`, `auto/gemini`, `auto/claude-sonnet`, `auto/best-reasoning` | 400 `Model is unavailable` (대안 목록 12개 반환)            |
| `oc/hy3-free`, `oc/north-mini-code-free`                                                                | 400 `[400]: Upstream request failed: Model is unavailable.` |
| `chain/fast-fallback`                                                                                   | 404 `No active credentials for provider: chain`             |
| `fast-fallback`, `auto/fast-fallback`                                                                   | 400 (체인명이 모델명으로 인식되지 않음)                     |

keirouter DB 상태: `accounts=4`, `chains=2(fast-fallback, coding-heavy)`, 최근 사용
`deepseek-v4-pro` 1건. 즉 **계정은 있지만 "활성 자격증명"이 없다** — 계정 OAuth/키 만료로 보인다.

**필요한 조치(서버 관리자 영역, 키/계정 정보를 내가 다루지 않음)**

1. keirouter 관리 UI 또는 CLI 에서 각 provider 계정 재인증(만료된 OAuth/API 키 갱신)
2. `fast-fallback` 체인에 활성 계정 연결 (`No active credentials` 해소)
3. 해결 후 이 명령으로 확인:
   ```bash
   curl -s https://privseai.com/privseai/llm/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"auto/best-fast","messages":[{"role":"user","content":"안녕"}],"stream":false}'
   ```

## 5. 앱 쪽 설정 (반영 완료)

| 파일                                 | 값                                                                                                              |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `maya-ui/backend/app/config.py`      | `MAYA_LLM_PROVIDER=private` → `OPENAI_BASE=https://privseai.com/privseai/llm/v1`, `OPENAI_MODEL=auto/best-fast` |
| `maya-ui/backend/app/llm_gateway.py` | base 정규화(`/v1` 중복 방지), `/health`, 키 필요 여부                                                           |
| `maya-ui/backend/app/llm.py`         | 정규화 적용, 20s 타임아웃, 빈 응답을 ERROR 로                                                                   |

## 6. 롤백

```bash
ssh 110 'ls /etc/nginx/sites-enabled/privseai.com.conf.bak-*'   # 최근 백업 확인
ssh 110 'cp -a /etc/nginx/sites-enabled/privseai.com.conf.bak-<ts> /etc/nginx/sites-enabled/privseai.com.conf && nginx -t && systemctl reload nginx'
```

## 7. 보안 (이번 라운드에서 함께 처리)

- `macbook-UAE` 공개키 제거 (18→17행), 백업 `authorized_keys.bak-20261124151831`
- `PermitRootLogin prohibit-password`, `PasswordAuthentication no` 적용 + `sshd -t` 검증
- `integration/server_audit_110.sh` 로 재점검용 스크립트 보관(읽기 전용)
