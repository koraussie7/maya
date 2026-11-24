"""환경 설정 로더."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class Settings:
    ws_port: int
    allow_origins: tuple[str, ...]
    openai_api_key: str
    openai_base: str
    openai_model: str
    # 대화 경로 프로바이더:
    #   "private"(기본) — 자체 110 서버의 smart-router(keirouter) → https://privseai.com/llm
    #   "cloud"          — OpenRouter (OPENAI_API_KEY 하나로)
    #   "local"          — Ollama (대화 경로 부적합: 실측 근거는 MAYA_VOICE_KO_MODEL_COMPARE.md)
    llm_provider: str
    # 클라우드 응답이 늦거나 실패하면 사용자가 5분을 기다리게 된다 → 짧게 끊고 에러를 준다.
    llm_timeout_s: float
    voxcpm_url: str
    voxcpm_engine: str
    piper_model: str
    piper_config: str
    hf_token: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        # 대화 경로LLM 선택 (2026-11-24 실측): 로컬 LLM 은 대화에 못 쓴다.
        #   qwen2.5-coder 3/9·9~18s, llama3.1:8b 9/9·5분+, gemma3:4b 는 중국어 샘,
        #   EXAONE-4 는 ollama 에서 사고 모드 제어 불가 → MAYA_VOICE_KO_MODEL_COMPARE.md
        # 기본은 **자체 서버(110)의 smart-router**: privseai.com 과 같이 있고, 키 발급/
        # 모델 선택을 서버가 관리한다. 바깥 인터넷은 nginx 의 /llm/ 로만 열린다.
        provider = os.environ.get("MAYA_LLM_PROVIDER", "private").strip().lower()
        if provider == "local":
            base = os.environ.get("OPENAI_BASE", "http://127.0.0.1:11434/v1")
            model = os.environ.get("OPENAI_MODEL", "gemma3:4b")
        elif provider == "cloud":
            base = os.environ.get("OPENAI_BASE", "https://openrouter.ai/api/v1")
            # gemini-2.0-flash-exp 같은 실험 슬러그 대신 정식 슬러그를 기본으로 둔다.
            model = os.environ.get("OPENAI_MODEL", "google/gemini-2.5-flash")
        else:
            provider = "private"
            # nginx 매핑: /privseai/llm/<path> → 127.0.0.1:20128/<path>
            #   /privseai/llm/v1/models → keirouter /v1/models
            #   (서버 실사에서 확인한 경로. /llm/ 만 잡으면 403 이다 — llm.py 는 POST 만 허용)
            base = os.environ.get("OPENAI_BASE", "https://privseai.com/privseai/llm/v1")
            # auto/best-fast: 무료 제공자들을 가용성/지연 순으로 라우팅하는 별칭
            model = os.environ.get("OPENAI_MODEL", "auto/best-fast")
        return cls(
            ws_port=int(os.environ.get("MAYA_WS_PORT", "8765")),
            allow_origins=tuple(
                o.strip()
                for o in os.environ.get(
                    "MAYA_ALLOW_ORIGINS",
                    "https://privseai.com,http://localhost:5173",
                ).split(",")
                if o.strip()
            ),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base=base,
            openai_model=model,
            llm_provider=provider,
            llm_timeout_s=float(os.environ.get("MAYA_LLM_TIMEOUT_S", "20")),
            voxcpm_url=os.environ.get(
                "VOXCPM_API_URL", "https://openbmb-voxcpm-tts.hf.space"
            ),
            voxcpm_engine=os.environ.get("VOXCPM_ENGINE", "voxcpm-api"),
            piper_model=os.environ.get(
                "PIPER_MODEL_PATH", "models/tts/piper/ko_KR-kss-medium.onnx"
            ),
            piper_config=os.environ.get(
                "PIPER_CONFIG_PATH", "models/tts/piper/ko_KR-kss-medium.onnx.json"
            ),
            hf_token=os.environ.get("HF_TOKEN", ""),
            log_level=os.environ.get("MAYA_LOG_LEVEL", "INFO"),
        )


SETTINGS = Settings.from_env()
