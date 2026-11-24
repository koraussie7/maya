"""vm_ko_* 스크립트의 **공통 OpenAI 호환 게이트웨이 환경 표준화**.

배경
- 2026-11-24 실측: `OPENAI_MODEL` 미지정 시 기본값 `gpt-4o-mini` 가 로컬 게이트웨이
  (ollama)에는 존재하지 않아 매 저장마다 `404 model 'gpt-4o-mini' not found` 가
  나면서 수십 초를 잡아먹는다 — 그래서 명시 지정으로 바꿈.
- 이후 다른 스크립트 (vm_ko_model_compare, vm_probe_extract 등) 에서도 같은
  패턴이 반복돼 매번 실측 시 함정을 다시 만났다. **재발 방지**가 이 모듈의 목적.

규약
- *한 번만* `setup_openai_env(model=...)` 를 호출하면:
    OPENAI_BASE_URL  ← 기본 http://127.0.0.1:11434/v1
    OPENAI_API_KEY   ← 기본 "ollama"
    OPENAI_MODEL     ← **반드시 명시**(없으면 RuntimeError — 의도적 fail-fast)
- *모델이 비어 있으면 기본값을 박지 않는다*. 기본값 박으면 404 함정이 다시 생긴다.

사용
    from vm_ko_common import setup_openai_env
    setup_openai_env(model="qwen2.5:3b")      # 기본 게이트웨이
    setup_openai_env(model="gemma2:9b",
                     base_url="http://10.0.0.7:11434/v1")   # 게이트웨이 명시

주의
- 모델 명을 *비교* 할 때는 모델을 명시하는 대신 `setup_openai_env()` 를 호출하지
  않고 직접 `OPENAI_MODEL` 을 루프에서 바꿔도 좋다 (예: vm_ko_model_compare).
  다만 그 경우에도 다른 환경 변수 (BASE_URL, API_KEY) 는 이 함수를 통해 박는다.
"""

from __future__ import annotations

import os
from typing import Optional


DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_API_KEY = "ollama"


class OpenAIEnvError(RuntimeError):
    """환경 변수가 잘못 설정됐을 때 — 404 함정 재발 방지용 fail-fast."""


def setup_openai_env(
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    overwrite: bool = False,
) -> dict:
    """OpenAI 호환 게이트웨이용 환경 변수를 표준화한다.

    Parameters
    ----------
    model : str, optional
        `OPENAI_MODEL` 에 박을 값. **반드시 명시**(또는 이미 환경에 있을 것).
        None 이면서 환경에도 없으면 즉시 `OpenAIEnvError`.
    base_url, api_key : str, optional
        게이트웨이 / 더미 키. 주지 않으면 모듈 기본값을 `setdefault` 로 박는다.
    overwrite : bool
        True 면 이미 환경에 있는 값도 덮어쓴다. (비교 스크립트에서만 의미 있음.)

    Returns
    -------
    dict
        실제로 박힌 값 (디버깅·실측 보고용).
    """
    if model is None and "OPENAI_MODEL" not in os.environ:
        raise OpenAIEnvError(
            "OPENAI_MODEL 이 지정되지 않았습니다. 기본값 `gpt-4o-mini` 는 "
            "로컬 ollama 게이트웨이에 없어 매 호출마다 404 + 수십 초 지연이 "
            "발생합니다. setup_openai_env(model=...) 로 명시하세요."
        )

    applied: dict = {}
    if base_url is None:
        base_url = DEFAULT_BASE_URL
    if api_key is None:
        api_key = DEFAULT_API_KEY

    if overwrite or "OPENAI_BASE_URL" not in os.environ:
        os.environ["OPENAI_BASE_URL"] = base_url
        applied["OPENAI_BASE_URL"] = base_url
    if overwrite or "OPENAI_API_KEY" not in os.environ:
        os.environ["OPENAI_API_KEY"] = api_key
        applied["OPENAI_API_KEY"] = api_key

    if model is not None:
        if overwrite or "OPENAI_MODEL" not in os.environ:
            os.environ["OPENAI_MODEL"] = model
            applied["OPENAI_MODEL"] = model

    applied.setdefault("OPENAI_BASE_URL", os.environ.get("OPENAI_BASE_URL"))
    applied.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY"))
    applied.setdefault("OPENAI_MODEL", os.environ.get("OPENAI_MODEL"))
    return applied


def openai_client(timeout: float = 60.0):
    """OpenAI 호환 클라이언트 팩토리. 환경 변수는 `setup_openai_env` 로 박혀 있어야 한다."""
    from openai import OpenAI

    base = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    key = os.environ.get("OPENAI_API_KEY", DEFAULT_API_KEY)
    return OpenAI(api_key=key, base_url=base, timeout=timeout, max_retries=0)
