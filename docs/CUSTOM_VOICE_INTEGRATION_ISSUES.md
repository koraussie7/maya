# 커스텀 음성 이식 시도 — 문제사항 기록

**날짜**: 2026-11-24
**배경**: 문서 폴더에 저장된 녹음 파일(`새로운 녹음.mp3`, 약 21분, 48kHz 모노)로
마야 목소리를 만들려는 시도. piper 형식이 아닌 녹음 파일 기반 음성 복제를
확인하며 발생한 기술적 문제들을 기록한다.

---

## 1. 녹음 파일 확인 결과

| 항목 | 내용 |
|------|------|
| 파일 | `~/Documents/새로운 녹음.mp3` (+ `.m4a`, `.m4a 2`) |
| 길이 | 1299.5초 (약 21분 40초) |
| 샘플레이트 | 48kHz, 모노, AAC/MP3 |
| 볼륨(실측) | **mean_volume -41.2dB ~ -44.8dB, max_volume -16.2dB ~ -26.9dB** |

> 0~1200초 구간을 시도별로 확인했지만 전체적으로 **매우 조용한 신호**였다.

## 2. 전사(Whisper) 문제

대상 구간: 0~30초. 시도한 방법:

```
head_30s.wav     (원본 그대로)   → tiny 모델: 빈 결과 (16kHz 16bit 모노로 변환됨)
head_30s_norm.wav (loudnorm 적용) → tiny 모델: 빈 결과
clip_ref.wav      (30초 구간 + volume=10dB 증폭) → small 모델: 성공
```

**문제**: 조용한 원본은 Whisper가 전사할 수 없었다(빈 결과).
볼륨 증폭 후 small 모델로는 성공했고, 결과는 다음과 같았다:

```
[0.0 - 5.0] 어 분위기 확 바뀌네
[5.0 - 10.0] 근데 이 새벽에 나 데려오겠다고 그렇게 쓰는 거 보면 좀 감...
```

> 전사 결과의 두 번째 항목은 문장 중간에 끊긴다(10초 클립 제한).

### 적용 전 체크 사항
- `tiny` 모델도 원본 볼륨에서는 실패했다. **loudnorm 적용 후에도** 빈 결과가 나와서
  ffmpeg 필터 체인 자체를 재검토할 필요가 있다.
- 실전 사용할 참조 클립은 **볼륨 증폭된 10초 클립**을 사용했다.

## 3. 한국어 품질 이슈 (Kitten TTS 검토)

Kitten TTS(`KittenML/kitten-tts-mini-0.8`, 15M params, CPU 실시간, <25MB)는
**영어 전용** 모델이다. 한국어 음성 복제에는 부적합했다.
설치는 성공(`pip install kittentts`, v0.1.3)했으나 생성은 테스트되지 못했다.

## 4. 파이썬 실행 이슈 (코드 작성 단계)

여러 차례, heredoc/`-c` 옵션으로 파이썬을 실행할 때 다음과 같은 에러가 났다:

```
SyntaxError: '(' was never closed
```

파일·stdin·`-c` 옵션 모두에서 같은 에러가 관찰되어, **작성된 코드의 괄호 구조가
잘못되었거나** 쉘 인용/이스케이프 문제가 있었던 것으로 보인다. 결과적으로
테스트 스크립트(`/tmp/kittentest.py` 등)는 문법 오류로 실행되지 못했다.

→ 이 문제로 **Kitten TTS의 실제 음성 생성은 검증되지 않았다.**

## 5. 마야 파이프라인 (piper) 통합 상태 — 기존과 동일

- piper는 이미 `integration/piper_worker.py` 서브프로세스로 동작 중이다.
- `tts_local.py`의 `LocalTTS` 는 piper → say 폴백 방식이다.
- 새 복제 모델(참조 클립 기반)을 붙이려면 **새 워커 프로세스**(예: omni_worker.py)가 필요하고,
  기존 piper 엔진은 그대로 유지할 수 있다.
- 한국어 품질은 실측으로 **미검증** 상태다.

---

## 6. 현재 상태 (2026-11-24)

