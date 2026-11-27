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
    # ── Pocket TTS (Hugging Face Space adapter) ────────────────────────
    # `MAYA_TTS_PROVIDER=pocket` 으로 활성. 기본은 piper (로컬) + voxcpm (원격 폴백).
    tts_provider: str = "piper"  # "piper" | "voxcpm" | "pocket"
    tts_voice: str = "default"   # pocket: default | male | clone
    pocket_tts_url: str = "https://pupunpu-jev-interaction.hf.space/gradio_api/call/pocket_tts"
    pocket_tts_timeout_s: float = 30.0
    # ── RAG / Knowledge (Agent 1) ─────────────────────────────────────
    # RAG 블록 — MAYA_RAG_ENABLED=0 이면 모듈이 no-op
    ma_rag_enabled: int = 0  # 기본값 0 (OFF)
    ma_rag_data_dir: str = "~/.cache/maya/rag"
    ma_rag_embed_model: str = "BAAI/bge-m3"
    ma_rag_chunk_tokens: int = 400
    ma_rag_chunk_overlap: int = 80
    ma_rag_topk_vec: int = 20
    ma_rag_topk_final: int = 5
    ma_rag_graph_enabled: int = 1  # 엔티/커뮤니티 그래프 사용 여부
    # ── AirLLM (옵트인 로컬 LLM) ───────────────────────────────────────
    # MAYA_LLM_PROVIDER=airllm 일 때만 활성. 기본은 꺼져 있다 (대화 경로 비권장).
    airllm_repo: str = "Qwen/Qwen2.5-1.5B-Instruct"
    airllm_cache_dir: str = "~/.cache/maya/airllm"
    airllm_compression: str = "4bit"
    airllm_max_new_tokens: int = 256
    airllm_dtype: str = "float16"
    airllm_device: str = "mps"

    @classmethod
    def from_env(cls) -> "Settings":
        # 대화 경로LLM 선택 (2026-11-24 실측): 로컬 LLM 은 대화에 못 쓴다.
        #   qwen2.5-coder 3/9·9~18s, llama3.1:8b 9/9·5분+, gemma3:4b 는 중국어 샘,
        #   EXAONE-4 는 ollama 에서 사고 모드 제어 불가 → MAYA_VOICE_KO_MODEL_COMPARE.md
        # 기본은 **자체 서버(110)의 smart-router**: privseai.com 과 같이 있고, 키 발급/
        # 모델 선택을 서버가 관리한다. 바깥 인터넷은 nginx 의 /llm/ 로만 열린다.
        provider = os.environ.get("MAYA_LLM_PROVIDER", "private").strip().lower()
        if provider == "airllm":
            # airllm 은 in-process 로컬 추론이라 base URL 이 의미 없다.
            # model 슬롯은 airllm_repo 와 별개로 “대화 모델 별칭” 으로만 쓴다.
            base = os.environ.get("OPENAI_BASE", "local://airllm")
            model = os.environ.get("OPENAI_MODEL", "airllm-local")
        elif provider == "local":
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
            # Pocket TTS 활성: MAYA_TTS_PROVIDER=pocket 또는 MAYA_TTS_POCKET=1
            tts_provider=os.environ.get(
                "MAYA_TTS_PROVIDER",
                "pocket" if os.environ.get("MAYA_TTS_POCKET", "").strip() == "1" else "piper",
            ).strip().lower(),
            tts_voice=os.environ.get("MAYA_TTS_VOICE", "default").strip().lower(),
            pocket_tts_url=os.environ.get(
                "MAYA_POCKET_TTS_URL",
                "https://pupunpu-jev-interaction.hf.space/gradio_api/call/pocket_tts",
            ),
            pocket_tts_timeout_s=float(
                os.environ.get("MAYA_POCKET_TTS_TIMEOUT_S", "30")
            ),
            # AirLLM 은 디폴트로 꺼져 있다. 켜려면 MAYA_LLM_PROVIDER=airllm.
            airllm_repo=os.environ.get(
                "AIRLLM_REPO", "Qwen/Qwen2.5-1.5B-Instruct"
            ),
            airllm_cache_dir=os.environ.get(
                "AIRLLM_CACHE_DIR", "~/.cache/maya/airllm"
            ),
            airllm_compression=os.environ.get(
                "AIRLLM_COMPRESSION", "4bit"
            ),
            airllm_max_new_tokens=int(
                os.environ.get("AIRLLM_MAX_NEW_TOKENS", "256")
            ),
            airllm_dtype=os.environ.get("AIRLLM_DTYPE", "float16"),
            airllm_device=os.environ.get("AIRLLM_DEVICE", "mps"),
        )


SETTINGS = Settings.from_env()
