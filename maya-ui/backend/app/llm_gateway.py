"""자체 LLM 게이트웨이(smart-router / keirouter) 설정 헬퍼.

배경 (2026-11-24 서버 실사)
  우리 서버(185.55.240.110)에 이미 두 가지가 떠 있고, privseai.com 도 같은 서버다.
    · /opt/smart-router  (포크, :20180) — 자체 정의 경로(smart/best-fast, smart/gapfill),
      `auto/*` 별칭은 없음.
    · /opt/keirouter     (:20128) — OpenAI 호환 `/v1/models`, `/v1/chat/completions`.
      `auto/best-fast` 같은 **합성 라우팅 별칭**이 여기에만 있다.
  로컬 LLM 은 대화 경로에 못 쓴다(실측: qwen2.5-coder 3/9·9~18s, llama3.1:8b 9/9·5분+,
  gemma3:4b 중국어 샘, EXAONE-4 사고 모드 제어 불가 → maya/docs/MAYA_VOICE_KO_MODEL_COMPARE.md).
  그래서 기본값을 자체 게이트웨이로 두되, **모델 ID 를 설정으로 분리**한다.

키 관리
  keirouter 는 `Authorization: Bearer <api_key>` 를 요구한다(`GET /v1/models` 부터 401).
  발급 키는 서버 `~/.keirouter/keirouter.db` 의 api_keys 테이블에 있다.
  → 키는 **환경변수로만** 넣는다(레포/문서/로그에 절대 쓰지 않는다). 이 파일은 키를 모른다.
"""

from __future__ import annotations

#: 서버 smart-router 의 실제 경로(포함 확인). 자체 정의라 OpenRouter 와 다르다.
SMART_ROUTER_PATHS = ("smart/best-fast", "smart/gapfill")

#: keirouter 의 라우팅 별칭(무료 제공자 가용성/지연 순).
KEIROUTER_ALIASES = ("auto/best-fast", "auto")

#: 외부(nginx)로 공개되어야 하는 경로 prefix. keirouter 기본 포트 20128 은 외부에 열지 않는다.
PUBLIC_PREFIX = "/llm"


def normalize_base_url(base: str) -> str:
    """게이트웨이 base URL 을 `…/v1` 없는 형태로 맞춘다.

    `stream_chat()` 은 `f"{base}/chat/completions"` 를 붙이므로, 사용자가
    `https://privseai.com/llm/v1` 이나 `http://127.0.0.1:20128/v1` 로 넣어도
    `/v1/v1/chat/completions` 가 되지 않게 끝의 `/v1` 만 제거한다.
    """
    b = (base or "").strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[: -len("/v1")]
    return b


def needs_api_key(provider: str, model: str) -> bool:
    """keirouter 는 모델과 무관하게 키가 필요하다. local(ollama) 만 키 없이 동작한다."""
    return provider != "local"


def health_url(base: str) -> str:
    """서버 헬스체크(`/health`) — 배포 후 가장 먼저 확인할 것."""
    return f"{normalize_base_url(base)}/health"


def ensure_v1(base: str) -> str:
    """OpenAI 호환 base 에 `/v1` 을 **보장**한다.

    `normalize_base_url()` 은 `…/v1` 중복만 막고, `/v1` 이 없는 주소는 그대로 둔다.
    그런데 ollama 는 `/v1/chat/completions` 만 받고(`/chat/completions` 는 404),
    OpenRouter·keirouter·자체 게이트웨이는 `/v1` 포함 주소를 쓴다. 운영에서
    `OPENAI_BASE=http://127.0.0.1:11434` 처럼 `/v1` 을 빼고 넣는 사고가 실제로 있었다
    (2026-11-24: 404 page not found). 그래서 **경로가 없으면 붙인다**.
    """
    b = normalize_base_url(base)
    rest = b.split("://", 1)[-1]
    if "/" not in rest:                      # 호스트만: http://127.0.0.1:11434
        return f"{b}/v1"
    if rest.rstrip("/").endswith("/v1"):     # 이미 /v1 포함
        return b
    return f"{b}/v1"                          # /privseai/llm 같은 중간 경로
