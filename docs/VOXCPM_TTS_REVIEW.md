# VoxCPM (OpenBMB/VoxCPM2) 분석 — 우리 TTS 경로에 쓸 수 있는가

**날짜**: 2026-11-24
**분석 대상**: [OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM) `main` (2026-11-09 push, 42.9k★, **Apache-2.0**)
**결론 요약**: 라이선스·한국어 지원·복제 방식 모두 **우리가 원하던 "녹음 파일로 목소리 만들기"** 에
정확히 맞는다. 다만 **2B 모델 + torch≥2.5 + CUDA 기준 튜닝**이라 이 맥(Intel i9, torch 2.2.2)에서는
실시간이 어렵다. → **클라우드 GPU/Apple Silicon 에서 엔진으로 채택, Intel 맥에서는 배치/오프라인 복제용**.
본 문서의 수치는 전부 저장소 코드·README·HF 메타데이터에서 확인한 것이고, 실측은 "측정 도구"까지만
이번에 만들었다(모델 다운로드·추론은 5절의 절차로 별도 실행).

---

## 1. 무엇인가 (README/code 확인)

- **토크나이저 없는 TTS**: 이산 토큰 대신 연속 잠재 표현을 **diffusion-autoregressive** 로 생성.
  파이프라인은 `LocEnc → TSLM → RALM → LocDiT` (README의 모델 그림 설명).
- 백본은 **MiniCPM-4** (`src/voxcpm/modules/minicpm4/model.py`, `VoxCPM2Model.__init__` 에서
  `self.base_lm = MiniCPMModel(config.lm_config)`).
- **VoxCPM2**: 2B, 30개 언어(**한국어 포함**), 48kHz 출력, 음성 디자인, 복제(Controllable/Ultimate).
- 라이선스: **코드·가중치 모두 Apache-2.0** (pyproject `license = "Apache-2.0"`, HF `cardData.license`).
- 릴리스 계보: VoxCPM-0.5B(2025.09) → VoxCPM1.5(2025.12) → **VoxCPM2(2026.04)** →
  현재 main 의 `model/voxcpm2.py` 가 최신(~1.28k 줄).

## 2. 우리 문제에 직결되는 기능 3개

`src/voxcpm/core.py` 의 `VoxCPM.generate()` 시그니처에서 확인:

| 모드                     | 인자                                                        | 우리 사용처                                   |
| ------------------------ | ----------------------------------------------------------- | --------------------------------------------- |
| **Voice Design**         | `text="(설명)본문"`                                         | 목소리를 글로 디자인 (녹음 없이)              |
| **Controllable Cloning** | `reference_wav_path=`                                       | ** 마야 녹음 클립 → 목소리 복제** ← 우리 목표 |
| **Ultimate Cloning**     | `prompt_wav_path=` + `prompt_text=` (+`reference_wav_path`) | 클립 + 전사문으로 음색·리듬·억양까지 재현     |

- `build_prompt_cache(prompt_text=, prompt_wav_path=, reference_wav_path=)` 로 **레퍼런스 인코딩을
  1회만** 하고 매 문장 재사용 가능 (`voxcpm2.py:695`). 우리는 문장 단위 스트리밍이므로 필수.
- `reference_wav_path` 는 **VoxCPM2 전용** — 코드에서 1.5/0.5B 에 넘기면 `ValueError`
  (`core.py:267`). 즉 복제(구조적으로 분리된 ref)를 원하면 **2B** 를 써야 한다.
- 레퍼런스 입력은 **16kHz 모노로 리샘플**해서 인코딩한다(`_encode_wav` → `librosa.load(sr=16k)`,
  AudioVAE `sample_rate: int = 16000`). 출력만 48kHz → **우리 `clip_ref.wav`(16k mono, 10초)가 그대로 입력**이다.
- `generate_streaming()` / `tts_model.generate_with_prompt_cache_streaming()` 존재.
  대화 중 첫 소리 지연을 줄이는 경로.

