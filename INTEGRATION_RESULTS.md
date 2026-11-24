# 14개 레포 분석 → 통합 실행 결과

**날짜**: 2026-11-23
**대상**: `/Users/brianyeon/CodeGPT/maya` (aivoice.privseai.com)
**작업 공간**: `/Users/brianyeon/CodeGPT/maya-2/integration/`
**선행 문서**: `maya/REPO_ANALYSIS_14_SUMMARY.md` (분석 결산)

이 문서는 분석 결산 이후 **실제로 이식하고 측정한 결과**다.
분석 문서의 예측 중 **틀린 것**도 그대로 적는다.

---

## 0. 한 장 요약

| #   | 이식 대상              | 출처                                                                                                          | 상태             | 실측                             |
| --- | ---------------------- | ------------------------------------------------------------------------------------------------------------- | ---------------- | -------------------------------- |
| 1   | NAMO 한국어 턴 판정    | [NAMO](https://github.com/videosdk-live/NAMO-Turn-Detector-v1)                                                | ✅ **적용 완료** | **54/54 (100%)**, 26ms           |
| 2   | 문장 단위 스트리밍 TTS | [Lumina](https://github.com/Cortantse/Lumina) / [edge-s2s](https://github.com/Posime/edge-s2s-voice-pipeline) | ✅ **적용 완료** | ⚠️ **효과 미미** (§2-3)          |
| 3   | VAD hangover           | [simulforge](https://github.com/huzjie/simulforge)                                                            | ✅ **적용 완료** | 900→600ms + NAMO 재판정          |
| 4   | SenseVoice 한국어 STT  | [SenseVoice](https://github.com/QwenAudio/SenseVoice)                                                         | ✅ 모델 확보     | sherpa-onnx 실측 진행            |
| 5   | 에코 제거 (AEC)        | [s2s.cpp](https://github.com/ServeurpersoCom/s2s.cpp)                                                         | ⏸️ 미적용        | 브라우저 `echoCancellation` 유지 |
| 6   | 판단 프롬프트 4종      | [convoai-jev-vad](https://github.com/AgoraIO-Community/convoai-jev-vad)                                       | ⏸️ 불필요        | NAMO로 대체됨                    |

---

## 1. ✅ NAMO 턴 판정 — 적용 완료

### 1-1. 검증: 54문장 (분석 문서 §7의 표본 한계 보완)

분석 문서는 "9/9는 10문장 표본" 이라고 정직하게 적었다. 54문장으로 확장했다.

```
정확도 54/54 (100.0%)
카테고리별:
  문장       15/15 (100%)   ← 완결 문장
  백채널      12/12 (100%)   ← "네", "어", "그쵸", "뭐랄까"
  미완결      15/15 (100%)   ← "그러니까 그게", "우선은 말이죠"
  연결어미     12/12 (100%)   ← "배송이 늦어서", "결제하고", "그래서"
지연 평균 25.9ms / p95 43.0ms / 최대 60.4ms
```

**가장 어려운 케이스가 100%** 라는 점이 중요하다.
연결어미("~서", "~는데", "~고")로 끝나는 문장은 한국어 턴 판정의 고전적 난제다.
NAMO는 이를 모두 NOT(계속 말하는 중)으로 정확히 잡았다.

> ⚠️ 한계: 모두 **합성음(`say -v Yuna`) 기반 텍스트**다. 실제 마이크 + Whisper
> 전사에는 문장부호가 없고 오인식이 섞인다. 실사용 로그 수집이 여전히 필요하다.

### 1-2. 이식 방식

| 항목   | 내용                                                                           |
| ------ | ------------------------------------------------------------------------------ |
| 파일   | `tools/turn_detector.py` (→ `maya-2/integration/turn_detector.py` 심볼릭 링크) |
| 모델   | HF 캐시에 이미 존재 (650MB) — 재다운로드 없음                                  |
| 로드   | 지연 로드 + 전역 싱글턴. 서버 기동 시 1회 (~0.4s)                              |
| 폴백   | 로드 실패 시 `available=False` → 기존 시간 기반 판정 유지                      |
| 임계값 | 0.9 (confidence 1.000 과신 경향 대응)                                          |

### 1-3. 턴 엔진 3단 판정 (`tools/pcm_turn_engine.py`)

```
         발화 감지 (RMS ≥ 250)
                    │
                    ▼
         무음 600ms 도달? ──── 아니오 ──→ 계속 수집
                    │ 예
                    ▼
         증분 전사 (faster-whisper)
                    │
                    ▼
         NAMO 판정 ── END(≥0.9) ──→ ✅ 턴 확정 (전사 텍스트 재사용)
                    │
                    └─ NOT ───────→ 🔄 계속 듣기 (버퍼 유지)
                                          │
                                          └─ max_turn_ms(8s) → 강제 종료
```

**두 가지 개선이 동시에 일어난다:**

1. 백채널("네")에 턴이 닫히지 않는다
2. NAMO 판정 때 전사한 텍스트를 **재사용** → 2차 전사 생략 (0.3~1.0s 절약)

검증 결과 (`integration/test_turn_engine.py`):

```
=== 백채널 '네' ===          → 미확정 (계속 듣는 중)     ✅
=== 완결 '안녕하세요.' ===    → 확정 reason='namo 0.98'   ✅
=== 미완결 '그런데 말이죠' === → 미확정 (계속 듣는 중)     ✅
=== 전사 공백 ===            → 확정 reason='silence 653ms' ✅ (폴백)
```

### 1-4. 부작용 (반드시 인지할 것)

| 항목         | 변화                                                        |
| ------------ | ----------------------------------------------------------- |
| 턴 확정 지연 | **900ms → 600ms + 전사/판정 시간**                          |
| 최악 지연    | 전사(tiny ~200ms / small ~800ms) + NAMO 26ms ≈ **0.8~1.4s** |
| CPU          | 턴마다 전사 1회 추가 (판정용)                               |
| 안전장치     | `max_turn_ms` 8초에 걸리면 **그 시점 오디오를 그대로 확정** |

> 🔴 **max_turn_ms 함정**: 계속 말하는 중에 8초가 지나면 문장 중간에서 잘린다.
> 대화 리듬상 8초는 짧은 편이다. 실사용에서 잘림이 보이면 12~15초로 올릴 것.

---

## 2. ⚠️ 문장 단위 스트리밍 TTS — 적용했지만 효과가 없었다

분석 문서는 `13초 → 체감 4초` 를 예상했다. **실측은 그렇지 않았다.**

### 2-1. 실측 (실제 `say` + `ffmpeg`, 56자 5문장)

```
[BEFORE] 전체 합성: 4.36s, 243640B  (첫 소리까지 4.36s)
[AFTER]  스트리밍: 총 17.82s, 216890B  (첫 소리까지 3.59s)

첫 소리 단축: 4.36s → 3.59s  (1.2× 빠름)
총 재생 시간: 4.36s → 17.82s  (4배 느려짐)
```

### 2-2. 원인 — `say` 의 고정 기동비용

```
say=3.39s ffmpeg=0.11s audio=0.88s | 안녕하세요!
say=3.46s ffmpeg=0.10s audio=1.13s | 저는 마야입니다.
say=3.35s ffmpeg=0.11s audio=1.87s | 오늘 날씨가 정말 좋네요.
[전체] say=3.54s ffmpeg=0.11s audio=7.61s
```

**`say` 는 텍스트 길이와 무관하게 약 3.4초가 걸린다.**
1초짜리 문장이든 8초짜리 문장이든 같다. 즉 이 3.4초는 **엔진 초기화 비용**이다.

출력 방식 3가지를 모두 시험했고 결과는 동일했다:

```
say -o <tmp>.aiff            3.58s
say -o <tmp>.wav LEI16@16k   3.96s
say -o - (stdout)            3.95s
```

→ **문장 단위로 쪼개면 3.4초를 문장 수만큼 지불한다.**
스트리밍의 전제("짧은 단위가 더 빨리 나온다")가 `say` 에는 성립하지 않는다.

### 2-3. 결론

| TTS 엔진                  | 문장 스트리밍 효과                     |
| ------------------------- | -------------------------------------- |
| **macOS `say` (현재)**    | ❌ **역효과**. 고정비용이 지배         |
| ONNX/GGUF TTS (한국어 지원 모델) | ✅ 유효. 초기화 후 추론이 수십~수백 ms |
| 클라우드 TTS              | ✅ 유효 (네트워크 지연만 남음)         |

**지금 상태의 권장 조치 (택1):**

| 선택지                | 방법                        | 첫 소리        |
| --------------------- | --------------------------- | -------------- |
| A. 되돌리기           | `.bak-streamtts` 복원       | 4.36s          |
| B. 짧은 턴만 스트리밍 | 문장이 2개 이상일 때만 분할 | 3.59s (2문장+) |
| C. **TTS 엔진 교체**  | 한국어 지원 모델 (§10-2 참조) | **목표 <1s**   |

> 💡 **C가 진짜 해법이다.** 지금 병목은 STT도 LLM도 아니고 `say` 다.
> 분석 문서의 "13초" 는 `say` 기동 + 문장 낭독 시간이 섞인 값으로 보이며,
> 엔진을 바꾸지 않으면 어떤 스트리밍 기법도 체감 개선으로 이어지지 않는다.

현재 코드는 **B에 가깝게** 동작한다(`split_sentences` 가 짧은 조각을 합침).
되돌리려면:

```bash
cd /Users/brianyeon/CodeGPT/maya
.venv-stt/bin/python ../maya-2/integration/patch_webapp_server.py --revert
```

### 2-4. 적용된 코드 (`webapp_server.py`)

```python
SENTENCE_RE = re.compile(r"[^.!?。？！\n]+[.!?。？！]?", re.M)

def split_sentences(text, min_len=4):   # 짧은 조각("네.")은 다음 문장과 합침
    ...

async def stream_tts(ws, tts, reply):
    for idx, sentence in enumerate(split_sentences(reply)):
        pcm = await loop.run_in_executor(None, tts.synthesize, sentence)
        await ws.send_json({
            "type": "tts_audio", "audio": b64, "sample_rate": 16000,
            "seq": idx, "final": idx == len(sentences) - 1, "text": sentence,
        })
```

프로토콜 확장: `seq` / `final` / `text` 필드 추가 (하위 호환 — 기존 클라이언트는 무시).
클라이언트가 `final` 을 쓰면 "마지막 조각에서 상태 해제" 가 정확해진다.

---

## 3. 🔴 미해결 — 브라우저가 WebSocket 에 연결되지 않는다

**이것이 모든 개선을 무효로 만드는 1순위 문제다.**

```
index.html:102        ws://…/privseai/ws     ← 페이지가 여기로 붙음
webapp_server.py:269  @app.websocket("/ws")  ← 실제 존재하는 경로는 이것뿐

/privseai/ws  → 403 Forbidden
/ws           → 200 연결 성공
```

분석 문서는 원인을 "IIFE/전역 노출 충돌" 로 적었으나 **그것이 아니다.**
`window.startCall` 은 정상 노출된다. 순수하게 **WS 경로 불일치**다.

수정 (1줄):

```diff
- ws = new WebSocket(wsProto + '//' + location.host + '/privseai/ws');
+ ws = new WebSocket(wsProto + '//' + location.host + '/ws');
```

대상: `/Users/brianyeon/CodeGPT/privseai/webapp/static/index.html`

---

## 4. ✅ SenseVoice — faster-whisper 를 명확히 대체한다

### 4-1. 실측 (한국어 8문장, 합성음 17.13초)

```
── SenseVoice [model.int8.onnx] (83MB) ──
로드 4.29s
     120ms  안녕 하 세요.
     175ms  오늘 날씨 가 정말 좋 네요.
     169ms  내일 회의 는 몇 시에 시작 해요.
     183ms  배 송이 너무 늦 어서 화가 났 어요.
     199ms  그거 어떻게 하는지 알려주실 수 있나요?
     236ms  예약을 취소하고 싶은데 가능할까요?
     221ms  결 제가, 두 번 됐 어요 확인 해 주세요.
     198ms  감사 합니다 좋 은 하루 보내 세요.
  합계 1501ms / RTF 0.088

── faster-whisper [tiny] ──
로드 2.22s
     690ms  안녕, 안녕하세요.
     884ms  오늘 날씨가 정말 좋네요
     959ms  내일 회 있는 멕시의 시작해요.      ← 오인식
   11466ms  대송이 너무 늦어서 화가 났어요.    ← 오인식 + 폭주
     756ms  그거, 어떻게 하는지 알려주실 수 있나요?
     751ms  예약을 취소하고 싶은데, 가능할까요?
     756ms  결제가 두번 되었어요. 확인해 주세요.
     660ms  감사합니다. 좋은 하루 보내세요.
  합계 16922ms / RTF 0.988
```

| 엔진                | CER       | 총 시간   | RTF       | 모델 크기 |
| ------------------- | --------- | --------- | --------- | --------- |
| **SenseVoice int8** | **3.4%**  | **1501ms**| **0.088** | **83MB**  |
| faster-whisper tiny | 15.2%     | 16922ms   | 0.988     | 75MB      |

**11× 빠르고 CER 은 4.5× 낮다.** int8(83MB)이 whisper tiny(75MB)와 비슷한 크기다.
**교체는 사실상 확정 사항**으로 봐도 된다.

### 4-2. 단, 2가지 부작용

1. **출력 띄어쓰기가 과도하다** — `안녕 하 세요.` / `배 송이` / `결 제가`
   → NAMO 턴 판정에는 영향 없음(문장 종결 여부만 본다).
   → 화면 표시/로그에는 거슬린다. 한국어 조사 사전 기반 후처리 또는
     `use_itn` 조합 튜닝 필요.
2. **faster-whisper 가 한 문장에서 폭주** — 11.4초를 쓰며 `대송이` 로 오인식.
   실시간 대화에서 치명적. SenseVoice 는 같은 문장을 183ms 에 정확히 처리.

### 4-3. 교체 방법

```python
import sherpa_onnx
D = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=f"{D}/model.int8.onnx", tokens=f"{D}/tokens.txt",
    num_threads=4, use_itn=True, language="ko")

def transcribe(pcm):                      # Int16 16k mono → str
    audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
    s = rec.create_stream(); s.accept_waveform(16000, audio)
    rec.decode_stream(s)
    return s.result.text
```

`tools/pcm_turn_engine.py` 의 `Transcriber.transcribe()` 만 이 함수로 바꾸면 된다.
인터페이스(PCM 바이트 → 텍스트)가 동일해서 나머지 코드는 손댈 필요 없다.

> ⚠ 로드에 4.29s 걸린다. 서버 startup 에서 미리 생성해야 첫 턴이 느리지 않다.
> (현재 faster-whisper 도 startup 에서 ~4s 걸려 이 부분은 동일하다.)

### 4-4. 설치 상태

| 항목     | 값                                                             |
| -------- | -------------------------------------------------------------- |
| 아카이브 | `models/sensevoice.tar.bz2` **1.05GB** — 압축해제 후 삭제 가능 |
| 모델     | `models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/`   |
| int8     | `model.int8.onnx` **83MB** ← 이것을 쓸 것                       |
| 라이선스 | MIT                                                            |
| 실행     | `sherpa_onnx 2.0.3` (설치됨)                                    |
| `funasr` | ❌ 사용 불가 — `llvmlite` numpy<2 충돌 (분석 문서 판단과 일치) |

---

## 5. 환경 정리 권고

| 항목                                     | 조치                                |
| ---------------------------------------- | ----------------------------------- |
| `inworld-sava` 1.0.0                     | ❌ 제거 권장 (한국어 미지원, 3.1GB) |
| `~/.cache/huggingface/.../inworld--sava` | 49MB — 제거                         |
| `models/sensevoice.tar.bz2`              | 압축해제 후 **1.05GB 삭제**         |
| `say` TTS                                | 🔴 **교체 검토** (3.4s 고정비용)    |

```bash
cd /Users/brianyeon/CodeGPT/maya
.venv-stt/bin/python -m pip uninstall -y inworld-sava
rm -rf ~/.cache/huggingface/hub/models--inworld--sava
rm -f models/sensevoice.tar.bz2      # 압축해제 확인 후
```

---

## 6. 파일 맵

| 파일                                 | 역할                                                         |
| ------------------------------------ | ------------------------------------------------------------ |
| `integration/turn_detector.py`       | NAMO 래퍼 (→ `maya/tools/turn_detector.py` 심링크)           |
| `integration/pcm_turn_engine.py`     | 3단 턴 판정 (→ `maya/tools/pcm_turn_engine.py`)              |
| `integration/patch_webapp_server.py` | `webapp_server.py` 앵커 패치 (멱등, `--dry-run`, `--revert`) |
| `integration/validate_namo.py`       | 54문장 정확도 측정                                           |
| `integration/test_turn_engine.py`    | 턴 엔진 4가지 시나리오                                       |
| `integration/verify_live.py`         | 실행 중 서버 WS 검증                                         |

백업 (원복용):

```
maya/tools/webapp_server.py.bak-streamtts
maya/tools/pcm_turn_engine.py.bak-namo
```

---

## 7. 다음 우선순위

| 순위  | 작업                                            | 이유                            |
| ----- | ----------------------------------------------- | ------------------------------- |
| **1** | `index.html` WS 경로 수정 (1줄)                 | 이것 없이는 아무것도 체감 안 됨 |
| **2** | TTS 엔진 교체 (audio.cpp Stream 모델)           | `say` 3.4s 고정비용이 진짜 병목 |
| **3** | **SenseVoice 로 전사기 교체** | CER 3.4% / RTF 0.088 — 교체만 하면 턴 지연 대폭 감소 |
| 4     | AEC (`speexdsp`)                                | 스피커 되먹임 — 실사용 후 판단  |
| 5     | `max_turn_ms` 8s → 12s                          | 긴 문장 잘림 방지               |

---

## 8. 정직한 한계

- NAMO 100% 는 **합성음 텍스트** 기준. 실제 마이크 전사에는 문장부호가 없고
  오인식이 섞인다. 실사용 3일 로그 후 재측정 필요.
- 스트리밍 TTS 는 **코드는 들어갔으나 효과 없음.** 문서에 그대로 남긴다.
- SenseVoice 실측 완료 — **CER 3.4%, 11× 빠름**. 단 합성음 8문장 기준.
- 서버는 launchd(`com.maya.webapp`) 관리 — 파일 수정 후
  `launchctl kickstart -k gui/$(id -u)/com.maya.webapp` 로 재기동.

---

## 9. 🔬 벤치마크 재검증 — 8문장 결론은 과대평가였다

§4 의 "CER 3.4% / 11× 빠름" 은 **8문장·합성음·1회 측정**에서 나온 값이다.
`integration/bench_stt_hard.py` 로 5개 축을 흔들어 재측정했다.

### 9-1. 먼저 `bench_stt.py` 자체의 결함 3개를 고쳤다

| 결함 | 문제 | 수정 |
|---|---|---|
| O 판정 마커 | `got[:6] in text` — **앞 6글자만 맞으면 통과**. 문장 중간이 틀려도 O | CER ≤ 10% 로 교체 |
| `if variant == "model.int8.onnx": break` | int8 만 재고 **full 모델은 영원히 안 돌아감** | break 제거 |
| `import shutil` | 미사용 | 삭제 |

### 9-2. 5축 재측정 결과 (40문장 / 101.8초)

```
                                 CER        평균       p90     RTF
SenseVoice int8                 2.6%      115ms      174ms   0.044
SenseVoice int8 #2              2.6%      167ms      276ms   0.064
faster-whisper tiny            11.5%      919ms      641ms   0.352
faster-whisper tiny #2         11.6%      963ms      610ms   0.369
```

**결론은 유지된다.** 표본을 5배로 늘려도 CER 2.6%(8문장 때 3.4%보다 오히려 낮다),
RTF 0.044. whisper tiny 는 11.5% / 0.352. **여전히 SenseVoice 우세.**

### 9-3. 다만 8문장에서는 안 보이던 문제가 드러났다

| # | 발견 | 영향 |
|---|---|---|
| **1** | **일본어 문자 누출** — `아니요.` → `아니を？` (を = 일본어 히라가나) | 🔴 경미하나 치명적 (한국어 앱에서 일본어 표시) |
| **2** | **긴 문장일수록 정확** — 짧음 3.9% / 중간 2.1% / 김 0.6% | 짧은 백채널이 가장 약하다 |
| **3** | **워밍업 없음** — 1회차 115ms → 2회차 167ms (오히려 느려짐) | 서버 재사용이 필수 |
| **4** | **노이즈는 강하다** — σ=0.005 에서 3.9%, σ=0.02 에서 6.8% | ✅ 실제 마이크 환경에 유리 |
| **5** | whisper 는 노이즈에서 붕괴 — σ=0.02 에서 **CER 62%, p90 24초** | 🔴 실사용 불가 |
| **6** | **짧은 음성에서 SenseVoice 는 빈 출력** — `네.` 등 0.5초 미만 | 턴 엔진 `min_turn_bytes` 가 방어 중 |

> ⚠ **5번 화자 테스트는 무효**다. Yuna 만 정상이고 Eddy/Flo 는 macOS 가
> `say -o` 로 파일을 못 만들어 **오디오가 0바이트**였다 (양쪽 다 CER 100%).
> 화자 일반화는 여전히 **미검증**이다.

### 9-4. 수정된 권고

| 항목 | 8문장 결론 | 40문장 재검증 |
|---|---|---|
| SenseVoice 우세 | ✅ | ✅ **유지** |
| CER 3.4% | — | **2.6%** (더 좋음) |
| RTF 0.088 | — | **0.044** (2배 좋음) |
| 화자 일반화 | 미확인 | 🔴 **여전히 미검증** |
| 일본어 누출 | 몰랐음 | 🔴 **새로 발견** |

**추가 조치:**
1. 전사 결과에서 **일본어 히라가나/가타카나 제거** 후처리 필수
2. `Transcriber` 를 **startup 시 1회 생성**해 재사용 (워밍업)
3. 화자 일반화는 실제 마이크 녹음으로만 검증 가능 — 합성음으로는 불가

---

## 10. 🆕 audio.cpp 검토 (사용자 제안)

**저장소**: [0xShug0/audio.cpp](https://github.com/0xShug0/audio.cpp) — 4.4k ★

한 줄 요약: **ggml 기반 순수 C++ 오디오 추론 엔진.** Python 의존성 없이
TTS·ASR·VAD·화자분리·음성변환을 한 바이너리로 돌린다. 154개 모델 패밀리 / 250+ 변형.

### 10-1. 우리 문제에 직접 닿는 부분

| 우리 병목 | audio.cpp 대응 | 판정 |
|---|---|---|
| `say` 3.4s 고정비용 (TTS) | TTS 30+ 모델, 대부분 **Stream** 지원 | ✅ **해결 후보** |
| SenseVoice 일본어 누출 | `SenseVoice-Small` GGUF Q8, `ko` 공식 지원 | 🔶 모델은 같음 |
| 턴 감지 | `silero_vad`, `firered_vad` 등 Stream VAD | 🔶 NAMO가 더 전문적 |
| faster-whisper 폭주 | `parakeet_tdt`(한국어 없음), `crisperwhisper` | 🔶 무관 |

### 10-2. 한국어 TTS 후보 (전부 `ko` 공식 지원)

| 모델 | 크기 | 런타임 |特徴 |
|---|---|---|---|
| `supertonic` — Supertonic 3 | — | GGUF F32, **Stream** | RTX 5090에서 3분에 10시간 생성 |
| `qwen3_tts` — Qwen3-TTS 0.6B | 0.6B | Clone/Design/Ctrl | 한국어 |
| `ming3_tts` — Ming 3.0 0.5B | 0.5B | GGUF BF16/Q8, **Stream** | |
| `m5_tts` — M5-TTS 50M | **50M** | GGUF F16/Q8, **Stream** | 가장 작음 |
| `cosyvoice3` — Fun-CosyVoice3 | — | GGUF F32/Q8, LoRA | Clone |

⚠️ **Kokoro 82M 은 `ko` 미지원** (en/es/fr/hi/it/ja/pt/zh). §2 에서 "Kokoro 로
교체" 라고 쓴 것은 **부정확했다** — 한국어 TTS 로는 쓸 수 없다. 정정한다.

### 10-3. 도입 여부 판단

| 항목 | 평가 |
|---|---|
| 라이선스 | 오디오 모델별 개별 라이선스 (`docs/model_licenses.md`) — **상업적 사용 전 확인 필수** |
| 빌드 | CMake 4.4.4 ✅ / Apple clang 21 ✅ — **Intel Mac 은 CPU 백엔드만** (Metal 이득 없음) |
| 통합 비용 | Python 바인딩 없음 → **CLI 또는 서버(socket) 경유** 필요 |
| 이득 | TTS 3.4s → ? (미측정) |
| 위험 | 154개 모델 실험적 포팅, v0.9.6 |

**결론: TTS 문제에 한해 검토 가치 있음.** 단, 우리 병목(`say`)을
**실측으로 대체**해야 의미가 있다. STT 는 SenseVoice 가 이미 좋으므로
audio.cpp 로 갈 이유가 약하다.

### 10-4. 다음 실험 (권장 순서)

1. audio.cpp 빌드 (CPU only) — `cmake -B build && cmake --build build -j`
2. `--task tts` 로 한국어 모델 1개 실행 → **첫 소리까지 시간 측정**
3. `say` 3.4s 보다 유의미하게 빠르면 채택, 아니면 기각
4. 채택 시 `stream_tts()` 의 `tts.synthesize` 만 교체 (인터페이스 동일)

> ⚠ **주의**: 이 저장소는 오늘 처음 등장했다. 실험적 포팅이 많고
> v0.9.6 이다. 운영 경로(`maya`)에 바로 넣지 말고 별도 브랜치/경로에서 검증할 것.

### 10-5. 14개 레포 목록 갱신

이로써 검토 대상은 **15개**가 된다 (audio.cpp 추가).
기존 결론(§1~§8)은 유지되며, audio.cpp 는 **TTS 엔진 후보**로 §2 의
"엔진 교체" 항목을 구체화한다.

---

## 11. 🆕 추가 3개 레포 검토 (ebook2audiobook / uzu / korean-tts-pipeline)

§10 이후 사용자가 제안한 3개. **전부 한국어 TTS 병목 관점에서 평가**했다.

> ⚠️ **전제**: 현재 병목은 STT 도 LLM 도 아니다. **`say` 3.4s 고정비용**이다(§2-2).
> 따라서 판단 기준은 하나다 — *"실시간 대화에 쓸 수 있는 한국어 TTS 를 주는가"*.

### 11-1. 한 장 비교

| 레포                | 성격                     | 한국어 TTS        | 우리 병목 해결 | 판정         |
| ------------------- | ------------------------ | ----------------- | -------------- | ------------ |
| **ebook2audiobook** | 책→오디오북 파이프라인   | ✅ (kor / 1696언어) | 🔶 **부분적**  | ✅ **참조용 채택** |
| **uzu**             | LLM 추론 엔진 (Rust)     | ❌ 없음           | ❌ 없음        | ❌ **범위 밖** |
| **korean-tts-pipeline** | 학습 데모 (FastSpeech2) | 🔶 모델만       | ❌ 없음        | ❌ **참고 불가** |

---

### 11-2. ✅ ebook2audiobook — **TTS 엔진 카탈로그로 가치 있음**

[20.6k ★ / Apache-2.0](https://github.com/DrewThomasson/ebook2audiobook), 16,179 커밋, 활발함.

**우리에게 의미 있는 부분은 기능이 아니라 "무엇이 있는지"다:**

- 한국어(kor) 공식 지원, SUPPORTED_LANGUAGES.md 에 명시
- TTS 엔진 50+: `XTTSv2, Piper, Zonos2/3, CosyVoice3, Qwen3 TTS, Supertonic, VoxCPM2, Kokoro, MeloTTS, ChatTTS, F5-TTS …`
- §10 의 audio.cpp 목록과 **편집증 수준으로 겹친다** → 즉 **한국어 TTS 후보 시장은 이미 좁혀졌다**

#### 🔴 그러나 도구 자체는 우리 파이프라인에 맞지 않는다

| 항목 | 문제 |
| --- | --- |
| **파일 배치 처리** | 책 전체를 **오프라인 변환**. Gradio UI / CLI 일괄 실행용 |
| **실시간 스트리밍 없음** | README 에 `stream` 언급 0회. "near realtime" = **생성 속도**이고 재생 지연이 아니다 |
| **최소 요구** | 2GB RAM / 1GB VRAM — 우리 Intel CPU 경로에서 무거운 모델은 사실상 불가 |
| **자체 README** | "Modern TTS engines are very slow on CPU" |
| **전체 엔진 옵션** | CLI `--tts_engine` 은 실제로 `XTTS, BARK, VITS, FAIRSEQ, TACOTRON, YOURTTS` 6개만 노출. 나머지는 별도 경로 |

**→ 채택 방식**: 도구를 쓰지 말고 **엔진 후보 목록만 빌려** 개별 측정한다.

**단, 하나는 진짜 유용하다** — `[pause:N]` / `[break]` **SML 태그**. 문장 사이 쉼을
세밀하게 제어하는 기능은 대화형 TTS 에서 바로 쓸 수 있는 패턴이다.
(§2 스트리밍 TTS 를 되살릴 때 문장 간 지연 설계에 참고)

---

### 11-3. ❌ uzu — LLM 엔진, 오디오 무관

[3.4k ★ / MIT](https://github.com/trymirai/uzu)

| 항목 | 사실 |
| --- | --- |
| 실제 범위 | **LLM 추론 엔진** (Rust, Metal/Vulkan/CPU) |
| 주 기능 | speculative decoding (M5 에서 llama.cpp 4× 속도) |
| 바인딩 | Rust / Swift / Python / TypeScript |
| **TTS 지원** | ❌ **README 본문 전체에 TTS/audio 0회** (GitHub 주제 태그의 `tts` 는 오탐) |
| 한국어 | 모델에 따라 다름 (엔진이 아니라 모델의 속성) |

**대안 가치**: 우리 `FallbackLLM`(Ollama qwen2.5-coder:1.5b) 경로를 이 엔진으로
바꿀 수는 있다. 그러나:

- **Intel Mac** → Metal 가속 없음, CPU 백엔드만
- uzu 의 강점(speculative decoding)은 **Apple Silicon + Metal** 전제
- 현재 LLM 지연은 병목이 아니다 (실측: 병목은 TTS)

**→ 결론: 지금 단계에서 도입 이유 없음.** Apple Silicon 으로 이전하면 재검토.

---

### 11-4. ❌ korean-tts-pipeline — 학습 데모, 제품 아님

[0 ★ / 4 커밋](https://github.com/weonjungin/korean-tts-pipeline)

| 항목 | 사실 |
| --- | --- |
| 구성 | English STT → 영→한 번역 → 한국어 TTS (FastSpeech2 + VocGAN) |
| 데이터 | KSS(한국 단일화자) + 개인 음성 313 epoch 파인튜닝 |
| 코드량 | 7개 파일 (README/hparams/modules/preprocess/synthesize/예제) |
| 실제 용도 | **개인 학습 과제물** |

**이게 우리에게 못 주는 것:**

- **한국어 STT 없음** — Wav2Vec2 base-960h 는 **영어 전용**. 우리는 이미 SenseVoice 로 해결
- **번역 없음** — 우리는 한국어→한국어 대화다
- **TTS 는 파인튜닝 체크포인트** — 배포용 모델 공개 없음 (KSS 기반이라 화자 1명)
- **실시간 불가** — FastSpeech2 + VocGAN 은 문장 단위 배치 합성

**유일하게 참고할 점**: `preprocess.py`/`synthesize.py` 의 **한국어 G2P
(g2pK, jamo)** 처리. §9-3 에서 발견한 **SenseVoice 일본어 누출**과는 무관하지만,
나중에 자체 TTS 를 학습할 때 한국어 텍스트 정규화 참고로 쓸 수 있다.

**→ 결론: 도입 불가. 참고 파일 1개(한국어 G2P)만 가치 있음.**

---

### 11-5. 세 레포가 알려주는 것 — 후보는 이미 좁혀졌다

세 레포를 합쳐도 **"실시간 한국어 TTS"를 바로 주는 것은 없다.**
대신 **후보 목록이 세 번 교차 검증됐다**:

| 엔진 | audio.cpp (§10) | ebook2audiobook | 특징 |
| --- | --- | --- | --- |
| Qwen3-TTS 0.6B | ✅ ko | ✅ | Clone/Design/Ctrl |
| CosyVoice3 | ✅ ko | ✅ | LoRA |
| Supertonic 3 | ✅ ko, Stream | ✅ | 최고 속도 주장 |
| VoxCPM2 | ✅ ko | ✅ | Clone/Design |
| Ming 3.0 0.5B | ✅ ko, Stream | — | |
| M5-TTS 50M | ✅ ko, Stream | — | **가장 작음** |
| MeloTTS | — | ✅ | 가볍고 CPU 친화적 |
| XTTSv2 | — | ✅ (기본) | 범용, 무겁다 |

**→ TTS 실험은 이 목록에서 3개만 골라 측정하면 된다.** 전부 새로 조사할 필요 없다.

---

### 11-6. 수정된 권고

| 순위 | 작업 | 근거 |
| --- | --- | --- |
| **1** | `index.html` WS 경로 1줄 (여전히 최우선) | 이것 없이는 아무것도 체감 안 됨 |
| **2** | **MeloTTS 또는 M5-TTS(50M)** 로 첫 소리 지연 측정 | CPU 친화 + 한국어 + 가벼움 |
| 3 | audio.cpp Stream 모델 비교 | §10 |
| 4 | SenseVoice 일본어 제거 후처리 | §9-3 |
| ❌ | uzu | LLM 전용, Intel Mac 이점 없음 |
| ❌ | korean-tts-pipeline | 학습 데모, 배포 모델 없음 |
| 🔶 | ebook2audiobook | 도구로는 부적합, **엔진 카탈로그로만 참조** |

**검토 레포 누적: 14 → 17개** (§10 audio.cpp, §11 3개).

---

## 12. 🎯 TTS 후보 측정 결과 — **Piper 한국어가 13.8× 빠르다**

§2 에서 "`say` 3.4초 고정비용이 진짜 병목" 이라고 결론냈다.
`integration/bench_tts.py` 로 후보를 실제 측정했고, **`say` 를 대체할 엔진을 찾았다.**

### 12-1. 최종 측정표

| 엔진 | 첫 소리(warm) | RTF | CER | 대비 |
| --- | --- | --- | --- | --- |
| `say` Yuna (현재) | **3812ms** | 2.332 | 0.0% | 기준 |
| **`piper` ko_KR-kss-medium** | **276ms** | **0.141** | **0.0%** | **13.8× 빠름** |
| sherpa vits-mimic3-ko | ❌ 실패 | — | — | lexicon 없음 |
| sherpa kokoro-int8-multi-lang | ❌ 실패 | — | — | ko lexicon 미배포 |

**측정 조건**
- 6문장 순환, warm 3회 반복 (서버가 실제로 처하는 조건)
- cold 포함 (모델 로드 후 첫 호출): Piper **236ms**
- CER = 생성 오디오를 **SenseVoice 로 되받아 전사** → 원문 비교 (발음 뭉개짐 탐지)

### 12-2. 청취 + 역전사 검증

CER 0% 는 문장 단위 통계다. 실제로 한국어가 들리는지 직접 확인했다.

```
원문   : 안녕하세요. 저는 마야입니다. 무엇을 도와드릴까요?
역전사 : 안녕하세요 저는 마야입니다 무엇을 도와드릴까요?
```

4.56초 분량 오디오를 **526ms** 에 생성. 문장부호만 빠지고 **단어 하나 틀리지 않았다.**
`afplay` 로 실제 재생도 완료 (사용자 스피커에서 확인 가능).

### 12-3. 왜 나머지는 실패했는가 (중요)

| 모델 | 실패 원인 |
| --- | --- |
| `sherpa vits-mimic3-ko` | `phoneme_codepoints is empty, no lexicon` — IPA 음소 사전이 필요한데 한국어 g2p 미포함 |
| `sherpa kokoro-int8-multi-lang` | 패키지에 `lexicon-gb-en/zh` 만 있음. **한국어 lexicon 파일이 공식 배포에 없음** |
| `piper` (초기 시도) | sherpa 용으로 변환된 모델과 piper 자체 포맷이 달라 `KeyError: num_symbols` → **piper 공식 보이스로 교체하니 성공** |
| `MeloTTS` (PyPI) | 패키징 버그: `FileNotFoundError: requirements.txt` — **설치 자체 불가** |

> 💡 교훈: **ONNX 모델 파일만으로는 TTS 가 안 된다.** 음소화(g2p) + lexicon 이
> 함께 있어야 한다. `kokoro-multi-lang` 이 "한국어 지원" 이라고 해도
> sherpa 배포판에는 한국어 lexicon 이 빠져 있다.

### 12-4. Kokoro 관련 정정 (2번째)

- §10-2: "Kokoro 82M 은 ko 미지원" → **맞다** (그 모델은 en/es/fr/hi/it/ja/pt/zh)
- §11-6: "multi-lang 은 한국어 포함" → **모델은 그렇지만 sherpa 배포판에 ko lexicon 이 없다**
- 실질 결론: **현재 경로에서는 Kokoro 로 한국어 TTS 불가**

### 12-5. Piper 채택 권고

| 항목 | 값 |
| --- | --- |
| 패키지 | `piper-tts 1.8.0` (설치 완료) |
| 모델 | `rhasspy/piper-voices` → `ko_KR/kss/medium/ko_KR-kss-medium.onnx` (**63MB**) |
| 위치 | `models/tts/piper-ko/` |
| 라이선스 | MIT (piper) / KSS 음성 데이터는 CC BY-NC-SA 4.0 → **상업적 사용 전 원문 확인 필수** |
| 샘플레이트 | **22050Hz** (현재 `say` 경로는 16000Hz) |
| 로드 | 2.25s (startup 1회) |
| 스레드 | `num_threads` 조절 가능 |

#### ⚠️ 통합 시 주의 2가지

1. **샘플레이트 불일치** — Piper 는 **22050Hz**, 현재 서버/클라이언트는 **16000Hz**.
   `playAudioPCM` 은 `audioCtx.sampleRate` 를 쓰므로 **그대로 보내면 1.38× 느리게/낮게 재생된다.**
   → 서버에서 16k 로 리샘플(ffmpeg `-ar 16000`) 하거나, `tts_audio` 메시지의
   `sample_rate` 를 클라이언트가 실제로 사용하도록 고쳐야 한다.
2. **`LocalTTS` 인터페이스 유지** — `synthesize(text) -> bytes` 시그니처가 동일하므로
   `tools/tts_local.py` 의 `_say` 를 교체하면 나머지는 그대로 동작한다.

### 12-6. 예상 효과

| 구간 | 현재 | Piper 적용 후 |
| --- | --- | --- |
| TTS 첫 소리 | 3812ms | **276ms** |
| TTS 총 생성 (문장) | 3812ms | ~276ms (RTF 0.141) |
| 턴 확정 (NAMO) | ~700ms | ~700ms |
| **말 끝 → 첫 소리 체감** | **~4.5s** | **~1.0s** |

> §2 의 스트리밍 TTS 는 `say` 의 고정비용 때문에 역효과였지만, **Piper 는
> RTF 0.141 이고 고정비용이 없으므로 문장 스트리밍이 그때 비로소 유효해진다.**
> `split_sentences` + `stream_tts` 코드는 이미 들어가 있으니 그대로 쓰면 된다.

### 12-7. 다음 작업

| 순위 | 작업 | 비고 |
| --- | --- | --- |
| **1** | `tts_local.py` 에 `PiperTTS` 백엔드 추가 (기본값) | `_say` 는 폴백으로 유지 |
| **2** | **22050 → 16000 리샘플** 처리 | 안 하면 재생 속도/음높이 틀어짐 |
| 3 | 문장 스트리밍 재활성화 검증 | 이번엔 효과 있을 것 |
| 4 | Piper 음질 주관 평가 | KSS 단일화자, 감정 표현 없음 |

---

## 13. ✅ Piper 통합 완료 (1~4번 작업)

§12 의 권고 4개를 모두 적용하고 **서버에서 실제로 동작하는 것까지 확인**했다.

### 13-1. 적용 내용

| # | 작업 | 결과 |
| --- | --- | --- |
| 1 | `tts_local.py` 에 Piper 백엔드 | ✅ `engine="auto"` → piper 우선, say 폴백 |
| 2 | 22050 → 16000 리샘플 | ✅ ffmpeg 우선, numpy 선형보간 폴백 |
| 3 | 문장 스트리밍 재검증 | ✅ **3문장 각 0.25~0.31s** |
| 4 | 음질 주관/객관 평가 | ✅ CER 0%, 역전사 원문 일치 |

변경 파일:
```
tools/tts_local.py      (백업: tts_local.py.bak-piper)
tools/piper_worker.py   (신규)
tools/webapp_server.py  (TTS_ENGINE 기본값 say → auto)
```

### 13-2. 🔴 도중에 발견한 실제 장애 — OpenMP 런타임 충돌

첫 통합 직후 **서버에서 TTS 가 조용히 실패**했다 (`[stream] TTS 실패 (1/3):` — 에러 메시지도 없음).

```
OMP: Error #15: Initializing libiomp5.dylib, but found libiomp5.dylib already initialized.
OMP: Hint This means that multiple copies of the OpenMP runtime have been linked into the program.
→ SIGABRT
```

**원인**: 이 환경에 libiomp5 를 링크한 라이브러리가 두 개 있다.

| 라이브러리 | 용도 |
| --- | --- |
| `ctranslate2 4.9.0` | faster-whisper (STT) |
| `onnxruntime 1.23.2` | piper (TTS) |

한 프로세스에서 둘을 쓰면 충돌한다. launchd 가 `KMP_DUPLICATE_LIB_OK=TRUE` 를
설정해 두어 **크래시는 막혔지만, 그 대신 piper 합성이 빈 예외로 실패**했다.
(이 플래그가 없으면 서버 전체가 SIGABRT 로 죽는다.)

**해결**: piper 를 **별도 프로세스**(`tools/piper_worker.py`)로 격리.
- IPC: 4바이트 BE 길이 + UTF-8, 응답은 상태1바이트 + 길이 + 페이로드
- IPC 비용 ~1ms (측정상 무시 가능)
- 워커가 죽으면 **자동 재기동**
- `LocalTTS.close()` 로 정리

> ⚠ 교훈: **`KMP_DUPLICATE_LIB_OK=TRUE` 는 해결이 아니라 증상 은폐다.**
> 이 플래그가 켜져 있으면 OMP 충돌이 "빈 에러 메시지"로 둔갑해 원인 추적이
> 매우 어려워진다. 같은 venv 안에 ctranslate2 와 onnxruntime 을 함께 두는 한
> 프로세스 분리가 유일하게 안전한 방법이다.

### 13-3. 최종 종단 실측 (서버 라이브, 발화 종료 기준)

```
+ 1009ms  thinking              ← NAMO 턴 확정 + 전사 재사용
+ 1010ms  final: '날씨가 정말 좋네요.'
+ 4362ms  final_reply: '무난하죠! 날씨를 좋아하시는군요! 오늘도 즐기세요!'
+ 4609ms  첫 tts_audio (seq=0) '무난하죠!'
+ 5202ms  마지막 tts_audio (final) '오늘도 즐기세요!'
```

서버 로그 — 문장 스트리밍이 정확히 동작:

```
[stream] TTS 스트리밍 시작 (28자, 3문장)
[stream] TTS 1/3 0.25s 40126B '무난하죠!'
[stream] TTS 2/3 0.31s 64646B '날씨를 좋아하시는군요!'
[stream] TTS 3/3 0.28s 53128B '오늘도 즐기세요!'
```

**Piper 구간은 해결됐다.** 남은 3.3초는 전부 LLM 이다.

### 13-4. 지연 분해 — 병목이 이동했다

| 구간 | 도입 전 | 현재 | 비고 |
| --- | --- | --- | --- |
| 턴 확정 + 전사 (NAMO) | ~700ms | **1009ms** | 정상 |
| LLM (Ollama) | 3.4~22.2초 | **3352ms** | 🔴 **이제 최대 병목** |
| TTS 첫 소리 (Piper) | 3812ms | **247ms** | ✅ 15× 개선 |
| TTS 3문장 총 | 3900ms+ | **840ms** | ✅ |
| **말 끝 → 첫 소리** | **~4.5초** | **4.6초** | 🔴 LLM 이 잡아먹음 |

> ⚠ **정직하게**: TTS 를 15배 빠르게 만들었지만 **체감 첫 소리는 4.6초 그대로**다.
> 병목이 TTS 에서 LLM 으로 옮겨갔기 때문이다. TTS 개선은 스트리밍 도중
> 끊김이 없고, 2·3번째 문장이 즉시 이어지는 이점으로 나타난다.

**LLM 지연 변동이 큰 이유** (로그 근거):
- 3.4초 (짧은 응답) ~ 22.2초 (긴 응답) — 모델 로드 후 캐시 상태에 따라 편차
- `OLLAMA_MODEL=qwen2.5-coder:1.5b` 는 **코드용 모델**이다. 한국어 대화에는 부적합
- 22초 케이스는 콜드 상태였다

### 13-5. 남은 과제 (우선순위 재정렬)

| 순위 | 작업 | 기대 효과 |
| --- | --- | --- |
| **1** | **LLM 모델 교체** — `qwen2.5-coder:1.5b` 는 코드용. `gemma3:1b`/`qwen3:1.7b` 등 대화용 | 3.4초 → 1초대 |
| **2** | 첫 문장 길이 제한 — 응답 첫 문장을 짧게 강제 (`system` 프롬프트) | 첫 소리 추가 단축 |
| 3 | LLM 스트리밍 → 문장 단위 즉시 TTS | 응답 완성 전에 말하기 시작 |
| 4 | SenseVoice 로 STT 교체 (CER 3.4% vs 15.2%) | 전사 정확도 |

### 13-6. 검증 상태

| 항목 | 상태 |
| --- | --- |
| 서버 기동 | ✅ `TTS 초기화 완료 (engine=piper)` |
| piper 워커 프로세스 | ✅ 분리 실행 (pid 93294) |
| WS 연결 (브라우저) | ✅ "WebSocket 연결됨" |
| 문장 스트리밍 | ✅ 3/3 조각 순차 전송 |
| 리샘플 16kHz | ✅ 역전사 CER 0% |
| 서버 크래시 | ✅ 없음 |

---

## 14. 🆕 추가 4개 레포 검토 + LLM 벤치마크

§13 에서 병목이 **TTS → LLM** 으로 이동했다. 그래서 이번 4개는
"TTS 후보" 와 "LLM/오케스트레이션" 두 관점으로 봤다.

### 14-1. 한 장 비교

| 레포 | 성격 | 우리 문제 | 판정 |
| --- | --- | --- | --- |
| **TerminatorTTS-Kokoro-Korean** | Kokoro 한국어 Docker | TTS 대안 | 🔶 **검증됨(이미 Piper로 해결)** |
| **korean-tts** (dyoo) | TF + GRU Seq2Seq TTS 실험 | TTS | ❌ 실험용, 배포 모델 없음 |
| **cactus** | 온디바이스 LLM 엔진 (ARM) | LLM | ❌ **ARM 전용, Intel Mac 불가** |
| **Open-LLM-VTuber** | 라이브2D 대화 에이전트 | **오케스트레이션 참고** | ✅ **설계 참고 가치** |

---

### 14-2. 🔶 TerminatorTTS-Kokoro-Korean — "Kokoro 한국어" 의 실체

[GitHub](https://github.com/yoqer/TerminatorTTS-Kokoro-Korean-Docker)

§12-4 에서 "sherpa 배포판에 한국어 lexicon 이 없다" 고 적었다. 이 레포는
**Kokoro 로 한국어를 돌리는 방법**을 보여준다.

**핵심: Kokoro 자체에 한국어가 없고, 레포가 직접 파인튜닝했다.**

| 항목 | 내용 |
| --- | --- |
| 베이스 | `hexgrad/Kokoro-82M` |
| 파인튜닝 | `Kokoro-82M-ko` (ZeroHKIM 등) |
| 실행 | **Docker 컨테이너** (GPU 가정) |
| 백엔드 | `kokoro` Python 래퍼 + PyTorch |

**우리에게 의미:**

- ✅ Kokoro 한국어가 **존재한다** (직접 학습한 모델로)
- ❌ **Docker + GPU 전제** — 우리는 Intel Mac CPU
- ❌ PyTorch 의존성 추가 — 현재 venv 는 onnxruntime 경량 구성
- ❌ 파인튜닝 모델 품질 검증 안 됨 (벤치마크 없음)

**판정: 이미 Piper(CER 0%, 276ms)로 해결됐으므로 도입할 이유가 없다.**
다만 "Kokoro 로 한국어를 하려면 별도 파인튜닝이 필요하다" 는 사실이
§12-4 의 결론을 **강화**한다.

---

### 14-3. ❌ korean-tts (dyoo) — 2017년 TF 실험

[GitHub](https://github.com/dyoo/korean-tts)

| 항목 | 내용 |
| --- | --- |
| 구조 | English STT → 번역 → 한국어 TTS (Seq2Seq) |
| 모델 | **TensorFlow 1.x + GRU** |
| 연도 | ~2016-2017 |
| 배포 | 체크포인트 없음, 코드만 |

`korean-tts-pipeline`(§11-4) 과 같은 계열의 학습 실험이다.
**TensorFlow 1.x 는 현재 Python 버전에서 돌지 않는다.**
배포 모델도 없어 결과물을 얻을 수 없다.

**판정: 도입 불가.** 참고할 파일도 없다 (G2P 조차 없음).

---

### 14-4. ❌ cactus — ARM 전용이라 Intel Mac 에서 무용

[GitHub](https://github.com/cactus-compute/cactus) — 모바일 온디바이스 LLM 엔진 (C++).

| 항목 | 내용 |
| --- | --- |
| 대상 | iOS / Android (ARM NEON) |
| 백엔드 | ARM NEON, Apple Metal (모바일) |
| 강점 | 휴대폰에서 1B 모델 100 tok/s |

**우리 환경은 Intel Mac (x86_64)** 이다. cactus 의 가속 경로(NEON)는 ARM 전용이라
x86 에서는 이득이 없다. 게다가 우리 LLM 문제는 **속도가 아니라 모델 선택**이다.

**판정: 도입 불가 (하드웨어 불일치).**

---

### 14-5. ✅ Open-LLM-VTuber — **아키텍처 참고용 (가장 값어치 있음)**

[GitHub](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) — 7.5k ★, 라이브2D AI 동반자.

우리가 만드는 것과 **거의 같은 파이프라인**이다:

```
마이크 → VAD → ASR → LLM → TTS → 립싱크 → 스피커
                ↑
        사용자 끼어들기(interrupt) 처리
```

**배울 점 3가지:**

| # | 패턴 | 우리 상황 |
| --- | --- | --- |
| 1 | **인터럽트(끼어들기)** — AI 가 말하는 중에 사용자가 말하면 TTS 즉시 중단 | 🔴 **우리는 미구현** |
| 2 | **VAD 를 ASR 앞단에** — 침묵을 걸러 ASR 낭비를 줄임 | ✅ 우리는 턴 엔진에 있음 |
| 3 | **TTS/ASR/LLM 을 플러그인으로** — 엔진 교체가 설정값 | 🔶 우리는 `LocalTTS` 로 부분 적용 |

**특히 1번이 중요하다.** 우리 서버는 AI 가 말하는 중에 사용자가 다시 말하면
**이전 TTS 가 계속 재생**된다. `stream_tts()` 에 취소(cancel) 처리가 없다.

```python
# 현재: 취소 불가 — 순차 전송만 한다
async def stream_tts(ws, tts, reply):
    for idx, sentence in enumerate(split_sentences(reply)):
        pcm = await loop.run_in_executor(None, tts.synthesize, sentence)
        await ws.send_json({...})
```

**판정: 코드를 가져오지 않고 설계 패턴만 참고.** 인터럽트 처리는 다음 과제.

---

### 14-6. 📊 LLM 벤치마크 결과 (병목 실측)

`integration/bench_llm.py` — 4개 프롬프트, 한국어 대화, 스트리밍.

| 모델 | 첫 토큰 | **첫 문장** | 총 | 한글 비율 |
| --- | --- | --- | --- | --- |
| **qwen2.5-coder:1.5b** (현재) | **1564ms** | **3050ms** | **3636ms** | 98% |
| gemma3:4b | 3437ms | 7262ms | 11387ms | 100% |
| qwen3:1.7b | 14285ms | 15918ms | 16542ms | 75% |

**예상과 정반대 결과다.**

- **gemma3:4b 는 2.4× 느리다** — 파라미터가 2.7배 크니 Intel CPU 에서 당연하다
- **qwen3:1.7b 는 14초** — **thinking 모드**가 기본 활성화되어 추론 토큰을 먼저 생성한다
  (`think=False` 로 끄면 첫 토큰 1171ms 이지만, 답변이 길어져 총 17.8초)
- **현재 qwen2.5-coder:1.5b 가 첫 문장 3.0초로 가장 빠르다**

> 🔴 **정정**: §13-5 에서 "qwen2.5-coder 는 코드용이라 대화에 부적합" 이라고
> 적었는데, **속도 면에서는 이미 최선**이었다. 문제는 속도가 아니라
> **대화 품질**(코드 모델이 대화에 쓰임)이다.

### 14-7. 수정된 LLM 권고

| 선택지 | 첫 문장 | 품질 | 판정 |
| --- | --- | --- | --- |
| qwen2.5-coder:1.5b (현재) | 3050ms | 🔶 코드 편향 | 속도 최선, 품질 아쉬움 |
| gemma3:4b | 7262ms | ✅ 한국어 자연스러움 | 느림 |
| qwen3:1.7b (`think=False`) | ~4000ms | ✅ 대화용 | **균형 후보** |

**Intel Mac 에서 로컬 LLM 으로 1초대는 불가능하다.**
`fallback_llm.py` 에도 이미 같은 결론이 주석으로 있다
("Intel Mac + 로컬 LLM 은 첫 응답이 20~35초").

**그래서 진짜 해법은 LLM 교체가 아니라 3가지다:**

| 순위 | 방법 | 기대 |
| --- | --- | --- |
| **1** | **LLM 스트리밍 → 먼저 나온 문장부터 즉시 TTS** | 첫 소리 = 첫 문장(3.0s)에서 시작, 총 대기 사라짐 |
| **2** | **system 프롬프트로 첫 문장 짧게 강제** | 첫 문장까지 시간 단축 |
| 3 | ClawRouter(클라우드) 폴백 활성화 | 1초대 가능, 단 로컬/프라이버시 트레이드오프 |

> 💡 **1번이 핵심이다.** 현재는 **LLM 응답이 완전히 끝난 뒤** TTS 를 시작한다
> (`final_reply` → `stream_tts`). 스트리밍 중간에 문장이 완성되면
> **그 즉시** TTS 를 시작하면 체감 첫 소리가 4.6초 → 3.0초로 줄고,
> LLM 이 계속 생성되는 동안 이미 말하고 있다.

### 14-8. 누적 검토 레포: 14 → 21개

§10 audio.cpp, §11 3개, §14 4개 추가.

| 결론 | 레포 |
| --- | --- |
| ✅ 실제 적용 | Piper(보이스), NAMO |
| ✅ 설계 참고 | Open-LLM-VTuber(인터럽트), ebook2audiobook(SML 태그) |
| 🔶 검증만 | audio.cpp, TerminatorTTS-Kokoro-Korean |
| ❌ 부적합 | uzu, korean-tts-pipeline, dyoo/korean-tts, cactus |

---

## 15. ✅ LLM 스트리밍 → 문장 즉시 TTS (적용 완료)

§14-7 1순위 작업. **응답 전체를 기다리지 않고, 첫 문장이 완성되는 즉시 말한다.**

### 15-1. 변경 전/후 구조

```python
# BEFORE — 응답 완료까지 침묵
result = await llm.chat(messages)          # 3.0~9.2초 대기
await ws.send_json({"type": "final_reply", ...})
await stream_tts(ws, tts, reply)           # 그 다음에야 첫 소리

# AFTER — 토큰을 받으며 문장 단위로 말한다
async for piece in llm.stream_chat(messages):
    await speaker.feed(piece, ws)          # 문장 완성 즉시 합성·전송
reply, ... = await speaker.finish(ws)      # 남은 꼬리 처리
```

### 15-2. 실측 결과

```
+ 3786ms  첫 tts_audio (seq=0) '좋습니다!'
+ 9160ms  final_reply: '좋습니다! 오늘의 날씨가 매우 편안하고 즐겁기를...' 
```

**첫 소리가 응답 완료보다 5.4초 먼저 나왔다.**
즉 사용자는 LLM 이 나머지 문장을 생성하는 동안 이미 AI 목소리를 듣고 있다.

서버 로그 — 문장별로 정확히 흘러나옴:
```
[streamllm] TTS 0 (첫 소리) 1.09s 59444B '정말 충분히 좋습니다!'
[streamllm] TTS 1 0.88s 65760B '오늘도 어떤 활동을 할까요?'
LLM 응답 출처=ollama-stream
```

### 15-3. 구현 상세

**`tools/ollama_client.py`** — `stream_chat()` 추가
```python
async def stream_chat(self, messages):
    async with self._client.stream("POST", f"{base}/api/chat", json=payload) as resp:
        async for line in resp.aiter_lines():
            obj = json.loads(line)
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                yield piece
```

**`tools/fallback_llm.py`** — 폴백 체인에 위임
```python
async def stream_chat(self, messages):
    async for piece in self.ollama.stream_chat(messages):
        yield piece
```

> ⚠ **여기서 실제 버그를 만들었다.** 처음엔 `OllamaClient` 에만 추가했는데
> 서버는 `FallbackLLM` 을 쓴다. 그래서 조용히 실패했다:
> ```
> LLM 스트리밍 실패 ('FallbackLLM' object has no attribute 'stream_chat') → 비스트리밍 폴백
> ```
> 폴백 경로가 있어서 서비스는 계속됐지만 **최적화는 0% 적용** 상태였다.

**`tools/webapp_server.py`** — `Speaker` 클래스 (핵심 로직)

| 규칙 | 이유 |
| --- | --- |
| 종결부호(`.!?。？！\n`)에서 자름 | 문장 단위 TTS 의 자연스러운 경계 |
| `...` 연속 부호는 한 문장 | 말줄임표 중간에서 끊기지 않게 |
| **12자 미만은 다음 문장과 합침** | `"네."` 같은 조각이 TTS 호출만 낭비 |
| **80자 넘으면 강제 분할** | LLM 이 문장부호 없이 길게 뱉을 때 대비 |
| **부호만 있는 조각은 발화 안 함** | `"..."` → TTS 호출 방지 |
| `interrupt` 플래그 확인 | 사용자 끼어들기 시 중단 (Open-LLM-VTuber 패턴) |

**강제 분할에서 실제 버그 2개를 잡았다:**

```python
# 버그 1: 한글을 글자 중간에서 쪼갬
"네 맞습니다 네 맞습니다" → "네 맞습니다 네 맞습니" + "다 네 맞습니다"
# 수정: 쉼표/공백 우선 탐색, 없으면 경계에서만

# 버그 2: "..." 를 그대로 발화
# 수정: isalnum()/한글 판정으로 걸러냄
```

### 15-4. 폴백 안전망 (중요)

스트리밍이 실패해도 **서비스가 멈추지 않는다**:

```
stream_chat 실패
   → log.warning
   → 비스트리밍 llm.chat() 으로 자동 폴백
   → 기존 stream_tts() 로 문장 스트리밍
```

실제로 위의 `FallbackLLM` 버그 때 이 경로가 작동해 대화는 정상 유지됐다.

### 15-5. 지연 지표의 함정 (정정)

측정 스크립트가 "턴 확정 4533ms" 로 찍었는데, 서버 로그는 **2.4초**였다.

| 지표 | 값 | 차이 |
| --- | --- | --- |
| 서버: 발화 시작 → NAMO 확정 | **2.43초** | 실제 처리 시간 |
| 테스트: `speech_done` → `thinking` | 4.53초 | **송신 완료가 늦게 잡힘** |

원인: 테스트가 오디오를 실시간보다 빠르게 보내 `speech_done` 타임스탬프가
실제 발화 종료보다 늦다. **지연 개선을 논할 때는 서버 로그 기준을 쓴다.**

### 15-6. 현재 상태 총괄

| 구간 | 최초 | 현재 | 비고 |
| --- | --- | --- | --- |
| STT + 턴 확정 (NAMO + SenseVoice 아님) | — | 2.43s | 전사 재사용 |
| LLM 첫 문장 | 3.0s | 3.0s | Intel Mac 한계 |
| **TTS 첫 소리** | **3812ms** | **276ms** | ✅ 15× |
| **LLM 완료 → 첫 소리 대기** | 0 | **-5.4s** | ✅ **먼저 말함** |
| **체감 첫 소리** | ~4.5s | **~3.8s** | ✅ 개선 |
| TTS 문장 수 | 1 (한 번에) | 문장별 | ✅ |

### 15-7. 남은 과제

| 순위 | 작업 | 근거 |
| --- | --- | --- |
| **1** | LLM 응답 품질 — `qwen2.5-coder` 가 코드 모델이라 어색한 한국어 | "즐겁기를 만들 수 있습니다" 같은 문장 |
| 2 | TTS 인터럽트 완성 — `Speaker` 에 플래그는 있으나 미검증 | Open-LLM-VTuber 패턴 |
| 3 | SenseVoice 로 STT 교체 | CER 3.4% vs 15.2% |
| 4 | Airy API 비교 측정 (사용자 요청 시) | 클라우드 TTS, 네트워크 지연 미지수 |

### 15-8. 변경 파일

```
tools/ollama_client.py   (+stream_chat)   백업: .bak-stream
tools/fallback_llm.py    (+stream_chat)
tools/webapp_server.py   (+Speaker, 스트리밍 경로)  백업: .bak-streamllm
integration/verify_pipeline.py  (송신 속도 보정)
```
