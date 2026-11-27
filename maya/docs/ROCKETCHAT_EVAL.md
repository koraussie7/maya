# Rocket.Chat 을 Maya UI / WebRTC 로 쓸 수 있나 — 평가 (2026-11-25)

> 링크: https://github.com/RocketChat/Rocket.Chat
> 질문: "이거 Maya UI 로 사용 가능? WebRTC 들어있어?"
> 결론: **Maya UI 대체는 불가. WebRTC도 Rocket.Chat 자체 기능이 아니다(Jitsi 등 외부 서버 전제).
> 쓸 수 있는 건 "관리자↔사용자 상담 채널" 로 한정이고, 그마저도 110 서버 메모리가 빠듯하다.**

---

## 1. 질문에 대한 직답

| 질문                                              | 답                | 근거                                                                                                                                |
| ------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Maya UI(SvelteKit 한 페이지 음성 비서) 대체 가능? | ❌                | Rocket.Chat 은 Meteor + React 기반 **채팅 워크스페이스**. Maya 의 `/ws/audio`·`AudioWorklet`·`session.start` 프로토콜과 접점이 없다 |
| WebRTC 들어있나?                                  | ⚠️ **반만**       | 음성/영상 통화는 **Jitsi Meet(또는 외부 미디어 서버) 앱을 연동**해야 동작한다. Rocket.Chat 코어에 미디어 서버가 없다                |
| 자기 서버에 올릴 수 있나?                         | ✅ (단 조건 있음) | MongoDB **replica set 필수**, NATS, Rocket.Chat 본체 — 최소 3개 프로세스                                                            |
| 우리 110 서버에 올라가나?                         | ⚠️ **빡빡**       | 4 vCPU / 15GB 중 **이미 5GB 사용, 가용 9GB**. Rocket.Chat 최소 권장이 Rocket.Chat 4GB + Mongo 4GB → 여유 거의 없음                  |

---

## 2. 실제 요구사항 (문서 실측)

| 항목                         | 값                                                                     | 출처                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| 최소 구성 (≤500 동시 사용자) | Rocket.Chat 2 vCPU / **4 GiB**, MongoDB 2 vCPU / **4 GiB** / replica 3 | [System Requirements](https://docs.rocket.chat/docs/system-requirements)                               |
| DB                           | MongoDB — **replica set 필수**(단일 노드 replica set 은 허용)          | [Configure External MongoDB](https://docs.rocket.chat/docs/configure-external-mongodb-for-rocket-chat) |
| 메시징                       | NATS (`NATS_URL`), WebSocket 기반 실시간                               | [Architecture Overview](https://developer.rocket.chat/docs/architecture-and-components)                |
| 런타임                       | Node 22.22+/24.15+ (공식 Docker 이미지에 포함)                         | 위 System Requirements                                                                                 |
| 실시간 통신                  | REST + WebSocket (채팅). **음성/영상은 Jitsi 등 별도**                 | 위 Architecture Overview                                                                               |

### 우리 110 서버 실측 (2026-11-25)

```
$ ssh 110 'docker ps; free -g; nproc'
charon  wikid82/charon:latest  Up 16 hours (healthy)     ← 현재 유일한 컨테이너
Mem: total 15  used 5  free 0  buff/cache 8  available 9
CPU: 4 cores
```

→ **가용 9GB**. Rocket.Chat(4) + Mongo(4) 넣으면 **1GB 이하로 남는다**. Maya 백엔드/STT 까지 같은 서버에 있으므로 OOM 위험.
→ 선택지: ① 서버 증설(16→32GB) ② Rocket.Chat 은 외부 SaaS ③ **Rocket.Chat 도입 안 함**.

---

## 3. Maya 와 무엇이 겹치고 무엇이 안 겹치나

| 계층        | Maya 현재                                                                  | Rocket.Chat                          | 재사용 가능?   |
| ----------- | -------------------------------------------------------------------------- | ------------------------------------ | -------------- |
| UI          | SvelteKit 단일 페이지 (`+page.svelte`), AudioWorklet PCM16k                | React 워크스페이스(채널/스레드/파일) | ❌ (전혀 다름) |
| 마이크 캡처 | `getUserMedia` + `AudioWorklet` → 16k PCM → WS                             | WebRTC(피어) — **Jitsi iframe/앱**   | ❌             |
| 전송        | `/ws/audio` 커스텀 프로토콜 (`session.start`, `audio.chunk`, `chat.final`) | Meteor DDP + REST                    | ❌             |
| STT/TTS     | SenseVoice / Piper / VoxCPM 클론                                           | **없음**                             | ❌             |
| 대화 LLM    | OpenRouter/자체 게이트웨이/AirLLM                                          | **없음** (앱으로 붙여야)             | ❌             |
| 계정·권한   | 페르소나 yaml                                                              | LDAP/OAuth/SAML, RBAC                | ⚠️ 필요할 때만 |

**핵심**: Rocket.Chat 은 "화면과 대화 기록을 관리하는 껍데기"이고, Maya 의 가치는 **음성 파이프라인(STT→LLM→클론 TTS)** 에 있다. Rocket.Chat 을 UI 로 쓰면 그 파이프라인을 **전부 새로 앱으로 이식**해야 한다 — 얻는 것보다 잃는 게 크다.

---

## 4. 그럼 언제 쓸 만한가 (유일한 현실 시나리오)

**"상담원이 여러 사용자와 동시에 음성 상담"** 하는 운영 콘솔이 필요할 때만.

```
사용자 브라우저 ──(Jitsi WebRTC 음성)──▶ Jitsi 서버 ──┬─▶ 상담원
                                                    └─▶ Maya 봇(assistant 앱, REST/WS 로 Maya 백엔드 호출)
```

- Rocket.Chat 이 **사람 상담원 + 대화 이력 + 파일 공유**를 담당
- Maya 는 **봇 계정 하나**로 붙어 STT/TTS/LLM 을 처리
- 이 경우 필요: Rocket.Chat + MongoDB + NATS + **Jitsi(JVB 2개 + Prosody + Coturn)** → 110 서버로는 불가, 별도 노드 필요

---

## 5. 권장 조치

| 우선순위   | 조치                                                                                                     | 비용                   |
| ---------- | -------------------------------------------------------------------------------------------------------- | ---------------------- |
| **권장**   | **도입하지 않는다.** Maya UI 유지 + 필요하면 `/maya` 페이지에 "상담원 연결" 버튼만 추가(Jitsi room 링크) | 0.5일                  |
| 대안 A     | 음성 통화가 정말 필요하면 **Jitsi Meet 단독**만 붙인다 (Rocket.Chat 없이). CPU/대역폭만 필요, DB 불필요  | 1~2일                  |
| 대안 B     | Rocket.Chat 도입은 **서버 32GB 증설 + Jitsi 별도 노드** 확보 후                                          | 인프라 비용            |
| 하지 말 것 | 15GB 서버에 Rocket.Chat+Mongo+Jitsi 를 몰아넣기                                                          | OOM / Maya 백엔드 다운 |

---

## 6. 참고 링크

- [RocketChat/Rocket.Chat](https://github.com/RocketChat/Rocket.Chat)
- [System Requirements](https://docs.rocket.chat/docs/system-requirements)
- [Architecture Overview](https://developer.rocket.chat/docs/architecture-and-components)
- [Configure External MongoDB (replica set)](https://docs.rocket.chat/docs/configure-external-mongodb-for-rocket-chat)
