#!/bin/bash
# Maya 백엔드 로컬 실행 (맥북). 이 파일 하나로 재시작한다.
#   ./scripts/run_maya_backend.sh            # 포그라운드
#   nohup ./scripts/run_maya_backend.sh &     # 백그라운드
# 스크립트 위치 기준 repo 루트를 **먼저** 구해 둔다. cd 이후에 dirname 을 다시
# 상대경로로 쓰면 `scripts/..` 가 이미 다른 디렉토리에 있어 실패한다(실측).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT/maya-ui/backend" || exit 1

# ── 서버 ─────────────────────────────────────────────
export MAYA_WS_HOST=0.0.0.0          # 터널/nginx 뒤에서 받으려면 0.0.0.0
export MAYA_WS_PORT="${MAYA_WS_PORT:-8788}"
# nginx(7684 → 18000) 뒤에 붙일 때는 **18000** 으로 맞춰라:
#   MAYA_WS_PORT=18000 bash scripts/run_maya_backend.sh
export MAYA_LOG_LEVEL=INFO

# ── 대화 LLM ─────────────────────────────────────────
# cloud(OpenRouter) | private(자체 110 게이트웨이) | local(ollama)
export MAYA_LLM_PROVIDER="${MAYA_LLM_PROVIDER:-local}"
export OPENAI_BASE="${OPENAI_BASE:-http://127.0.0.1:11434/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}"
export OPENAI_MODEL="${OPENAI_MODEL:-qwen2.5-coder:1.5b}"
# 백엔드 `_fmt` 는 비스트림 클라이언트라 tok/s 가 느린 로컬 모델을 견디게 넉넉히
export MAYA_LLM_TIMEOUT_S="${MAYA_LLM_TIMEOUT_S:-90}"
# ── 대화 기록 / 비용 ─────────────────────────────────
# 원문은 전부 저장(maya/transcripts, append-only JSONL). LLM 에는 최근 N턴만 보낸다.
#   전체 재전송 → 입력이 턴 수의 제곱으로 증가(1,000턴 $2.10 실측)
#   8턴 윈도우 → 1,000턴 약 $0.07 (30배 절감)
export MAYA_CONTEXT_TURNS="${MAYA_CONTEXT_TURNS:-8}"
export MAYA_TRANSCRIPTS_DIR="${MAYA_TRANSCRIPTS_DIR:-$REPO_ROOT/maya/transcripts}"

# ── 페르소나 / 음성 ──────────────────────────────────
export MAYA_PERSONA="${MAYA_PERSONA:-lyra_vex}"      # lyra_vex_b = 여성(B)
# TTS: voxcpm-api(클론) | piper | say | auto
export TTS_ENGINE="${TTS_ENGINE:-voxcpm-api}"
# 살아있는 Space 는 `openbmb-voxcpm-demo`. `openbmb-voxcpm-tts` 는 응답하지 않는다(실측).
export VOXCPM_API_URL="${VOXCPM_API_URL:-https://openbmb-voxcpm-demo.hf.space}"
# 워커에 Space 이름을 넘기려면 이 값을 쓴다(비우면 URL 만 사용).
export VOXCPM_SPACE="${VOXCPM_SPACE:-OpenBMB/VoxCPM-Demo}"
# 클론 레퍼런스(페르소나 yaml 이 우선, 없을 때만 이 값을 쓴다).
#   clip_ref_speaker_a.wav = 본인, clip_ref_speaker_b.wav = 여성(B)
# 오디오 송출 포맷: wav(기본) | opus(12.8배 작음, 브라우저 WebCodecs 필요)
#   10초 문장 기준 base64 427KB → 33KB. 호환 안 되면 자동으로 wav 폴백.
# HF Space 는 **동시 처리를 못 한다**(실측: backend is busy). 병렬로 보내면 전부 거절된다.
#   batch-never-fan-out: 요청은 순차 1건, 거절은 재시도(폴백 아님).
export MAYA_TTS_WORKERS="${MAYA_TTS_WORKERS:-1}"
export MAYA_TTS_BUSY_RETRIES="${MAYA_TTS_BUSY_RETRIES:-3}"
export MAYA_TTS_BUSY_WAIT_S="${MAYA_TTS_BUSY_WAIT_S:-1.5}"
export MAYA_TTS_BUDGET_S="${MAYA_TTS_BUDGET_S:-45}"
export MAYA_AUDIO_CODEC="${MAYA_AUDIO_CODEC:-wav}"
export VOXCPM_REF_WAV="${VOXCPM_REF_WAV:-$REPO_ROOT/maya/models/tts/reference/clip_ref_speaker_a.wav}"

# STT 는 SenseVoice 워커(메인 venv) — 백엔드 프로세스에는 torch 가 없다
export MAYA_STT_WORKER_PYTHON="${MAYA_STT_WORKER_PYTHON:-$REPO_ROOT/.venv-voicemem/bin/python}"

exec "${PYTHON:-$REPO_ROOT/.venv-voicemem/bin/python}" -m app.server