## 3. 성능 표 (README, 한국어 관련만 발췌)

**CV3-eval WER/CER(%)**

| 모델            | ko       | ja   | zh   | en   |
| --------------- | -------- | ---- | ---- | ---- |
| CosyVoice2      | 19.7     | 9.13 | 4.08 | 6.32 |
| CosyVoice3-1.5B | **5.69** | 7.57 | 3.91 | 4.99 |
| Fish Audio S2   | **2.76** | 3.96 | 2.65 | 2.43 |
| **VoxCPM2**     | **5.69** | 5.96 | 3.65 | 5.00 |

**Minimax-MLS (한국어)**: WER VoxCPM2 **1.962** (Minimax 1.747, ElevenLabs 1.865, FishAudio 1.180),
SIM **83.3** (전 모델 중 최상위).

해석: **한국어 인식률(WER)은 상위권이지만 1위는 아님. 화자 유사도(SIM)는 최고 수준** →
"내 목소리처럼 들리는가" 가 목표인 우리 용도에서는 강점이 정확히 이 지표다.
반대로 **한국어 발음 정확도 최고점**을 원하면 FishAudio S2 계열이 더 앞선다(비공개/유료 여부 확인 필요).

**속도/자원** (README 표)

|                                        | VoxCPM2                     | VoxCPM1.5         | VoxCPM-0.5B       |
| -------------------------------------- | --------------------------- | ----------------- | ----------------- |
| 파라미터                               | 2B                          | 0.6B              | 0.5B              |
| 출력 SR                                | 48kHz                       | 44.1kHz           | 16kHz             |
| RTF (RTX 4090)                         | ~0.30                       | ~0.15             | ~0.17             |
| VRAM                                   | ~8GB                        | ~6GB              | ~5GB              |
| **RTF (M4 Pro / Metal, llama.cpp Q8)** | **~1.76**                   | —                 | —                 |
| 복제 방식                              | isolated ref + continuation | continuation only | continuation only |

> ⚠️ **M4 Pro 에서 RTF 1.76 = 실시간 1.76배 느림**. 즉 Apple Silicon 에서도 "실시간 대화"는 아니다.
> 다만 이건 llama.cpp 백엔드 수치이고, PyTorch MPS 에서는 별도 측정 필요(5절).

## 4. 이 맥에서 돌릴 수 있는가 (실측 환경)

| 항목                    | 실측값                                                      | 판정                                                              |
| ----------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------- |
| CPU                     | **Intel Core i9-9980HK** (x86_64)                           | ❌ Apple Silicon 아님 → MPS 가 Intel GPU 기반                     |
| RAM                     | 32GB                                                        | ⚠️ 2B 모델(fp16 ~5GB) + AudioVAE + 임베딩 모델 동시 상주하면 빠듯 |
| torch (파이프라인 venv) | **2.2.2**                                                   | ❌ VoxCPM `pyproject.toml` 요구 `torch>=2.5.0`                                     |
| 의존성 사전 점검        | VoxCPM 은 코드 상 `einops` + 최상위 `gradio` import 를 요구. 우리 venv 는 둘 다 없어 `ModuleNotFoundError` 로 죽었다(이번에 `einops` 만 설치) | 별도 venv 필수 근거 |
| VoxCPM2 가중치          | **4.96GB** (`model.safetensors` 4.58 + `audiovae.pth` 0.38) | 다운로드 가능                                                     |
| VoxCPM1.5 가중치        | 1.95GB                                                      | 다운로드 가능                                                     |
| VoxCPM-0.5B 가중치      | 1.61GB                                                      | 다운로드 가능 (16kHz 출력)                                        |

**결론**: 이 저장소의 TTS 경로(`integration/tts_local.py`, torch 2.2.2 고정)에 **같은 venv 로는 못 넣는다**.
→ 그래서 `integration/voxcpm_worker.py` 를 **별도 venv 서브프로세스**로 설계했다(6절).

