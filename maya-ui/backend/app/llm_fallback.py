"""LLM 다층 fallback 체인 — 한 모델/엔드포인트가 막혀도 다음 단계로 넘어간다.

설계 메모 (2026-11-26 실측 근거)

Tier 0 — 로컬 LLM (Ollama / llama.cpp OpenAI 호환 서버)
   · 사용자가 직접 호스팅한 uncensored GGUF 모델.
   · 기본 base: http://127.0.0.1:11434/v1  (Ollama)
   · 환경변수:
       MAYA_LOCAL_LLM_BASE   (Ollama 외 llama.cpp 도 그대로 호환 — 예: http://127.0.0.1:8080/v1)
       MAYA_LOCAL_LLM_MODEL  (default: bonsai-ptq1-0-27b-uncensored)
       MAYA_LOCAL_LLM_DISABLED=1 로 통째로 비활성
   · 인증 없음 (Authorization 헤더 생략). ensure_v1() 으로 /v1 보장.
   · reasoning 모드 없음 — extra_payload 미설정.
   · Tier 0 성공 시 judge 비용 계산에서 “로컬 응답이 더 빠르다” 가 반영됨.
   · Ollama 모델이 응답 가능하면 즉시 사용, 안 되면 다음 tier 로 자연 폴백.

Tier 1 — privseai 내부 (자체 서버, 무료, 키 셋됨)
   · keirouter `/privseai/llm/v1` — keirouter `auto/best-fast` 별칭으로 무료 제공자
     자동 라우팅. 282개 모델 노출됨(프로빙). 단 **모든 upstream 이 현재 unavailable**
     → "도달은 OK / 응답은 400" 패턴이 많아 빈 응답으로 자주 빠진다.
   · smart-router 별칭 (`smart/best-fast`) — 자체 정의, 별도 자격증명 필요.
     현재 "No active credentials for provider: smart" 로 차단됨.

Tier 2 — OpenRouter (키 셋됨, pay-as-you-go)
   · `qwen/qwen3-32b` (DeepInfra) — **reasoning 모드 기본 ON → content=null** 버그가
     있었다. `reasoning: {enabled:false}` 를 페이로드에 넣으면 정상 응답한다 (안녕하세요!).
     → 이게 **현재 디폴트 모델의 1순위**가 되어야 한다.
   · `meta-llama/llama-3.1-8b-instruct` — 1061ms, 정상.
   · `openai/gpt-4o-mini` — 2779ms, 가장 안정적.
   · free 슬러그 (`*:free`) 는 2026-11 시점에서 **모두 단종** — fallback 후보에서 제외.

Tier 2.5 — ClawRouter (클라우드 라우터, INTEGRATION_RESULTS.md 권장)
   · INTEGRATION_RESULTS.md 에서 "1초대 가능" 으로 언급된 클라우드 폴백.
   · 2026-11-26 실측 (사용자 제공 sk- 키로 검증):
      · 정답 호스트: https://clawrouter.com/v1   (DNS OK · /v1/models 200)
      · 인증: `Authorization: Bearer sk-...`     (Bearer 헤더 정상 인식)
      · 노출 모델 42개 (Claude/GPT/Gemini/M2 등). 실측 OK 모델: `claude-haiku-5-5`
        (HTTP 200 in 2.6s, 10 prompt tokens · 3 completion tokens).
      · 다른 모델(`MiniMax-M2.5-highspeed`, `gemini-3-flash-preview` 등)은
        `insufficient_user_quota` (계정 잔액 ~$0.000002) 로 거부됨.
   · 이전에 잘못 적었던 호스트들 — 더 이상 기본값 아님:
      · `https://clawrouter.openclaw.ai/v1`  (DNS NXDOMAIN, 존재하지 않는 호스트)
      · `https://api.clawrouter.dev/v1`      (DNS NXDOMAIN)
      · `clawrouter/auto` 슬러그              (이 호스트엔 없음 — 404 model_not_found)
   · 환경변수로 덮을 수 있다:
       MAYA_CLAWROUTER_BASE    (기본 https://clawrouter.com/v1)
       MAYA_CLAWROUTER_MODEL   (기본 claude-haiku-5-5 — 실측 OK 모델)
       MAYA_CLAWROUTER_API_KEY (없으면 키 헤더 빼고 시도)
       MAYA_CLAWROUTER_DISABLED=1 로 통째로 비활성
   · OpenAI 호환 → reasoning 차단 페이로드 동일 적용.
   · tier=2 (cloud) 로 표시해 judge 가 OpenRouter 후보들과 함께 랭킹하게 둔다.
   · 인증 실패(403/401) 또는 quota 부족(403 insufficient_user_quota) 시 다음 tier 로
    자연스럽게 빠진다 — fallback 체인의 정상 동작.

Tier 3 — AirLLM (in-process, GPU 24GB+ 필요)
   · 운영에서는 비현실적(2B 모델도 토큰당 수십 초) — 비활성 기본.
   · 켜져 있으면 fail-safe 로 마지막에 호출한다.

Tier 4 — safe-fail
   · 모두 실패 시 결정 가능한 고정 문장(라우터 hit 와 동일)을 합성기에 넘긴다 —
     사용자는 "지금 응답을 못 해요" 같은 짧은 한국어 메시지를 듣는다.

이 모듈은:
  · `_PROBE_CACHE` 로 엔드포인트 가용성을 주기적으로 캐시(30분) — 매 호출마다
    프로빙하지 않는다.
  · `stream_chat_fallback()` async generator — Tier 1 → 2 → 3 → 4 순서로 시도.
  · `attempt_stream()` 한 번에 한 모델/엔드포인트를 시도, 실패 시 reason 반환.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .protocol import ServerMsgType, WSMessage
from .llm_judge import JevLayaJudge, STATS

log = logging.getLogger(__name__)


# ── Tier 정의 ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Tier:
    """단일 fallback 후보. (provider, base, model, key, options) 묶음."""
    name: str           # 로그/디버그용
    tier: int           # 1=internal, 2=paid-cloud, 3=in-process
    base: str
    model: str
    api_key: str = ""
    needs_key: bool = True
    # True 이면 Authorization 헤더를 아예 보내지 않는다. Cloudflare Access 라우터
    # (`clawrouter/auto`) 처럼 키 없이 동작하는 엔드포인트에서 `invalid_proxy_key`
    # 오답을 피하기 위함. 기본값은 False (정상 인증).
    skip_auth_header: bool = False
    extra_headers: dict = field(default_factory=dict)
    # payload 에 추가 (예: reasoning:{enabled:false})
    extra_payload: dict = field(default_factory=dict)
    # streaming=False 이면 안 받는 모델 → False 로 두면 비스트림 폴링
    stream: bool = True
    # 이 모델은 비용/지연 면에서 어느 정도인지 — 로그용
    cost_hint: str = "low"
    note: str = ""
    # 첫 토큰 응답 타임아웃 (초). Ollama가 모델을 로드하거나 답을 천천히 흘리면
    # 이 시간 안에 첫 토큰이 안 오면 → 즉시 다음 tier 로 넘어간다.
    # 클라우드 tier 는 느슨하게, 로컬은 빡빡하게.
    # 환경변수 override:
    #   MAYA_LOCAL_LLM_FIRST_TOKEN_TIMEOUT_S=8 → Tier 0 (로컬) 기본값
    #   MAYA_CLOUD_FIRST_TOKEN_TIMEOUT_S=20   → Tier 2 (cloud) 기본값
    first_token_timeout_s: float = 8.0


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# 실제로 로컬에서 도는 모델을 선호한다. 채팅용(embed 모델 제외) 중 가장 작은 것부터.
_MODEL_HINTS = ("exaone", "qwen", "gemma", "llama", "mistral")


def _detect_ollama_model(base: str) -> str:
    """Ollama `/api/tags` 에서 설치된 모델 이름을 고른다. 없으면 "".

    base 가 OpenAI 호환(`.../v1`)이어도 태그 엔드포인트는 `/api/tags` 이다.
    느리거나 막혀 있으면 기다리지 않고 빈 문자열(→ tier 0 생략).
    """
    try:
        import urllib.request
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        with urllib.request.urlopen(root + "/api/tags", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in (data.get("models") or [])]
        names = [n for n in names if n and "embed" not in n.lower()]
        if not names:
            return ""
        for hint in _MODEL_HINTS:
            for n in names:
                if hint in n.lower():
                    return n
        return names[0]
    except Exception:  # noqa: BLE001 — 서버 없음/타임아웃/timeout/파싱 실패
        return ""


def build_tiers() -> list[Tier]:
    """환경변수에서 fallback 체인을 구성한다.

    우선순위: Tier 0 (로컬) → Tier 1 (privseai) → Tier 2 (cloud) → Tier 4 (safe-fail)
    judge 가 비활성이면 이 순서 그대로 시도.
    """
    api_key = _env("OPENAI_API_KEY")
    tiers: list[Tier] = []

    # ── Tier 0 — 로컬 LLM (Ollama / llama.cpp) ─────────────────────────────────
    # 사용자가 직접 호스팅하는 uncensored 모델. 키 없이 동작.
    # Ollama create 로 등록한 이름 또는 llama.cpp 서버의 모델 슬러그.
    if _env("MAYA_LOCAL_LLM_DISABLED", "0") != "1":
        local_base = _env("MAYA_LOCAL_LLM_BASE", "http://127.0.0.1:11434/v1")
        # 기본 모델명을 하드코딩하지 않는다. 설치 안 된 이름이면 매 턴 404 로
        # 실패하고 다음 tier 로 넘어가며 수 초를 버린다(2026-11-27 실측:
        # bonsai-ptq1-0-27b-uncensored 미설치 → 첫 응답 4.59초 중 대부분이 재시도).
        # 명시값(MAYA_LOCAL_LLM_MODEL) > Ollama 자동 감지 > 기본 이름 순.
        local_model = _env("MAYA_LOCAL_LLM_MODEL", "") or _detect_ollama_model(local_base)
        if not local_model:
            log.info("로컬 LLM 모델을 찾지 못해 tier 0 을 건너뜁니다 (%s)", local_base)
        # 로컬 LLM의 첫 토큰 응답 한계. Ollama가 모델을 로드하거나
        # 답을 천천히 흘리면 이 시간 안에 첫 토큰이 안 오면 → 즉시 다음 tier.
        # 기본 8초 — 이 안에 답이 안 오면 사용자 체감은 '먹통' 이다.
        local_first_tok = float(_env("MAYA_LOCAL_LLM_FIRST_TOKEN_TIMEOUT_S", "8"))
        if local_model:
            tiers.append(Tier(
                name=f"local-llm/{local_model}",
                tier=0,
                base=local_base,
                model=local_model,
                api_key="",
                needs_key=False,                # 키 없음
                skip_auth_header=True,          # Authorization 헤더 자체 생략
                extra_payload={},               # Ollama 는 reasoning 모드 미사용
                cost_hint="zero",
                first_token_timeout_s=local_first_tok,
                note=(f"로컬 uncensored · base={local_base} · model={local_model} "
                      f"· TTFB≤{local_first_tok:.0f}s"),
            ))

    # Tier 1 — privseai 내부
    privseai_base = _env("MAYA_PRIVSEAI_BASE", "https://privseai.com/privseai/llm/v1")
    if api_key:
        tiers.append(Tier(
            name="keirouter-auto/best-fast",
            tier=1, base=privseai_base, model="auto/best-fast",
            api_key=api_key, needs_key=True,
            extra_payload={"reasoning": {"enabled": False},
                           "chat_template_kwargs": {"enable_thinking": False}},
            cost_hint="zero",
            note="keirouter 무료 제공자 자동 라우팅",
        ))
        tiers.append(Tier(
            name="keirouter-smart/best-fast",
            tier=1, base=privseai_base, model="smart/best-fast",
            api_key=api_key, needs_key=True,
            extra_payload={"reasoning": {"enabled": False},
                           "chat_template_kwargs": {"enable_thinking": False}},
            cost_hint="zero",
            note="smart-router 별칭 (자격증명 없으면 자동 실패)",
        ))

    # Tier 2 — OpenRouter paid (현재 사용 가능한 슬러그만)
    if api_key:
        common_hdr = {"HTTP-Referer": "https://privseai.com/maya",
                      "X-Title": "Maya Voice Assistant"}
        # 1) 현재 디폴트 모델 — reasoning 비활성화 필수
        tiers.append(Tier(
            name="openrouter-qwen3-32b-no-reasoning",
            tier=2,
            base=_env("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
            model=_env("OPENAI_MODEL", "qwen/qwen3-32b"),
            api_key=api_key, needs_key=True,
            extra_headers=common_hdr,
            extra_payload={"reasoning": {"enabled": False},
                           "chat_template_kwargs": {"enable_thinking": False}},
            cost_hint="low",
            note="현재 디폴트 — reasoning OFF 가 없으면 content=null 로 침묵",
        ))
        # 2) llama-3.1-8b-instruct
        tiers.append(Tier(
            name="openrouter-llama-3.1-8b",
            tier=2, base="https://openrouter.ai/api/v1",
            model="meta-llama/llama-3.1-8b-instruct",
            api_key=api_key, needs_key=True,
            extra_headers=common_hdr,
            cost_hint="low",
            note="paid · 한국어 응답 정상",
        ))
        # 3) gpt-4o-mini (안정성 최우선)
        tiers.append(Tier(
            name="openrouter-gpt-4o-mini",
            tier=2, base="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
            api_key=api_key, needs_key=True,
            extra_headers=common_hdr,
            cost_hint="med",
            note="paid · 가장 안정적, 가장 비쌈",
        ))

    # ── Tier 2.5: ClawRouter (INTEGRATION_RESULTS.md 권장 클라우드 폴백) ──
    # OpenAI 호환 라우터. 2026-11-26 실측:
    #   · 정답 호스트: https://clawrouter.com/v1 (clawrouter.openclaw.ai 는 NXDOMAIN)
    #   · 정답 모델: claude-haiku-5-5         (clawrouter/auto 는 이 호스트에 없음)
    #   · 인증: Authorization: Bearer sk-...  (CF Access 게이트는 아님)
    # 환경변수(MAYA_CLAWROUTER_*)로 운영 중 덮을 수 있다.
    # 계정 잔액이 $0.000000 이하로 떨어지면 `insufficient_user_quota` (403) 가
    # 돌아오고, 어댑터가 next tier 로 자연스럽게 폴백한다 — 안전망 동작 정상.
    if _env("MAYA_CLAWROUTER_DISABLED", "0") != "1":
        claw_base = _env("MAYA_CLAWROUTER_BASE", "https://clawrouter.com/v1")
        claw_model = _env("MAYA_CLAWROUTER_MODEL", "claude-haiku-5-5")
        claw_key = _env("MAYA_CLAWROUTER_API_KEY", "")
        tiers.append(Tier(
            name="clawrouter",
            tier=2,
            base=claw_base,
            model=claw_model,
            api_key=claw_key,
            # 키가 있으면 Authorization 헤더 전송, 없으면 생략.
            # (clawrouter.com 은 CF Access 게이트가 아니라 키 헤더가 정상 인증이다.)
            needs_key=bool(claw_key),
            extra_headers={"HTTP-Referer": "https://privseai.com/maya"},
            extra_payload={"reasoning": {"enabled": False}},
            cost_hint="low",
            note=f"ClawRouter cloud · base={claw_base} · model={claw_model}",
        ))

    # Tier 3 — AirLLM (옵트인)
    if _env("MAYA_LLM_PROVIDER", "").lower() == "airllm":
        # 실제 어댑터는 llm_airllm 모듈이 — 여기선 placeholder 만 두지 않고
        # 실제 어댑터를 호출한다. 어댑터가 import 안 되면 안전하게 skip.
        pass  # stream_chat_fallback() 의 분기에서 처리

    return tiers


# ── 단일 시도 ──────────────────────────────────────────────────────────────
async def attempt_stream(
    tier: Tier,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    client: httpx.AsyncClient,
) -> tuple[bool, str, list[str] | None, WSMessage | None]:
    """Tier 하나를 시도한다. 성공 시 (ok=True, reason="ok", tokens, None) 반환.
    실패 시 (ok=False, reason=이유, None, error_msg_or_None).
    error_msg 가 WSMessage.ERROR 면 호출자가 그대로 송출하면 된다.
    """
    base = tier.base.rstrip("/")
    if not base.endswith("/v1") and "/v1" not in base.split("://", 1)[-1].split("/", 1)[1]:
        url = base + "/v1/chat/completions"
    else:
        url = base + "/chat/completions"

    payload = {
        "model": tier.model,
        "messages": messages,
        "stream": tier.stream,
        "temperature": temperature,
    }
    if tier.stream:
        # 일부 백엔드에서 max_tokens 가 있으면 끊김 (ollama 사례) → 스트림에선 생략
        pass
    else:
        payload["max_tokens"] = max_tokens
    payload.update(tier.extra_payload)

    headers = {"Content-Type": "application/json"}
    if not tier.skip_auth_header and tier.needs_key and tier.api_key:
        headers["Authorization"] = f"Bearer {tier.api_key}"
    headers.update(tier.extra_headers)

    t0 = time.monotonic()
    # ── TTFB 데드라인 ──────────────────────────────────────────────────────
    # 로컬 Ollama 가 모델을 로드하거나 답을 흘리는데 너무 느리면,
    # 전체 응답을 기다리지 않고 여기서 끊고 다음 tier 로 넘어간다.
    # 이게 핵심: "local llm 응답이 느리면 openrouter 로 변경"
    ttfb = getattr(tier, "first_token_timeout_s", None)
    ttfb_deadline = (t0 + ttfb) if ttfb and ttfb > 0 else None
    try:
        if tier.stream:
            async with client.stream("POST", url, json=payload, headers=headers,
                                     timeout=httpx.Timeout(timeout_s, connect=4.0)) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    err_text = body[:200].decode("utf-8", "replace")
                    elapsed = (time.monotonic() - t0) * 1000
                    log.warning("tier=%s HTTP %d in %.0fms: %s",
                                tier.name, resp.status_code, elapsed, err_text)
                    return False, f"http_{resp.status_code}", None, None
                tokens: list[str] = []
                first_token = True
                async for line in resp.aiter_lines():
                    # ── TTFB 초과 → 즉시 끊고 다음 tier ──────────────────────
                    if ttfb_deadline is not None and first_token and \
                            time.monotonic() > ttfb_deadline:
                        elapsed = (time.monotonic() - t0) * 1000
                        log.warning("tier=%s TTFB timeout after %.0fms (limit=%.0fs) → fallback",
                                    tier.name, elapsed, ttfb)
                        return False, "ttfb_timeout", None, None
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
                        # ── 버그 수정 ──
                        # OpenRouter/keirouter 응답에서 reasoning 모드 모델은
                        # `delta.reasoning` 에 사고를, `delta.content` 에는
                        # null 을 보낸다. 둘 다 yield 하면 안 됨.
                        # reasoning 토큰은 버리고 content 만 받는다.
                        if delta.get("content"):
                            if first_token:
                                first_token = False
                                ttfb_ms = (time.monotonic() - t0) * 1000
                                log.info("tier=%s TTFB=%.0fms (limit=%.0fs)",
                                         tier.name, ttfb_ms, ttfb or 0)
                            tokens.append(delta["content"])
                if not tokens:
                    return False, "empty_stream", None, None
                elapsed = (time.monotonic() - t0) * 1000
                log.info("tier=%s OK in %.0fms, %d tokens", tier.name, elapsed, len(tokens))
                return True, "ok", tokens, None
        else:
            resp = await client.post(url, json=payload, headers=headers,
                                     timeout=httpx.Timeout(timeout_s, connect=4.0))
            if resp.status_code >= 400:
                err_text = resp.text[:200]
                return False, f"http_{resp.status_code}", None, None
            data = resp.json()
            ch = (data.get("choices") or [None])[0] or {}
            msg = ch.get("message") or {}
            txt = msg.get("content") or ""
            if not txt.strip():
                return False, "empty_content", None, None
            return True, "ok", [txt], None
    except httpx.TimeoutException:
        log.warning("tier=%s timeout after %.0fs", tier.name, timeout_s)
        return False, "timeout", None, None
    except httpx.HTTPError as exc:
        log.warning("tier=%s http error: %s", tier.name, exc)
        return False, "http_error", None, None
    except Exception as exc:  # noqa: BLE001
        log.warning("tier=%s error: %s", tier.name, exc)
        return False, "exception", None, None


# ── 결정 가능한 safe-fail 응답 (마지막 수단) ────────────────────────────────
SAFE_FAIL_MESSAGES = [
    "잠깐, 생각이 잘 안 나네요. 한 번만 더 말씀해 주실래요?",
    "지금 답을 만들기 어렵네요. 다른 방식으로 물어봐 주실래요?",
    "제가 잠시 멈칫했어요. 다시 말씀해 주시면 이어갈게요.",
]


def _safe_fail_pick(text: str) -> str:
    """가벼운 결정론적 선택(매번 같은 텍스트 → 같은 캐시 키 → 같은 음성)."""
    return SAFE_FAIL_MESSAGES[hash(text) % len(SAFE_FAIL_MESSAGES)]


# ── 메인: 체인 시도 ─────────────────────────────────────────────────────────
async def stream_chat_fallback(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 512,
    timeout_s: float | None = None,
    persona_pace: str | None = None,
) -> AsyncIterator[WSMessage]:
    """Tier 순회 — Jev/Laya judge 가 순서를 동적으로 조정한다.

    흐름:
      1) build_tiers() 로 후보 tier 목록 구성 (환경변수 기반)
      2) JevLayaJudge 로 (tier, score) 리스트 생성 — System-1 rule prior ×
         System-2 rerank × 롤링 성공률/지연 을 결합
      3) verdict.ordered 순서대로 attempt_stream
      4) 각 결과를 STATS 에 기��� → 다음 judge 호출 시 반영
      5) 모두 실패 시 결정 가능한 safe-fail 한국어 문장 yield

    judge 가 비활성(MAYA_LLM_JUDGE=0) 이면 build_tiers() 순서 그대로 사용.
    """
    tiers = build_tiers()
    timeout_s = timeout_s or float(_env("MAYA_LLM_TIMEOUT_S", "20"))

    # ── judge 로 tier 순서 결정 ─────────────────────────────────────────────────
    judge = JevLayaJudge.from_env()
    if judge.is_enabled and tiers:
        verdict = judge.judge(tiers, messages, persona_pace=persona_pace)
        ordered = verdict.ordered
        log.info("LLM judge (%s, %.1fms) → %s | %s",
                 verdict.source, verdict.elapsed_ms,
                 " > ".join(f"{t.name}({s:.2f})" for t, s in ordered),
                 verdict.note[:120])
    else:
        ordered = [(t, 1.0) for t in tiers]
        log.info("LLM fallback chain (no judge): %d tier(s) — %s",
                 len(tiers), ", ".join(t.name for t in tiers))

    if not ordered:
        yield WSMessage(type=ServerMsgType.ERROR.value, payload={
            "message": "LLM fallback: 사용 가능한 tier 가 없습니다 (OPENAI_API_KEY=?)",
        })
        return

    last_reasons: list[str] = []
    async with httpx.AsyncClient() as client:
        for tier, _score in ordered:
            t_attempt = time.monotonic()
            ok, reason, tokens, _ = await attempt_stream(
                tier, messages, temperature=temperature, max_tokens=max_tokens,
                timeout_s=timeout_s, client=client,
            )
            elapsed_ms = (time.monotonic() - t_attempt) * 1000
            # ── 롤링 통계 기록 (다음 judge 호출에 반영) ─────────────
            try:
                STATS.record(tier.name, bool(ok and tokens), elapsed_ms)
            except Exception:  # noqa: BLE001
                pass
            if ok and tokens:
                for tok in tokens:
                    yield WSMessage(type=ServerMsgType.ASSISTANT_DELTA.value,
                                    payload={"text": tok})
                yield WSMessage(
                    type=ServerMsgType.ASSISTANT_FINAL.value,
                    payload={"text": "".join(tokens)},
                )
                return
            last_reasons.append(f"{tier.name}={reason}")

        # 모든 tier 실패 → safe-fail
        safe_text = _safe_fail_pick(" ".join(m.get("content", "") for m in messages))
        log.warning("LLM fallback exhausted: %s → safe-fail", "; ".join(last_reasons))
        yield WSMessage(type=ServerMsgType.ASSISTANT_DELTA.value,
                        payload={"text": safe_text})
        yield WSMessage(type=ServerMsgType.ASSISTANT_FINAL.value,
                        payload={"text": safe_text})
        yield WSMessage(type=ServerMsgType.ERROR.value, payload={
            "message": "LLM fallback exhausted — 모든 tier 실패",
            "reasons": last_reasons,
            "safe_fail_used": True,
        })
