# 작업사항 보고 — 2026-11-25

**대상**: `maya-2` 저장소 — AirLLM 어댑터(`tests/test_llm_airllm.py`) 검토 + 야간 러너 산출물 + 현재 작업 트리 상태
**작성 시각**: 2026-11-25 (세션 보고)
**검증 방식**: 테스트 실행 결과 / `git status` / 로그 파일 실측 (추정 없음)

---

## 0. 한 줄 요약

| 영역                 | 결론                                                                                | 근거                                   |
| -------------------- | ----------------------------------------------------------------------------------- | -------------------------------------- |
| `test_llm_airllm.py` | **5개 테스트 전부 통과 (0.03s)** — 네트워크·디스크·GPU 없이 동작                    | `pytest -q` → `5 passed`               |
| AirLLM 어댑터        | 코드와 테스트의 계약(ERROR/DELTA/FINAL, env 우선순위)이 **일치**                    | 코드 리뷰                              |
| 야간 러너            | 1~5·6~8 단계 실행됨. **8절 표가 비어 있음**(산출물 목록 미기록)                     | `OVERNIGHT_REPORT_2026-11-25.md:66-70` |
| 작업 트리            | 변경 12건 + 미추적 2건(`llm_airllm.py`, `cv3_clone_bench.py`)이 **아직 커밋 안 됨** | `git status --short`                   |

---

## 1. `test_llm_airllm.py` — 기존 코드 대비 무엇이 달라졌나

### 1.1 이전 상태(문제)

| 항목                    | 이전                                        | 지금                                                              |
| ----------------------- | ------------------------------------------- | ----------------------------------------------------------------- |
| 테스트 개수             | AirLLM 어댑터에 대한 단위 테스트 없음       | **5개**                                                           |
| 모킹 방식               | —                                           | `sys.modules` 주입(`airllm`, `transformers`) + `importlib.reload` |
| 네트워크/모델 필요 여부 | 어댑터를 검증하려면 실제 모델 다운로드 필요 | **불필요**                                                        |
| env 우선순위 검증       | 없음                                        | `MAYA_AIRLLM_MODEL > MAYA_AIRLLM_REPO > AIRLLM_MODEL > 기본`      |

### 1.2 현재 테스트 5건

| #   | 테스트                                                | 검증 내용                                                                                              |
| --- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| 1   | `test_stream_airllm_chat_import_missing_yields_error` | `sys.modules["airllm"] = None` → ImportError → ERROR 1회 yield 후 종료, `payload.provider == "airllm"` |
| 2   | `test_stream_airllm_chat_happy_path`                  | 로드→생성 정상. 마지막은 `assistant.final`, 그 앞에 `assistant.delta` 최소 1개, DELTA 합치면 원문 복원 |
| 3   | `test_stream_airllm_chat_load_failure`                | `from_pretrained` 실패 → ERROR, 메시지에 `"로드"`                                                      |
| 4   | `test_stream_airllm_chat_generate_failure`            | `generate()` 실패 → ERROR, 메시지에 `"생성"`                                                           |
| 5   | `test_airllm_model_env_priority`                      | 기본값에 `Qwen` 포함 → `AIRLLM_MODEL` < `MAYA_AIRLLM_REPO` < `MAYA_AIRLLM_MODEL` 순 override           |

실행 결과:

```
PYTHONPATH=maya-ui/backend .venv-voicemem/bin/python -m pytest \
  maya-ui/backend/tests/test_llm_airllm.py -q
→ 5 passed in 0.03s   (rc=0)
```

### 1.3 테스트 설계에서 짚을 점 (지금 고치지 않음, 기록만)

1. **`monkeypatch` 는 테스트 종료 시 `sys.modules` 를 복원한다.**
   그런데 테스트 본문에서 `_restore_modules(saved)` 와 `importlib.reload(llm_airllm)` 를 **직접 한 번 더** 한다.
   → 동작은 같지만 이중 복원이라, `monkeypatch` 를 `_install_fake_modules` 로 통일하면 더 단순해진다.
2. **`_restore_modules` 는 가짜 모듈을 무조건 `pop` 한다.**
   원래 시스템에 `transformers` 가 설치돼 있던 환경에서는 `saved` 로 복원되지만,
   `saved` 에 없던 이름은 pop 이후 **실제 패키지가 사라진 상태로 남는다**.
   → 테스트 실행 순서에 따라 이후 테스트가 영향을 받을 수 있다(현재 5건은 서로 독립이라 통과).
3. **`_FakeModel.from_pretrained` 는 `cls._fail_cls` 를 참조**하는데, `_FakeModel` 자체에는 이 속성이 없다.
   `FakeAutoAirLLMForCausalLM` 에만 있다 → 어댑터가 `_FakeModel` 을 직접 쓰면 `AttributeError`.
   지금은 항상 서브클래스만 쓰므로 문제 없음.
4. **happy path 의 DELTA 검증이 약하다.** `"안녕하세요" in reconstructed` 만 본다.
   토큰 분할 방식이 바뀌면 놓칠 수 있어, `max_tokens`/`temperature` 가 실제로 전달됐는지(spy) 확인하는 편이 좋다.
5. **`if __name__ == "__main__"` fallback 블록은 pytest 경로에서는 실행되지 않는다.**
   `pytest-asyncio` 가 설치돼 있으므로 실효 코드는 5개 테스트뿐 → 유지/제거 결정 필요(현재 venv엔 설치돼 있어 통과).