## 5. 설치·측정 절차 (별도 venv, 파이프라인 무영향)

```bash
# Intel 맥 CPU 전용 torch (2.5.1 이 마지막 x86_64 macOS 휠)
python3 -m venv /tmp/voxcpm-venv
/tmp/voxcpm-venv/bin/pip install --upgrade pip
/tmp/voxcpm-venv/bin/pip install torch==2.5.1 torchaudio==2.5.1        # x86_64 macOS 휠
/tmp/voxcpm-venv/bin/pip install voxcpm                              # torch>=2.5 요구 충족

# 모델 (약 5GB) — HF 직접 다운로드
/tmp/voxcpm-venv/bin/python -c "from huggingface_hub import snapshot_download; \
 snapshot_download('openbmb/VoxCPM2', local_dir='./pretrained_models/VoxCPM2')"

# 측정 (워커만 별도 venv, 측정 스크립트는 메인 venv)
.venv-voicemem/bin/python integration/voxcpm_tts_bench.py \
  --worker-python /tmp/voxcpm-venv/bin/python \
  --model ./pretrained_models/VoxCPM2 \
  --reference /Users/brianyeon/CodeGPT/maya/models/tts/reference/clip_ref.wav \
  --prompt-text "<clip_ref.wav 10초 구간의 전사문>" \
  --device cpu --repeat --asr --baseline-piper
```

`--asr` 은 합성 결과를 우리 `SenseVoice` 로 다시 전사해 CER 를 낸다(발음 검증).
현재 기준선(실측, 2026-11-24, `integration/voxcpm_tts_bench.json`):

| 엔진 | 문장 | 합성 시간 | 오디오 | RTF |
|------|------|-----------|--------|-----|
| say 폴백 | "안녕하세요, 오늘 기분이 어떠세요?" | 2.86s | 2.47s | RTF 1.16 |
| say 폴백 | "제가 도와드릴 일이 있을까요?" | 2.77s | 1.88s | RTF 1.47 |
| say 폴백 | "지금 서울의 날씨는 맑고 기온은 영상 십 도입니다." | 2.77s | 3.45s | RTF 0.80 |

> 위 CER 은 숫자 표기 차이("십 도"→"10 도")가 섞여 과대평가될 수 있는데,
> SenseVoice 전사가 거의 그대로 나왔다. 즉 **현재 say 는 발음은 합격, 목소리 복제가 0** 이다.
> VoxCPM 을 붙이는 이유는 발음 개선이 아니라 **목소리(SIM)** 다(3절 Minimax-MLS SIM 83.3).

> 주의: 이 맥의 piper 는 `piper` 파이썬 모듈이 없어 **say 폴백**으로 측정됐다
> (`integration/tts_local.py` 로그: `piper load failed: ModuleNotFoundError: No module named 'piper'`).
> 즉 위 기준선은 "piper 가 아닌 say" 수치다.

## 6. 어떻게 붙이는가 — `voxcpm_worker.py`

`integration/piper_worker.py` 와 **동일한 IPC**(5바이트 헤더 + JSON/PCM)를 쓴다. 그래서
`tts_local.LocalTTS` 에 엔진을 하나 더 추가하는 형태로 끝난다(파이프라인 수정 최소).

```
LocalTTS.synthesize(text)  ──(IPC)──▶  voxcpm_worker.py  ──▶  VoxCPM2 (별도 venv)
        ▲                                   │
        └────────── Int16 16kHz PCM ◀───────┘  (48kHz 출력을 워커에서 16k 로 리샘플)
```

워커의 설계 결정(코드에 주석으로 남김):

1. **레퍼런스 캐시 1회**: `build_prompt_cache(reference_wav_path=...)` 를 준비 단계에서 만들고
   문장마다 재사용 → 매 요청 인코딩 제거.
2. **fp32 강제(로드 구간만)**: Intel Mac MPS 는 bf16 지원이 제한적이라 dtype 선택
   (`pick_runtime_dtype`)을 fp32 로 유도하고, 로드 직후 원복.