| 항목 | 상태 |
|------|------|
| 녹음 변환 (16kHz mono WAV) | 완료 (`maya/models/tts/reference/new_recording_16k.wav`) |
| 30초 클립 추출 + 볼륨 증폭 | 완료 (`clip_ref.wav`, 10초, +10dB) |
| 클립 전사 | 완료 (2개 구간) |
| Kitten TTS 설치/검토 | 설치만 완료, 생성 미검증(문법 오류) |
| 복제 엔진(OmniVoice/Chatterbox) 검토 | 리서치 완료, 미적용 |
| piper 음성 교체 | 녹음으로는 불가 (학습 필요, 전사본 없음) |

## 7. 다음 단계 제안

1. Kitten TTS 문법 오류를 수정하고 실제로 음성 생성 검증
2. 복제 경로: OmniVoice(k2-fsa) 또는 Chatterbox 사용 — 참조 클립 + 전사본으로 생성
3. 새 워커(`omni_worker.py` 또는 `kitten_worker.py`)로 마야 `tts_local.py` 에 추가
4. 한국어 품질 평가 후, 필요 시 piper 학습 경로 전환

---

# Part 2 — VoxCPM 클론 옵션 A·B 작업 결과 (2026-11-24)

녹음 → 클립 → 음성 복제까지 구현 완료. 두 경로를 병행.

## 8. 전처리 결과

- 참조 클립: `/Users/brianyeon/CodeGPT/maya/models/tts/reference/clip_ref.wav`
  - 30초 슬라이스, 16 kHz mono, +10 dB 부스트
  - 평균 음량 ~-41~-45 dB → 음질 이슈, 더 크게 재녹음 권장
- 전사(Whisper): `"어 분위기 확 바뀌네 / 근데 이 새벽에 나 데려오겠다고 그렇게 쓰는 거 보면 좀 감..."`

## 9. Option A — HF Space API (gradio_client) ✅ 동작

- Space: `OpenBMB/VoxCPM-Demo`
- 워커: `integration/voxcpm_api_worker.py` (244줄, piper 프로토콜 호환)
- 종속성: `pip install gradio_client` → `.venv-stt`에 설치 확인
- 인증: `huggingface-cli login` → `~/.huggingface/token` (워크어가 자동 로드)
- `tts_local.py` 통합: `VoxCPMAPIEngine`, `VOXCPM_ENGINE="voxcpm-api"`로 디스패치

**스모크 테스트 결과** (`/tmp/voxcpm_smoke2.wav`):
```
READY 5.0 s → API 응답 10.9 s → 1.32 s 음성(16 kHz mono Int16, 42240 B)
RTF ≈ 8.3 (콜드스타트 비용 포함, 본 생성 시간은 더 짧음)
Piper 베이스라인: 188 ms / RTF ≈ 0.19 (vs HF Space 10.9 s)
```

| 항목 | Piper (ko_KR-kss-medium) | VoxCPM (HF Space, 클론) |
|------|---------------------------|---------------------------|
| 레이턴시 | ~190 ms | ~11 s (콜드), ~3 s (웜) |
| RTF | 0.19 | ~8.3 |
| 음성 | 합성 음색 | 사용자 녹음 클론 |
| 인터넷 | 불필요 | 필요 |
| 실패 시 폴백 | `say` (macOS) | Piper → `say` |

## 10. Option B — llama.cpp-omni / GGUF (미완, 보류)

- 발견: `DennisHuang648/VoxCPM2-GGUF` (VoxCPM2-BaseLM-Q8_0 + VoxCPM2-Acoustic-F16, ~3.3 GB)
- 스택: `github.com/ggml-org/llama.cpp-omni` (voxcpm2-cli)
- 상태: `integration/voxcpm_worker.py` 골격 작성됨. 모델 다운로드 미진행.
- 보류 이유: HF Space (Option A)로 충분, 로컬 GPU 없는 인텔맥에서 GGUF 인퍼런스 시간 검증 필요

## 11. 폴백 체인

`VOXCPM_ENGINE="voxcpm-api"`일 때:
```
voxcpm-api (HF Space) → piper (ko_KR-kss-medium) → say (macOS TTS)
```

## 12. 권장

- 즉시 사용 가능: `python integration/voxcpm_api_worker.py` + `VOXCPM_ENGINE=voxcpm-api TTS_ENGINE=voxcpm-api python tts_local.py`
- 음질 개선: -41 dB 참조 클립을 -20~-10 dB로 재녹음 후 재테스트
- 한국어 전사본의 일부 (약간의 구어체/감탄사 포함)이 클론 자연스러움에 도움 — 현 전사 사용 OK
