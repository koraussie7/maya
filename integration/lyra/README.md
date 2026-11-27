# Lyra 보조 TTS (Supertonic 3)

기존 Maya 음성 파이프라인(voxcpm / piper / say)은 **건드리지 않고**,
Supertonic 3 기반의 Lyra 보이스를 **보조 경로**로 추가한다.

```
existing path:  maya-ui/backend/app/tts.py::synthesize()   ← 0% 수정
new add-only:   maya-ui/backend/app/tts.py::synthesize_lyra()
```

## 활성화

```bash
export MAYA_TTS_LYRA=1               # 보조 경로 켜기
export MAYA_TTS_LYRA_VOICE=F1         # voices/<name>.json  (기본 F1)
export MAYA_TTS_LYRA_SPEED=1.05       # 속도 (기본 1.05)
```

## 의존성

```bash
python -m venv .venv-lyra
source .venv-lyra/bin/activate
pip install supertonic==1.3.1 numpy soundfile
```

`integration/lyra/lyra_tts.py` 는 옵션 의존성 (`supertonic`) 이 없을 때
`LyraTTSUnavailable` 예외로 안전하게 떨어지며, `synthesize_lyra()` 는
빈 바이트열을 돌려준다. 기존 `synthesize()` 에는 영향이 없다.

## 보이스 스타일

`integration/lyra/voices/<name>.json` — Supertonic 3 가 요구하는 형식.

기본 제공 음성:

| 이름 | 코드      |
| ---- | --------- |
| F1   | `F1.json` |
| F2   | `F2.json` |
| F3   | `F3.json` |
| F4   | `F4.json` |
| F5   | `F5.json` |

각 파일은 `LyraTTS.load_style(name)` 을 통해 로드되며
`tts.get_voice_style_from_path(json_path)` 로 검증된다.

## 레퍼런스 음성

`record_reference.py` 는 사용자가 직접 보유한 `.json` 보이스 스타일 파일을
`voices/` 로 복사·검증한다.

```bash
source .venv-lyra/bin/activate
python -m integration.lyra.record_reference --name lyra --json-path ./lyra_style.json
```

> Supertonic 3 는 사전 계산된 `.json` 보이스 스타일만 사용한다.
> 무작위 오디오에서 직접 음성을 학습하는 경로는 아직 노출돼 있지 않다.

## 출력 형식

| 항목       | 값                   |
| ---------- | -------------------- |
| 샘플레이트 | 44,100 Hz            |
| 채널       | mono                 |
| 샘플 포맷  | 16-bit PCM (`int16`) |
| 컨테이너   | WAV                  |

> 기존 `integration/tts_local.LocalTTS.synthesize()` 의 16kHz 와는 **다르다**.
> 다운스트림이 형식을 가정한다면 호출자에서 리샘플링 책임을 진다.
