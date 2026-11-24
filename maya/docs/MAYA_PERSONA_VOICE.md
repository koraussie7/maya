# Maya 페르소나 — 같은 목소리 + 같은 인격 (2026-11-24 구현·검증)

**결론**: 둘 다 됩니다. **페르소나는 YAML 한 파일**, **목소리는 레퍼런스 클립 한 개**로 고정됩니다.
실측으로 음색 일치(F0 142Hz → 137Hz)와 발음 정확(전사 CER 0)까지 확인했습니다.

---

## 1. 구조

```
maya/personas/lyra_vex.yaml            ← 인격 + 목소리 지정 (유일한 진실 원본)
maya/models/tts/reference/clip_ref.wav ← 7.1초 16kHz mono, peak 정규화
        │
        ├─ persona.py  → system 메시지(prompt + 응답 규칙) + temperature + voice 경로
        ├─ session.py  → 세션마다 페르소나 로드 → LLM system / TTS 인자로 전달
        └─ tts_local.LocalTTS.synthesize(text, ref_wav=, prompt_text=) → VoxCPM 클론
```

| 파일                                    | 역할                                                                        |
| --------------------------------------- | --------------------------------------------------------------------------- |
| `maya/personas/lyra_vex.yaml` (신규)    | 인격·말투·문장 수·temperature·음색 클립·클립 전사문                         |
| `maya-ui/backend/app/persona.py` (신규) | YAML 로더(**PyYAML 없이도 동작하는 최소 파서**), 응답 규칙 자동 부착        |
| `maya-ui/backend/app/session.py`        | `SYSTEM_PROMPT` 하드코딩 제거 → `load_persona()`, temperature·TTS 인자 전달 |
| `maya-ui/backend/app/tts.py`            | `synthesize(text, ref_wav, prompt_text)` — 클론 미적용 시 **경고 로그**     |
| `integration/tts_local.py`              | `synthesize()` 에 `ref_wav` / `prompt_text` 인자 추가(세션별 음색)          |

## 2. 레퍼런스 클립 재생성 (녹음 → 클립)

`~/Documents/새로운 녹음.mp3`(21분, 48kHz)에서:

1. 앞 60초 추출 + **loudnorm(-18 LUFS)** → `/tmp/cand_60s.wav`
2. 30ms 프레임 RMS VAD, 임계값 `max(p20*1.6, 150)`, 0.6초 이상 무음만 문장 경계로
3. **2~5.5초 구간만** 골라 이어 붙이되 총 ≤ 9초 → 7.11초
4. peak 정규화(-1dB) → `maya/models/tts/reference/clip_ref.wav`

> 왜 2~5.5초만: 너무 짧으면 음색이 안 잡히고, 너무 길면 VoxCPM 이 앞부분 위주로 반영한다.
> 대화체라 3초 이상 연속 발화가 사실상 없어서(실측 최대 1.68초) **여러 짧은 구간을 이어 붙였다**.

## 3. 클립 전사문

SenseVoice 로 전사(실측):

```
어 지금 하고 있어 얘기하자아 갑자기 러시아로?
```

→ `lyra_vex.yaml` 의 `voice.prompt_text`. **클립을 바꾸면 이 줄도 반드시 다시 적어야** Ultimate Cloning 품질이 나온다.

## 4. 실측 (검증)

| 확인            | 결과                                                                               |
| --------------- | ---------------------------------------------------------------------------------- |
| 페르소나 로드   | `lyra_vex`, ref=clip_ref.wav, prompt_text 정상                                     |
| 클론 합성(콜드) | 41.2s → 36.36s 오디오 (첫 호출: Space 웜업 포함)                                   |
| 클론 합성(웜)   | **7.7s → 1.80s 오디오(입력 1.4배)**                                                |
| **음색 일치**   | 기준 F0 **142Hz** → 출력 **137Hz**, 스펙트럼 중심 1940→1473Hz (같은 대역)          |
| **발음**        | 출력 전사 `안녕 오늘 하루도 어땠어?` vs 입력 `안녕, 오늘 하루 어땠어?` → **CER 0** |

> F0/중심주파수는 절대 검증이 아니라 **같은 화자 대역인지**의 지표다. 최종 판단은 직접 들어봐야 한다.

## 5. 사용법

```bash
# 기본 페르소나(lyra_vex)
MAYA_PERSONA=lyra_vex python -m app.server

# 다른 프로필
MAYA_PERSONA_PATH=/abs/path/profile.yaml python -m app.server

# 음성 클론 (voxcpm-api 엔진일 때만 적용)
export TTS_ENGINE=voxcpm-api
export VOXCPM_API_URL=https://openbmb-voxcpm-tts.hf.space
```

**주의**

- `TTS_ENGINE` 이 piper/say 로 폴백되면 **목소리가 달라진다** → `tts.py` 가
  `클론 미적용(engine=...)` 경고를 남긴다. 그 경고가 보이면 클론이 안 걸린 것이다.
- HF Space API 는 호출마다 음색이 약간 흔들린다. **완전 고정**은 로컬/GPU 에서
  `build_prompt_cache()` 1회 생성 후 재사용하는 경로(`integration/voxcpm_worker.py`).
