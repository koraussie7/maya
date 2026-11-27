"""AirLLM 어댑터 단위 테스트.

목적:
  - 패키지 미설치 시 ERROR WSMessage yield 후 깔끔히 종료
  - 정상 추론 시 ASSISTANT_DELTA 청크 + ASSISTANT_FINAL 종료
  - 모델 로드/생성 실패 시 ERROR WSMessage yield
  - 환경 변수 우선순위 (MAYA_AIRLLM_MODEL > MAYA_AIRLLM_REPO > AIRLLM_MODEL > 기본)

모킹 전략:
  - `airllm.AutoAirLLMForCausalLM` 과 `transformers.AutoTokenizer` 를 sys.modules
    에 가짜 모듈로 주입 → import 시점 / 호출 시점 모두 가로챌 수 있다.
  - 모든 테스트는 네트워크/디스크/GPU 없이 동작한다.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from typing import Any

import pytest

from app.protocol import ServerMsgType


# ---------------------------------------------------------------------------
# 가짜 모듈 (airllm + transformers)
# ---------------------------------------------------------------------------


class _FakeTensor:
    """numpy/torch .shape 호환을 위한 최소 스탠드인 (list-of-list 래퍼)."""

    def __init__(self, rows):
        self._rows = rows

    @property
    def shape(self):
        return (len(self._rows), len(self._rows[0]))

    def __getitem__(self, idx):
        return self._rows[idx]


class _FakeTokenizer:
    def __init__(self, decoded: str = "안녕하세요") -> None:
        self._decoded = decoded

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return "<|user|>테스트"

    def __call__(self, prompt, return_tensors=None):
        # 실제 transformers 의 BatchEncoding 처럼 .shape 를 가진 객체를 리턴
        return {"input_ids": _FakeTensor([[1, 2, 3]])}

    def decode(self, ids, skip_special_tokens=True):
        return self._decoded


class _FakeModel:
    def __init__(self, decoded: str = "안녕하세요", fail: bool = False) -> None:
        self._decoded = decoded
        self._fail = fail
        self.generate_calls = 0

    @classmethod
    def from_pretrained(cls, model_name, compression_config=None):
        if cls._fail_cls:  # type: ignore[attr-defined]
            raise RuntimeError("load failed")
        return cls(fail=cls._fail_gen_cls)  # type: ignore[attr-defined]

    def generate(self, **kwargs):
        self.generate_calls += 1
        if self._fail:
            raise RuntimeError("gen failed")
        # 입력 길이 3 → 출력 길이 6 → 새로 생성된 토큰 3개
        return [[1, 2, 3, 100, 101, 102]]


# 모듈 레벨에 attach / detach 하는 헬퍼


def _install_fake_modules(decoded: str = "안녕하세요", fail_load: bool = False, fail_gen: bool = False):
    """airllm, transformers 가짜 모듈을 sys.modules 에 주입."""
    fake_airllm = types.ModuleType("airllm")

    class FakeAutoAirLLMForCausalLM(_FakeModel):
        _fail_cls = fail_load
        _fail_gen_cls = fail_gen

    fake_airllm.AutoAirLLMForCausalLM = FakeAutoAirLLMForCausalLM  # type: ignore[attr-defined]

    class _FakeAutoTokenizer:
        @classmethod
        def from_pretrained(cls, model_name):
            return _FakeTokenizer(decoded=decoded)

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoTokenizer = _FakeAutoTokenizer  # type: ignore[attr-defined]

    installed = {}
    for name, mod in [("airllm", fake_airllm), ("transformers", fake_transformers)]:
        if name in sys.modules:
            installed[name] = sys.modules[name]
        sys.modules[name] = mod
    return installed, fail_gen


def _restore_modules(saved: dict[str, Any]) -> None:
    for name, mod in saved.items():
        sys.modules[name] = mod
    for name in ("airllm", "transformers"):
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_airllm_chat_import_missing_yields_error(monkeypatch):
    """airllm 이 설치되어 있지 않으면 ERROR 한 번 yield 후 종료."""
    # 패키지 제거
    for name in ("airllm", "transformers"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "airllm", None)  # ImportError 유발

    from app import llm_airllm

    importlib.reload(llm_airllm)

    msgs = []
    async for m in llm_airllm.stream_airllm_chat([{"role": "user", "content": "hi"}]):
        msgs.append(m)
        if m.type == ServerMsgType.ERROR.value:
            break

    assert msgs, "ERROR 메시지가 최소 1번 yield 되어야 함"
    err = msgs[0]
    assert err.type == "error"
    assert err.payload.get("provider") == "airllm"
    assert "airllm" in err.payload.get("message", "")


@pytest.mark.asyncio
async def test_stream_airllm_chat_happy_path(monkeypatch):
    """정상 로드 → 정상 생성 → DELTA + FINAL yield."""
    saved, _ = _install_fake_modules(decoded="안녕하세요 반갑습니다")
    monkeypatch.setenv("MAYA_AIRLLM_MODEL", "fake/test-model")

    from app import llm_airllm

    importlib.reload(llm_airllm)

    msgs = []
    async for m in llm_airllm.stream_airllm_chat(
        [{"role": "user", "content": "테스트"}],
        temperature=0.5,
        max_tokens=64,
    ):
        msgs.append(m)

    _restore_modules(saved)
    importlib.reload(llm_airllm)

    types_ = [m.type for m in msgs]
    # 마지막은 무조건 FINAL
    assert types_[-1] == ServerMsgType.ASSISTANT_FINAL.value, msgs
    # FINAL 이전에 DELTA 최소 1개
    assert ServerMsgType.ASSISTANT_DELTA.value in types_[:-1], msgs
    # 합쳐서 원문 복원 가능
    reconstructed = "".join(m.payload["text"] for m in msgs[:-1])
    assert "안녕하세요" in reconstructed
    assert msgs[-1].payload["text"] == "안녕하세요 반갑습니다"


@pytest.mark.asyncio
async def test_stream_airllm_chat_load_failure(monkeypatch):
    """모델 로드 실패 시 ERROR yield 후 종료."""
    saved, _ = _install_fake_modules(fail_load=True)
    monkeypatch.setenv("MAYA_AIRLLM_MODEL", "fake/load-fails")

    from app import llm_airllm

    importlib.reload(llm_airllm)

    msgs = []
    async for m in llm_airllm.stream_airllm_chat([{"role": "user", "content": "x"}]):
        msgs.append(m)
        if m.type == ServerMsgType.ERROR.value:
            break

    _restore_modules(saved)
    importlib.reload(llm_airllm)

    assert msgs[0].type == "error"
    assert msgs[0].payload.get("provider") == "airllm"
    assert "로드" in msgs[0].payload.get("message", "")


@pytest.mark.asyncio
async def test_stream_airllm_chat_generate_failure(monkeypatch):
    """생성 실패 시 ERROR yield 후 종료."""
    saved, _ = _install_fake_modules(fail_gen=True)
    monkeypatch.setenv("MAYA_AIRLLM_MODEL", "fake/gen-fails")

    from app import llm_airllm

    importlib.reload(llm_airllm)

    msgs = []
    async for m in llm_airllm.stream_airllm_chat([{"role": "user", "content": "x"}]):
        msgs.append(m)
        if m.type == ServerMsgType.ERROR.value:
            break

    _restore_modules(saved)
    importlib.reload(llm_airllm)

    assert msgs[0].type == "error"
    assert msgs[0].payload.get("provider") == "airllm"
    assert "생성" in msgs[0].payload.get("message", "")


def test_airllm_model_env_priority(monkeypatch):
    """환경 변수 우선순위: MAYA_AIRLLM_MODEL > MAYA_AIRLLM_REPO > AIRLLM_MODEL > 기본값."""
    from app import llm_airllm

    importlib.reload(llm_airllm)

    monkeypatch.delenv("MAYA_AIRLLM_MODEL", raising=False)
    monkeypatch.delenv("MAYA_AIRLLM_REPO", raising=False)
    monkeypatch.delenv("AIRLLM_MODEL", raising=False)
    assert "Qwen" in llm_airllm._airllm_model()

    monkeypatch.setenv("AIRLLM_MODEL", "x/A")
    assert llm_airllm._airllm_model() == "x/A"

    monkeypatch.setenv("MAYA_AIRLLM_REPO", "y/B")
    assert llm_airllm._airllm_model() == "y/B"

    monkeypatch.setenv("MAYA_AIRLLM_MODEL", "z/C")
    assert llm_airllm._airllm_model() == "z/C"


# pytest-asyncio 가 없는 환경을 위한 fallback (직접 asyncio.run)
def _maybe_pytest_asyncio():
    try:
        import pytest_asyncio  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":  # pragma: no cover
    if not _maybe_pytest_asyncio():
        # pytest-asyncio 미설치: asyncio 코루틴을 직접 실행
        async def _main():
            # 환경 변수 일관성을 위해 monkeypatch 없이 실행 → 수동 setenv
            for case in ("import_missing", "happy", "load_fail", "gen_fail"):
                if case == "import_missing":
                    sys.modules["airllm"] = None
                    from app import llm_airllm as la

                    importlib.reload(la)
                    async for m in la.stream_airllm_chat([{"role": "user", "content": "hi"}]):
                        print("IMPORT_MISSING:", m.type, m.payload)
                        break
                else:
                    saved, fail_gen = _install_fake_modules(
                        decoded="hi",
                        fail_load=(case == "load_fail"),
                        fail_gen=(case == "gen_fail"),
                    )
                    from app import llm_airllm as la

                    importlib.reload(la)
                    async for m in la.stream_airllm_chat([{"role": "user", "content": "x"}]):
                        print(f"{case.upper()}:", m.type, m.payload)
                        if m.type == "error":
                            break
                    _restore_modules(saved)
                    importlib.reload(la)

        asyncio.run(_main())
