# 마야 야간 자동 실행 리포트 — 2026-11-25

- 시작: 01:26:50
- 호스트: 192.168.1.3 / x86_64
- 정책: 측정·합성·다운로드만. 서버 설정·배포 변경 없음.

- caffeinate: 활성


## 1. CosyVoice3 GGUF 준비

- ✅ 모델 다운로드 (8.6G)
  - cosyvoice3-f32.gguf
  - cosyvoice3-q8_0.gguf

## 2. CosyVoice3 한국어 클론

- 모델 파일: `cosyvoice3-f32.gguf`
- ❌ cosyvoice3_tts — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/cosyvoice3_tts.log)

## 3. Piper 한국어 벤치

- ❌ piper_bench — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/piper_bench.log)

## 4. TTS 폴백 엔진 비교

- ❌ tts_engines — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/tts_engines.log)

## 5. Opus 코덱 왕복 검증

- ❌ opus_check — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/opus_check.log)

## 6. LLM 게이트웨이 상태

- ❌ gateway_probe — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/gateway_probe.log)

## 7. 한국어 기억 회귀 (raw 저장 + 검색)

- ❌ vm_ko_raw — rc=125 (/Users/brianyeon/CodeGPT/maya-2/.overnight/vm_ko_raw.log)

## 8. 생성된 산출물


| 파일 | 크기 |
|------|------|

- 종료: 01:27:44