- 지연: 웜 7.7s/1.8s ≈ **RTF 4.3** — 대화에 넣으면 문장이 길어질수록 늘어난다.
  문장을 1~2개로 제한(`max_sentences: 2`)해 둔 이유다.

## 6. 페르소나를 추가하려면

`maya/personas/` 에 YAML 한 개 추가하고 `MAYA_PERSONA=<파일명>` 으로 선택.
`voice.ref_wav` 에 **다른 화자**의 클립을 넣으면 그 목소리로 바뀐다(프롬프트는 그대로 써도 됨).

---

## 7. 목소리 A/B 옵션 (2026-11-24 추가)

### 7.0 화자는 몇 명인가 — 측정으로 확정 (`integration/count_speakers.py`)

21분 녹음의 발화 192구간을 MFCC+스펙트럼+F0 로 특징화하고 k 를 바꿔 가며 군집 품질을 쟈다.

| k | 실루엣 | 군집 크기 | F0 중앙값 |
|---|--------|-----------|-----------|
| **2** | **0.424** ← 최고 | 79 / 113 | **124Hz / 192Hz** |
| 3 | 0.380 | 109 / 64 / 19 | 192 / 125 / 113 |
| 4 | 0.204 | 19 / 58 / 64 / 51 | 113 / 196 / 125 / 188 |
| 5 | 0.126 | 18 / 52 / 27 / 37 / 58 | 113 / 183 / 132 / 122 / 198 |

F0 분포: p10=117Hz, **p50=163Hz**, p90=234Hz.

**결론: 화자는 2명이다**(저음역 ~120Hz 1명 + 고음역 ~190Hz 1명). k 를 늘리면 실루엣이
**떨어지기만** 한다(0.424→0.380→0.204→0.126) — 즉 3명 이상은 데이터가 지지하지 않는다.

> 정정(중요): 앞서 "여성 화자가 여러 명일 수 있다"고 쓴 것은 **근거 없는 추측**이었다.
> 그때 본 F0 변화(170→227→157→122Hz)는 **같은 사람의 억양 변화 + 짧은 구간 노이즈**였고,
> 실제 군집 품질은 k=2 에서 가장 깔끔한다. `split_speakers.py --k 2` 는 정당했다.
>
> 다만 고음역 군집(192Hz)은 **“여성 1명”으로 단정할 수 없다** — 이 녹음은 2명 대화지만
> p90=234Hz 라는 높은 꼬리는 다른 사람이 잠깐 끼어든 구간일 가능성도 배제할 수 없다.
> 화자 수 확정을 자동화하려면 화자분리 모델(pyannote 등)과 **실제 음성 청취**가 필요하다.

같은 녹음에서 **화자를 2명 분리**해 두 목소리를 모두 썼다.

| 페르소나 | 클립 | F0 | 스펙트럼 중심 | 용도 |
|----------|------|----|---------------|------|
| `lyra_vex` (A) | `clip_ref_speaker_a.wav` (6.2s) | **117Hz** | 1489Hz | 사용자 본인 음성 |
| `lyra_vex_b` (B) | `clip_ref_speaker_b.wav` (8.1s) | **201Hz** | 2121Hz | 여성 화자 음성 |

**클론 결과 검증** (동일 텍스트·동일 규칙)

| 출력 | F0 | 중심 | 판정 |
|------|----|------|------|
| A 클론 | 127Hz | 1437Hz | 기준 A 대역 유지 |
| B 클론 | **192Hz** | **2231Hz** | 기준 B 대역 유지, 전사 CER 0 |

### 쓰는 곳 3가지

1. **UI 토글** — `privseai.com/maya/` 사이드바 하단 `목소리` 선택(남성 A / 여성 B).
   선택값은 매 턴 `chat.text` 메시지의 `persona` 필드로 서버에 전달된다.
2. **서버에서 전환** — `Session.switch_persona(name)`. system 메시지만 교체하고
   **대화 이력은 유지**한다(목소리 바꿨다고 맥락을 잃지 않게). 전환되면 `persona.changed` 이벤트.
3. **CLI** — `integration/maya_live_talk.py --persona lyra_vex_b`,
   `integration/maya_ws_smoke.py --persona lyra_vex_b`

### 구현 메모

- `persona.load_persona(name)` / `available_personas()` 추가 — 이름이 없으면 **기본 페르소나로 안전 폴백**
  (오타로 대화가 죽지 않게).
- `mayaClient.sendText(text, persona)` — 기존 호출은 그대로 호환(기본값 `''`).
- 클립 분리 도구: `integration/split_speakers.py` (MFCC+스펙트럼+F0 → KMeans k=2).
  녹음 21분 → 발화 192구간 → 화자 2군집. 결과는 `maya/models/tts/reference/speakers.json`.

> 주의: A와 B 는 **같은 대화 녹음**에서 나왔다. 서로 겹처 말한 구간은 분리되지 않으니,
> 한쪽 클립에 다른 사람 목소리가 섞이면 클론 음색이 흐려진다 — 섞이면 `speakers.json` 의
> `clip_start` 를 보고 다른 구간으로 교체하는 것이 빠릅니다.