3. **`optimize=False`**: `torch.compile` 워밍업이 첫 호출을 수십 초 늘린다 — 대화 경로에선 손해.
4. **48k → 16k 리샘플을 워커에서**: 우리 파이프라인 계약은 항상 Int16 mono 16kHz
   (`tts_local.LocalTTS.synthesize` docstring). ffmpeg 있으면 ffmpeg, 없으면 numpy 보간.

## 7. 판단: 어디에 쓰고, 어디에 쓰지 말 것

| 용도                              | 판단                | 근거                                                                                |
| --------------------------------- | ------------------- | ----------------------------------------------------------------------------------- |
| **녹음 파일 → 마야 목소리(복제)** | ✅ **최적 후보**    | isolated ref 복제(V2 전용), 한국어 SIM 최상위, Apache-2.0, 16k 레퍼런스 입력 그대로 |
| 실시간 대화 TTS (첫 소리 <300ms)  | ❌ 이 맥에서는 불가 | 2B + RTF 0.3(4090)/1.76(M4) → GPU 필요. 현재 우리는 say 276ms(15-6 기록)            |
| 문장 단위 스트리밍                | ⚠️ 구조는 지원      | `generate_streaming` 존재하나 첫 청크 지연 실측 필요(5절)                           |
| piper 대체(즉시)                  | ❌                  | piper 는 CPU 실시간·~60MB. VoxCPM2 는 5GB·GPU급                                     |

**권장 경로**

1. **오프라인 복제 → 참조 음성 제작**: 녹음(21분)에서 좋은 구간 10~30초 + 전사문을 골라
   VoxCPM2 Ultimate Cloning 으로 "마야 목소리" 샘플/데이터셋을 만든다(배치, GPU 1회).
2. **실시간 경로는 지금 그대로**(piper/say) 유지. GPU 서버가 생기면 vLLM-Omni 의
   OpenAI 호환 `/v1/audio/speech` 로 붙이는 게 가장 깔끔하다(README 확인: `vllm serve --omni`).
3. 복제 품질을 더 올려야 하면 VoxCPM2 **LoRA 파인튜닝**(`conf/voxcpm_v2/voxcpm_finetune_lora.yaml`,
   `train_manifest` = 오디오+전사 jsonl) — CUSTOM_VOICE 문서 5절의 "piper 학습 필요" 문제를 대체.

## 8. 미확인 사항 (정직하게)

- **Intel 맥 PyTorch MPS 에서의 실제 RTF**: 이번에 측정하지 않았다(모델 5GB 다운로드 + 별도 venv 필요).
  README/벤치마크에 이 조합의 수치가 없어 **추정 금지**. 5절 절차로 실측해야 한다.
- **한국어 첫 청크 지연**: 스트리밍 API 는 확인했지만 실측 없음.
- **clip_ref.wav 전사문 정확도**: `docs/CUSTOM_VOICE_INTEGRATION_ISSUES.md` 2절은 첫 5초 구간만
  전사했다. 10초 클립 전체 전사문을 다시 만들어 `--prompt-text` 로 넣어야 Ultimate Cloning 품질이 나온다.
- **AudioVAE 입력 16kHz 가정**: 코드상 `assert sample_rate == self.sample_rate` 로 48k 원본을
  직접 못 넣는다 → 워커가 ffmpeg 로 16k 변환 후 전달(구현 완료).

## 9. 이번에 추가한 파일

| 파일                                | 역할                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------- |
| `integration/voxcpm_worker.py`      | VoxCPM2 를 piper_worker 와 같은 IPC 로 감싼 서브프로세스(별도 venv)       |
| `integration/voxcpm_tts_bench.py`   | 워커/piper 기준선 지연·RTF·CER 측정 → `integration/voxcpm_tts_bench.json` |
| `integration/voxcpm_tts_bench.json` | 이번 기준선 실행 결과(say 폴백 CER 포함)                                  |
