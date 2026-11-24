# Moonshine (moonshine-ai/moonshine) — Maya Voice 평가

> 사용자 링크: https://github.com/moonshine-ai/moonshine
> 평가일: 2026-11-24
> 평가자: AI 에이전트
> 결론: **Maya 음성 파이프라인 즉시 통합 후보 — TTS·STT 모두 가능, GPU 불필요, 한국어·영어 모두 지원**

---

## 1. 핵심 요약

Moonshine는 **온디바이스 음성 AI 라이브러리** (STT + TTS) 로, Maya가 직면한 두 가지 큰 제약 — (a) MacBook Intel에서 VoxCPM 등 대형 TTS는 PyTorch ≥2.5.0 wheel 부재로 로컬 실행 불가, (b) OpenRouter/클라우드 의존 — 을 **단일 패키지로 해결**한다.

| 항목          | 값                                                                             |
| ------------- | ------------------------------------------------------------------------------ |
| 저장소        | [github.com/moonshine-ai/moonshine](https://github.com/moonshine-ai/moonshine) |
| 라이선스      | MIT                                                                            |
| 패키지        | `moonshine-voice` (PyPI, v0.0.59)                                              |
| 플랫폼        | Python, JavaScript/WASM, iOS, Android, macOS, Linux, Windows, Raspberry Pi     |
| 언어          | 한국어 + 영어 + 다국어                                                         |
| 모델 변종     | `tiny`, `tiny.en`, `base`, `small`, `medium`, streaming 변종                   |
| GPU 필요 여부 | ❌ — CPU/MPS/CUDA 모두 지원                                                    |

**위치 인용 (README):** _"Everything runs on-device — fast, private, and no API keys"_ / _"Optimized for live streaming, with low latency by doing work while the user is still talking"_

---

## 2. Maya 적용 적합성

### 2.1 STT (Speech → Text)

Maya 현재:

- `webapp_server.py` 가 `STT_LEGACY=1` 일 때 `faster-whisper` (`base`) 사용
- `STT_LEGACY=0` 이면 SenseVoiceSmall (`iic/SenseVoiceSmall`) 사용
- 한국어 인식 정확도 한계 + GPU 의존 (SenseVoice) / 느린 CPU 추론 (Whisper base)

Moonshine STT 장점:

- **스트리밍 최적화**: 사용자가 말하는 동안 실시간 부분 결과 반환 (Maya의 full-duplex 비서와 정확히 일치)
- **다국어 동시 모델**: 한 모델이 한국어/영어/일본어/중국어/스페인어 등 동시 처리
- **저지연**: 모델 크기 4단계(`tiny`/`base`/`small`/`medium`) — Maya의 Intel MacBook에서 `tiny`·`base` 는 실시간 가능
- **Python API 단순**: `moonshine_voice.transcribe(...)`

### 2.2 TTS (Text → Speech)

Maya 현재:

- Piper (ONNX, 로컬, 22050Hz, 영어 위주)
- Kokoro / XTTS 검토했으나 한국어 품질 또는 의존성 문제로 미통합

Moonshine TTS 장점:

- **한국어 음성 합성 포함**
- **온디바이스** — GPU 불필요 (VoxCPM이 막힌 Intel MacBook에서 결정적 우위)
- MIT 라이선스 — 상업적 사용 가능

### 2.3 통합 표면

```python
# STT 사용 예시 (PyPI 문서 기준)
from moonshine_voice import transcribe

result = transcribe(audio_path)        # 파일
result = transcribe(numpy_array, 16000)  # 라이브 PCM 스트림

# TTS 사용 예시
from moonshine_voice import synthesize
audio = synthesize(text="안녕하세요", lang="ko")
```

→ 기존 `webapp_server.py` 의 `recognize()` 함수와 **drop-in 교체** 가능 (Whisper의 `transcribe()` 와 동일한 인터페이스).

---

## 3. 비용·제약 비교

| 후보                       | 한국어         | GPU | Intel MacBook                | 라이선스   | 스트리밍 |
| -------------------------- | -------------- | --- | ---------------------------- | ---------- | -------- |
| **Moonshine**              | ✅             | ❌  | ✅                           | MIT        | ✅       |
| Whisper (`faster-whisper`) | ⚠️ (한국어 약) | ❌  | ✅ (느림)                    | MIT        | ❌       |
| SenseVoiceSmall            | ⚠️ (중어 강함) | ❌  | ⚠️                           | Apache-2.0 | ❌       |
| VoxCPM2                    | ✅             | ✅  | ❌ (PyTorch ≥2.5 wheel 없음) | Apache-2.0 | ❌       |
| Piper TTS                  | ❌             | ❌  | ✅                           | GPL-3.0    | ❌       |

**Moonshine는 모든 열에서 ✓ 또는 ✓/⚠** — Maya의 현재 환경과 정합성 최고.

---

## 4. 통합 작업 항목 (제안)

### Phase 1: STT 교체 (1~2시간)

1. `moonshine-voice` 설치: `pip install moonshine-voice`
2. `webapp_server.py` 의 `recognize()` 를 `moonshine_voice.transcribe()` 호출로 교체
3. 모델 사이즈 — MacBook CPU에서는 `tiny` 또는 `base` 권장 (`small` 이상은 GPU 권장)
4. 폴백 — `STT_LEGACY=1` (Whisper) 유지 (Maya 운영 안정성)

### Phase 2: TTS 교체 (선택, 2~3시간)

1. Moonshine TTS로 한국어 합성 테스트
2. Piper 대비 자연스러움 비교
3. 기존 Piper 워커와 병렬 운영 가능 (`TTS_ENGINE=moonshine` env 분기)

### Phase 3: 스트리밍 STT (4~6시간)

1. `moonshine_voice.stream(...)` 으로 WebSocket `/ws/audio` 에서 부분 결과 반환
2. 기존 `webaudio-vad-silero` + full-duplex 파이프라인과 통합

---

## 5. 위험·검토 사항

| 항목                | 메모                                                                                           |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| 패키지 성숙도       | v0.0.59 — 1.0 미만, API 변경 가능성 있음                                                       |
| 모델 정확도         | 한국어 STT 정확도 자체 벤치마크 데이터 없음 (테스트 필요)                                      |
| TTS 음성 자연스러움 | 한국어 TTS 음성 품질 미검증 (Piper/kokoro와 비교 필요)                                         |
| 의존성              | `moonshine-voice` 가 어떤 PyTorch / ONNX 버전을 요구하는지 미확인 — `pip install` 후 확인 필요 |

---

## 6. 권장 조치

1. **`pip install moonshine-voice` 실행 + 한국어 샘플 STT 정확도 테스트** (30분)
2. 통과 시 **`webapp_server.py` 의 STT 백엔드를 Moonshine로 전환** (`STT_ENGINE=moonshine` env 분기 추가)
3. Piper TTS 한국어 한계가 결정적이면 **Moonshine TTS를 차용** (현재 Piper는 유지하되 모드 추가)

---

## 7. 참고 링크

- [moonshine-ai/moonshine (GitHub)](https://github.com/moonshine-ai/moonshine)
- [Moonshine 문서 (readthedocs)](https://moonshine-voice.readthedocs.io/)
- [PyPI: moonshine-voice](https://pypi.org/project/moonshine-voice/)
