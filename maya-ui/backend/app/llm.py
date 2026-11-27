"""OpenAI 호환(OpenRouter 등) LLM 스트리밍 클라이언트.

2026-11-26 — 다층 fallback 체인 (llm_fallback.py) 으로 위임.
- 환경변수 `MAYA_LLM_FALLBACK=1` (기본) → `stream_chat_fallback()` 가 Tier 1~4 순회
- `MAYA_LLM_FALLBACK=0` → 기존 단일 프로바이더 경로 (airllm 분기는 그대로 유지)
- 버그 수정: cloud(OpenRouter)에서도 reasoning 모드 차단 — `qwen/qwen3-32b` 같은
  reasoning 기본 ON 모델이 content=null 로 응답하는 문제를 끊는다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

import httpx

from .config import SETTINGS
from .llm_gateway import ensure_v1, needs_api_key
from .protocol import ServerMsgType, WSMessage

log = logging.getLogger(__name__)


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 512,
) -> AsyncIterator[WSMessage]:
    """LLM 토큰 스트림을 WSMessage 스트림으로 변환.

    `MAYA_LLM_FALLBACK=1` (기본) 이면 다층 fallback 체인 (Tier 1 → 4) 으로 위임.
    한 모델/엔드포인트가 200 + content=null (reasoning 모드) 또는 4xx/timeout 이면
    다음 tier 로 즉시 넘어간다. 모든 tier 가 막히면 **결정 가능한 safe-fail**
    한국어 문장을 yield 한다 (라우터 hit 와 같은 결정론적 흐름).
    """
    use_fallback = os.environ.get("MAYA_LLM_FALLBACK", "1").strip() != "0"
    if use_fallback and SETTINGS.llm_provider != "airllm":
        # AirLLM 은 in-process 라서 fallback 이득이 적다. 명시적으로 켠 경우만.
        from .llm_fallback import stream_chat_fallback
        async for msg in stream_chat_fallback(
            messages, temperature=temperature, max_tokens=max_tokens,
            timeout_s=SETTINGS.llm_timeout_s,
        ):
            yield msg
        return

    # ── AirLLM 분기 ─────────────────────────────────────────────────────────
    # 의존성을 옵트인으로 두기 위해 여기서 임포트 — 패키지가 없으면
    # 분기 자체가 안 보이도록 한다(에러 메시지가 명확해진다).
    if SETTINGS.llm_provider == "airllm":
        try:
            from .llm_airllm import stream_airllm_chat  # type: ignore
        except Exception as exc:  # ImportError 등
            yield WSMessage(
                type=ServerMsgType.ERROR.value,
                payload={
                    "message": (
                        "airllm 백엔드를 사용하려면 `pip install airllm` 으로 "
                        "패키지를 설치하세요"
                    ),
                    "detail": str(exc),
                    "provider": "airllm",
                },
            )
            return
        async for msg in stream_airllm_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield msg
        return

    if not SETTINGS.openai_api_key and needs_api_key(SETTINGS.llm_provider, SETTINGS.openai_model):
        yield WSMessage(
            type=ServerMsgType.ERROR.value,
            payload={
                "message": f"OPENAI_API_KEY is not set (provider={SETTINGS.llm_provider})",
                "provider": SETTINGS.llm_provider,
            },
        )
        return

    payload = {
        "model": model or SETTINGS.openai_model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # ── 버그 수정 (2026-11-26) ───────────────────────────────────────────
    # 모델이 사고(think) 블록을 내려고 하면 첫 토큰이 수십 초 늦어지고(EXAONE-4 실측),
    # 더 중요한 건 **content=null** 이라는 점이다. reasoning 모드를 끄지 않으면
    # qwen/qwen3-32b 같은 모델이 HTTP 200 + content=null 을 보내서 UI는 침묵한다.
    # · private(자체 게이트웨이): `reasoning:{enabled:false}` 와
    #   `chat_template_kwargs:{enable_thinking:false}` 둘 다 보낸다.
    # · cloud(OpenRouter): reasoning 차단이 필요하다 — ollama 만 모르는 필드를
    #   거부하므로(400), ollama 경로가 아닐 때 보낸다.
    send_reasoning_off = SETTINGS.llm_provider in ("private", "cloud")
    if send_reasoning_off:
        payload["reasoning"] = {"enabled": False}
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    # 스트리밍 요청에 max_tokens 를 넣으면 일부 백엔드(ollama)가 즉시 끊거나
    # 아예 응답하지 않는다 → 스트림에서는 생략하고, 비스트림 경로만 쓴다.
    if payload.get("stream"):
        payload.pop("max_tokens", None)
    headers = {
        "Authorization": f"Bearer {SETTINGS.openai_api_key}",
        "Content-Type": "application/json",
    }
    # Ollama(OpenAI 호환)는 API 키가 불필요하다 — 빈 Bearer 헤더는 httpx에서
    # `Illegal header value`로 거절된다. 로컬 프로바이더에서는 키 헤더를 빼 보낸다.
    if SETTINGS.llm_provider == "local" and not SETTINGS.openai_api_key:
        headers.pop("Authorization", None)
    if "openrouter" in SETTINGS.openai_base:
        headers["HTTP-Referer"] = "https://privseai.com/maya"
        headers["X-Title"] = "Maya Voice Assistant"

    url = f"{ensure_v1(SETTINGS.openai_base)}/chat/completions"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(SETTINGS.llm_timeout_s, connect=10.0)
    ) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    log.error("LLM HTTP %d (%s/%s): %s", resp.status_code,
                              SETTINGS.llm_provider, payload["model"], body[:500])
                    yield WSMessage(
                        type=ServerMsgType.ERROR.value,
                        payload={
                            "message": f"LLM HTTP {resp.status_code}",
                            "body": body[:500].decode("utf-8", "replace"),
                        },
                    )
                    return
                buf: list[str] = []
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        # reasoning 토큰은 버리고 content 만 받는다
                        # (delta.reasoning 은 사고 과정, 사용자 화면엔 안 보냄)
                        token = delta.get("content") or ""
                        if token:
                            buf.append(token)
                            yield WSMessage(
                                type=ServerMsgType.ASSISTANT_DELTA.value,
                                payload={"text": token},
                            )
                # 토큰이 한 개도 안 오면 사용자는 "멈춘 화면"만 본다. 클라우드 모델 슬러그가
                # 내려가면 200 + 빈 스트림이 오는 경우가 있어 빈 결과를 에러로 알린다.
                if not buf:
                    yield WSMessage(
                        type=ServerMsgType.ERROR.value,
                        payload={"message": "LLM 응답이 비어 있음 (reasoning 모드 가능성)",
                                 "model": payload["model"],
                                 "provider": SETTINGS.llm_provider},
                    )
                    return
                yield WSMessage(
                    type=ServerMsgType.ASSISTANT_FINAL.value,
                    payload={"text": "".join(buf)},
                )
        except httpx.TimeoutException as exc:
            # 기본 20s — 사용자가 5분을 기다리는 일을 막는다(로컬 LLM 실측 사고).
            log.warning("LLM timeout after %.0fs: %s", SETTINGS.llm_timeout_s, exc)
            yield WSMessage(
                type=ServerMsgType.ERROR.value,
                payload={"message": f"LLM 응답 시간 초과({SETTINGS.llm_timeout_s:.0f}s)",
                         "model": payload["model"],
                         "provider": SETTINGS.llm_provider},
            )
        except httpx.HTTPError as exc:
            log.exception("LLM stream error")
            yield WSMessage(
                type=ServerMsgType.ERROR.value,
                payload={"message": f"LLM stream failed: {exc}"},
            )
