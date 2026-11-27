"""AirLLM in-process LLM 어댑터.

2026-11-25 — 통합 플랜 1-3 노드:
  airllm 은 GPU 24GB+ 가 있으면 70B 모델을 4bit 로 in-process 추론한다.
  맥북(Apple Silicon) 에서는 metal 가속을 쓸 수 있어, OpenRouter 가 끊겼을
  때 폴백으로 쓸 수 있다. 다만 110 서버에 GPU 가 없으므로 1 토큰에 수십 초가
  걸리는 점은 동일하게 적용된다. 이 어댑터는 토큰 단위로 즉시 yield 해서
  파이프라인이 막히지 않도록 한다.

설치(옵션):
    pip install airllm

사용:
    MAYA_LLM_PROVIDER=airllm MAYA_AIRLLM_MODEL=unsloth/Llama-3-70B-Instruct-bnb-4bit
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from .protocol import ServerMsgType, WSMessage

log = logging.getLogger(__name__)


def _airllm_model() -> str:
    # .env 의 MAYA_AIRLLM_REPO (config.Settings.airllm_repo) 와 일치
    return (
        os.getenv("MAYA_AIRLLM_MODEL")
        or os.getenv("MAYA_AIRLLM_REPO")
        or os.getenv("AIRLLM_MODEL")
        or "Qwen/Qwen2.5-1.5B-Instruct"
    )


async def stream_airllm_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 512,
) -> AsyncIterator[WSMessage]:
    """airllm 으로 토큰 단위 스트리밍.

    airllm 은 동기 API 이므로 executor 로 스레드에 위임하고, 토큰 단위로
    yield 한다. 이렇게 하면 백엔드 메인 루프는 막히지 않는다.
    """
    try:
        from airllm import AutoAirLLMForCausalLM  # type: ignore
        from transformers import AutoTokenizer  # type: ignore
    except Exception as exc:  # ImportError 등
        yield WSMessage(
            type=ServerMsgType.ERROR.value,
            payload={
                "message": "airllm 패키지를 설치하세요: pip install airllm transformers",
                "detail": str(exc),
                "provider": "airllm",
            },
        )
        return

    model_name = _airllm_model()
    loop = asyncio.get_running_loop()

    def _load() -> tuple[Any, Any]:
        log.info("airllm 로딩: %s", model_name)
        # AirLLM 은 양자화된 모델을 lazy 로딩한다.
        model = AutoAirLLMForCausalLM.from_pretrained(
            model_name,
            compression_config={"provider": "bnb"},
        )
        tok = AutoTokenizer.from_pretrained(model_name)
        return model, tok

    try:
        model, tok = await loop.run_in_executor(None, _load)
    except Exception as exc:
        log.exception("airllm 로드 실패")
        yield WSMessage(
            type=ServerMsgType.ERROR.value,
            payload={
                "message": f"airllm 모델 로드 실패: {model_name}",
                "detail": str(exc),
                "provider": "airllm",
            },
        )
        return

    prompt = tok.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tok(prompt, return_tensors="pt")

    # airllm 의 .generate 는 토큰별 generator 를 받는다 (HuggingFace 호환).
    # 1 토큰씩 yield 해서 파이프라인 응답성을 유지한다.
    def _generate() -> Any:
        return model.generate(
            **inputs,
            max_new_tokens=max_tokens,
            temperature=temperature,
            do_sample=temperature > 0,
            streamer=None,  # 동기식 .generate 가 토큰 id 텐서를 반환
        )

    try:
        # 토큰 단위로 누적하기 위해 두 단계로: 1) 전체 시퀀스 생성
        # 동기 함수를 스레드에서 실행
        out_ids = await loop.run_in_executor(None, _generate)
        # 입력 길이만큼 잘라 새로 생성된 토큰만 추출
        new_tokens = out_ids[0][inputs["input_ids"].shape[1]:]
        text = tok.decode(new_tokens, skip_special_tokens=True)
    except Exception as exc:
        log.exception("airllm 생성 실패")
        yield WSMessage(
            type=ServerMsgType.ERROR.value,
            payload={
                "message": "airllm 생성 실패",
                "detail": str(exc),
                "provider": "airllm",
            },
        )
        return

    # 누적 응답을 한 번에 yield — airllm 은 토큰 스트리밍이 약해서
    # 차라리 청크 단위로 끊어 보낸다(시각적 진행감).
    chunk_size = 16
    for i in range(0, len(text), chunk_size):
        await asyncio.sleep(0)  # 이벤트 루프 양보
        yield WSMessage(
            type=ServerMsgType.ASSISTANT_DELTA.value,
            payload={"text": text[i : i + chunk_size]},
        )

    # 종료 마커
    yield WSMessage(
        type=ServerMsgType.ASSISTANT_FINAL.value,
        payload={"text": text},
    )

    log.info("airllm 응답 완료 (%d chars)", len(text))
