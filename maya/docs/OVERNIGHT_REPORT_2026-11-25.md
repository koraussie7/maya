# 마야 야간 자동 실행 리포트 — 2026-11-25

- 시작: 07:45:23
- 호스트: 192.168.1.3 / x86_64
- 정책: 측정·합성·다운로드만. 서버 설정·배포 변경 없음.

- caffeinate: 활성


## 1. CosyVoice3 GGUF 준비

- ✅ 모델 준비됨 (8.6G)
  - cosyvoice3-f32.gguf
  - cosyvoice3-q8_0.gguf

## 2. CosyVoice3 한국어 클론

- 모델 파일: `cosyvoice3-f32.gguf`
- ❌ cosyvoice3_tts — rc=1 (`missing required --task argument`, 당시 러너가 서브커맨드 형식으로 호출)
- ✅ **재실행 성공** (`cv3_clone_bench.py`, q8_0, cpu x8) — 벽시계 92.35s / 오디오 6.30s
  - RTF **14.66** 벽시계 기준 / **12.40** 순수 추론(78.1s, 모델 로드 제외) · peak RSS 2702MB · 24kHz mono
  - F0 레퍼런스 220.7Hz → 출력 204.3Hz (**-16.4Hz**, 같은 여성 음역)
  - 재전사: "안녕하세요 저는 나이이라예요 오늘 하루는 좀 어떠셨나요?" (유사도 90.2%)
  - → 오프라인 복제는 가능, **실시간 대화에는 부적합** → piper 유지

## 3. Piper 한국어 벤치

- ✅ piper_bench — 3s
  - engine: piper
  -    0.23s   0.93s  RTF  0.25  | 네.
  -    0.23s   2.55s  RTF  0.09  | 안녕하세요, 오늘 하루 어떠셨어요?
  -    0.34s   4.39s  RTF  0.08  | 내일 아침 아홉 시에 회의가 있으니까 조금 일찍 시작하

## 4. TTS 폴백 엔진 비교

- ✅ tts_engines — 6s
  - say    engine=say    2.71s 0.91s RTF 2.96
  - piper  engine=piper  0.22s 1.25s RTF 0.18

## 5. Opus 코덱 왕복 검증

- ✅ opus_check — 0s
  - 프레임 수: 50
  - 압축 크기: 2499 bytes  (비율 12.81x)
  - 디코드 후: 32000 bytes
  - 처리 시간: 15.4 ms (3238 frames/s)

## 6. LLM 게이트웨이 상태

- ❌ gateway_probe — rc=1 (/Users/brianyeon/CodeGPT/maya-2/.overnight/gateway_probe.log)
  -   python-urllib/3.11                               성공 0/3  중앙지연 Nones
  -   MayaLive/0.1 (+https://privseai.com/maya)        성공 0/3  중앙지연 Nones
  - 판정: 성공률 절반 미만 → 대화 경로에 넣지 말 것 (게이트웨이 계정 재인증 또는 로컬/클라우드 대체 필요)

## 7. 한국어 기억 회귀 (raw 저장 + 검색)

- ✅ vm_ko_raw — 29s
  -   이미 저장된 기억이 있어 저장 단계를 건너뜁니다
  - 저장된 기억 3건
  -   [식이 제한]  1393ms top1_ok=True  내 식이 제한이 뭐야?
  -   [거주지/이름]    79ms top1_ok=True  내가 사는 곳이 어디라고 했지?
  -   [직장]    77ms top1_ok=True  출근 시간이 몇 시였지?
  -   [이름]    75ms top1_ok=True  내 이름이 뭐라고 했지?
  - 요약: top1 정답 4/4  (로그를 키운 검색 1건)

## 8. 생성된 산출물

| 파일 | 크기 | 내용 |
|------|------|------|
| `integration/gateway_probe.json` | 1,838 B | 게이트웨이 3회 × 2 UA 프로브 결과 (전부 400) |
| `integration/vm_ko_raw_memory.json` | 3,029 B | 한국어 기억 4질의 top1/스코어 상세 |
| `/tmp/cv3_bench.wav` | 302,444 B | CosyVoice3 클론 출력 6.30s @ 24kHz mono |
| `/tmp/cv3_bench.json` | 958 B | 위 합성의 RTF/F0/STT 유사도 리포트 |
| `.overnight/*.log` (7개) | 28 KB | 단계별 stdout/stderr 원본 |

### 8.1 이번 러너에서 실패한 2건 (산출물 아님, 기록)

| 단계 | 실패 원인 | 조치 |
|------|-----------|------|
| cosyvoice3_tts | `audiocpp_cli` 를 서브커맨드 형식으로 호출 → `missing required --task argument` | `cv3_clone_bench.py` 로 재실행 성공 (§2) |
| gateway_probe | 6회 프로브 전부 HTTP 400 `Model is unavailable` (모델명 `oc/hy3-free`, `nvidia/z-ai/gl...`) | 게이트웨이 계정 재인증 또는 로컬/클라우드 대체 — 대화 경로 미투입 |

- 종료: 07:46:19
