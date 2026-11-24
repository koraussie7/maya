"""OpenAI 호환(OpenRouter 등) LLM 스트리밍 클라이언트."""

from __future__ import annotations

import json
import logging
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
    """LLM 토큰 스트림을 WSMessage 스트림으로 변환."""
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
    # 모델이 사고(think) 블록을 내려고 하면 첫 토큰이 수십 초 늦어진다(EXAONE-4 실측).
    # 지금 기본값(auto/best-fast)은 무료 제공자 라우팅이라, 사고 모델이 잡혔을 때만
    # 해당 설정이 의미를 갖는다 — 지원하지 않는 백엔드는 무시한다.
    # ⚠️ private(자체 게이트웨이)·ollama 차이: ollama 는 모르는 필드를 400 으로 거절하므로
    #    private 일 때만 보낸다(local 경로에는 넣지 않는다).
    if SETTINGS.llm_provider == "private":
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
                        payload={"message": "LLM 응답이 비어 있음",
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