---

## 2. AirLLM 어댑터 쪽 미완 (테스트는 통과하지만 기능은 미검증)

| 항목                                | 상태                                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------------------- |
| `maya-ui/backend/app/llm_airllm.py` | **미추적 파일(untracked)** — git 에 아직 없음                                               |
| 실제 모델 로드                      | 미검증 (테스트는 가짜 모듈)                                                                 |
| 스트리밍                            | **가짜**다. `llm_airllm.py:110` 이 전체 시퀀스를 먼저 받은 뒤 `chunk_size=16` 글자로 잘라 DELTA 를 보낸다 |
| docstring 불일치                    | 모듈 docstring(7~8행)은 "토큰 단위로 즉시 yield" 인데 코드(126~127행)는 "한 번에 yield" — 문서가 코드보다 앞서 있음 |
| 모델 캐시                           | 전역 캐시 없음 → 호출마다 `from_pretrained` 재실행                                              |
| 디바이스                            | docstring 은 "맥북 Apple Silicon metal" 전제, 그러나 이 맥은 **Intel x64**이고 `compression_config` 도 bnb 고정(device 인자 없음) |
| 동시 요청                           | 미검증 (모델 단일 인스턴스 전제면 큐 필요)                                                  |
| 첫 토큰 지연                        | 미측정                                                                                      |

→ **"테스트 5 passed" 는 계약(에러 처리·env 우선순위) 검증이지 성능/품질/스트리밍 검증이 아니다.**

### 2.1 코드 ↔ 테스트 정합성 (실제 대조)

| 계약 | 코드 (`llm_airllm.py`) | 테스트 | 일치 |
|------|------------------------|--------|------|
| import 실패 → ERROR 1회 | 50~62행, `provider: airllm` | 테스트 1 | ✅ |
| 로드 실패 → ERROR, "로드" 포함 | 79~89행, `"airllm 모델 로드 실패: {model_name}"` | 테스트 3 | ✅ |
| 생성 실패 → ERROR, "생성" 포함 | 114~124행, `"airllm 생성 실패"` | 테스트 4 | ✅ |
| DELTA 후 FINAL | 128~140행 | 테스트 2 | ✅ |
| env 우선순위 | 29~36행 | 테스트 5 | ✅ |
| temperature / max_tokens 전달 | 100~102행 | ❌ 미검증 | ⚠️ |
| FINAL 텍스트 = DELTA 합 | 같은 `text` 변수 | 테스트 2 는 부분 문자열만 확인 | ⚠️ |

**코드에만 있는 결함 1건**: `tok.apply_chat_template`(91행)·`tok(...)`(94행)이 `try` **밖**에 있다.
토크나이저가 채팅 템플릿을 지원하지 않으면 ERROR 없이 예외가 그대로 전파된다.
테스트는 가짜 토크나이저라 이 경로를 못 잡는다(112행 `inputs["input_ids"].shape[1]` 도 리스트 반환 시 도달 불가).

---

## 3. 야간 러너 산출물 상태

`maya/docs/OVERNIGHT_REPORT_2026-11-25.md` 실측:

- 8절 "생성된 산출물" **표가 헤더만 있고 행이 비어 있음**(66~70행)
- 종료 시각 `07:46:19` 기록됨
- 토큰 검색 항목 요약: **top1 정답 4/4**
- `.overnight/*.log` 6개가 갱신됨(cosyvoice3_tts, gateway_probe, opus_check, piper_bench, tts_engines, vm_ko_raw)

→ 러너는 돌았으나 **산출물 섹션을 채우는 로직이 비어 있다**(스크립트 개선 대상).

---

## 4. 커밋되지 않은 변경 (12건 변경 + 2건 신규)

```
M  .overnight/*.log (6)                # 러너 로그
M  integration/gateway_probe.json
M  integration/vm_ko_raw_memory.json
M  maya-ui/backend/app/config.py
M  maya-ui/backend/app/llm.py
M  maya-ui/backend/app/llm_gateway.py
M  maya/docs/OVERNIGHT_REPORT_2026-11-25.md
M  scripts/overnight_run.sh
?? integration/cv3_clone_bench.py
?? maya-ui/backend/app/llm_airllm.py   ← 테스트 대상 본체
?? maya-ui/backend/tests/test_llm_airllm.py
```

→ 마지막 푸시(`07582b7`, 01:32) 이후 작업분이 **원격에 반영되지 않은 상태**(ahead 0/behind 0 은 로컬 기준 커밋만 의미).

---

## 5. 다음 작업 (우선순위)

| 순위 | 작업                                        | 이유                                              |
| ---- | ------------------------------------------- | ------------------------------------------------- |
| 1    | `llm_airllm.py` + `test_llm_airllm.py` 커밋 | 본체가 untracked 라 원격에서는 테스트가 실행 불가 |
| 2    | 야간 리포트 8절 채우기                      | 리포트가 "무엇이 나왔는지"를 증명하지 못함        |
| 3    | DELTA 를 실제 토큰 스트리밍으로             | 현재는 한 번에 생성 후 분할 → 체감 지연 개선 없음 |
| 4    | AirLLM 실측(첫 토큰, 토큰/초, RSS)          | Intel Mac 에서 실사용 가능 여부 판단              |
| 5    | 테스트 정리(1.3 의 1·2번)                   | 이중 복원 / 모듈 누수 제거                        |
