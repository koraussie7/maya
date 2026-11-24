#!/bin/bash
# 마야 음성 파이프라인 야간 자동 러너 (7시간 무인).
#
# 원칙
#   · **위험한 작업 없음**: 측정·합성·다운로드·문서 생성만. 서버/nginx 설정 변경 금지.
#   · **실패해도 다음 단계**: 각 단계는 타임아웃 + 실패 기록 후 계속.
#   · **결과는 한 파일**: maya/docs/OVERNIGHT_REPORT_<날짜>.md 에 누적.
#   · **슬립 방지**: caffeinate 로 감싼다(없으면 계속 진행).
#
# 실행
#   nohup bash scripts/overnight_run.sh > /tmp/overnight.log 2>&1 &
#   # 또는
#   bash scripts/overnight_run.sh            # 포그라운드(터미널 유지 상태)
#
# 상태
#   tail -f /tmp/overnight.log               # 진행 확인
#   cat maya/docs/OVERNIGHT_REPORT_*.md      # 결과

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$REPO/.venv-voicemem/bin/python"
DATE="$(date +%Y-%m-%d)"
STAMP="$(date +%H:%M:%S)"
REPORT="$REPO/maya/docs/OVERNIGHT_REPORT_${DATE}.md"
LOGDIR="$REPO/.overnight"
AUDCPP="/tmp/audcpp/audiocpp_cli"
CV3_DIR="/tmp/audcpp/models/cv3/CosyVoice3-GGUF"
REF_B="$REPO/maya/models/tts/reference/clip_ref_lyra_vex_b.wav"
REF_TEXT="찾아봤는데 보통 챗지피티나 클로드 프로 같은 건 한 달에 20불 정도 거든 물론 더"
# macOS 에는 GNU timeout 이 없다 — coreutils 의 gtimeout 이 있으면 쓴다. 없으면 제한 없이 실행.
TO="$(command -v timeout || command -v gtimeout || true)"
mkdir -p "$LOGDIR"

say() { echo "[$(date +%H:%M:%S)] $*"; }

section() {
    echo "" >> "$REPORT"
    echo "## $*" >> "$REPORT"
    echo "" >> "$REPORT"
}

# 결과 요약 한 줄을 리포트에 남긴다
note() { echo "$*" >> "$REPORT"; }

# ── 시작 ────────────────────────────────────────────────────────────────
cat > "$REPORT" <<EOF
# 마야 야간 자동 실행 리포트 — $DATE

- 시작: $STAMP
- 호스트: $(hostname) / $(uname -m)
- 정책: 측정·합성·다운로드만. 서버 설정·배포 변경 없음.

EOF

# 슬립 방지(있으면). 자식 프로세스에 적용되도록 exec 대신 백그라운드 caffeinate
if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i -w $$ &
    CAFFEINATE_PID=$!
    say "caffeinate 활성(pid $CAFFEINATE_PID) — 7시간 슬립 방지"
    note "- caffeinate: 활성"
else
    say "caffeinate 없음 — 슬립 방지 불가"
    note "- caffeinate: 없음(슬립 주의)"
fi
echo "" >> "$REPORT"

run_step() {
    local name="$1"; shift
    local timeout_s="$1"; shift
    say "▶ $name"
    local start
    start=$(date +%s)
    # macOS 에는 GNU timeout 이 없다(coreutils 의 gtimeout). 위에서 한 번만 해석했다.
    if [ -n "$TO" ]; then
        ( cd "$REPO" && "$TO" "$timeout_s" "$@" ) > "$LOGDIR/${name}.log" 2>&1
    else
        ( cd "$REPO" && "$@" ) > "$LOGDIR/${name}.log" 2>&1
    fi
    local rc=$?
    local dur=$(( $(date +%s) - start ))
    if [ $rc -eq 0 ]; then
        say "  ✓ $name (${dur}s)"
        note "- ✅ $name — ${dur}s"
    elif [ $rc -eq 124 ]; then
        say "  ⏱ $name 타임아웃 (${dur}s)"
        note "- ⏱ $name — 타임아웃 ${dur}s"
    else
        say "  ✗ $name 실패(rc=$rc, ${dur}s)"
        note "- ❌ $name — rc=$rc (${LOGDIR}/${name}.log)"
    fi
    return 0   # 실패해도 다음 단계
}

# ── 1. CosyVoice3 모델 다운로드 (이미 진행 중이면 건너뜀) ────────────────
section "1. CosyVoice3 GGUF 준비"
if [ -d "$CV3_DIR" ] && [ "$(du -sm "$CV3_DIR" 2>/dev/null | cut -f1)" -ge 4000 ] 2>/dev/null; then
    say "  ✓ 이미 준비됨 ($(du -sh "$CV3_DIR" | cut -f1))"
    note "- ✅ 모델 준비됨 ($(du -sh "$CV3_DIR" | cut -f1))"
else
    say "  ⬇ 모델 다운로드(최대 50분)"
    ( cd "$REPO" && "${TO:-timeout}" 3000 "$PY" - <<'PYEOF'
from huggingface_hub import snapshot_download
p = snapshot_download("audio-cpp/audio.cpp-gguf",
                      allow_patterns=["CosyVoice3-GGUF/*", "CosyVoice3-GGUF/**"],
                      local_dir="/tmp/audcpp/models/cv3")
print("done", p)
PYEOF
    ) > "$LOGDIR/cosyvoice_download.log" 2>&1
    if [ -d "$CV3_DIR" ]; then
        note "- ✅ 모델 다운로드 ($(du -sh "$CV3_DIR" | cut -f1))"
    else
        note "- ❌ 모델 다운로드 실패 ($LOGDIR/cosyvoice_download.log)"
    fi
fi
ls "$CV3_DIR" 2>/dev/null | head -5 | sed 's/^/  - /' >> "$REPORT" || true

# ── 2. CosyVoice3 한국어 클론 (모델 있을 때만) ───────────────────────────
section "2. CosyVoice3 한국어 클론"
if [ -x "$AUDCPP" ] && [ -d "$CV3_DIR" ]; then
    CV3_MODEL=$(ls "$CV3_DIR"/*.gguf 2>/dev/null | head -1)
    note "- 모델 파일: \`$(basename "${CV3_MODEL:-none}")\`"
    if [ -n "${CV3_MODEL:-}" ]; then
        run_step "cosyvoice3_tts" 900 "$AUDCPP" tts \
            --family cosyvoice3 \
            --model "$CV3_MODEL" \
            --text "안녕하세요. 오늘 하루도 고생 많으셨어요." \
            --language ko \
            --voice-ref "$REF_B" \
            --reference-text "$REF_TEXT" \
            --out "$LOGDIR/cv3_ko.wav"
        if [ -f "$LOGDIR/cv3_ko.wav" ]; then
            ffprobe -v error -show_entries format=duration -of csv=p=0 "$LOGDIR/cv3_ko.wav" \
                2>/dev/null | sed 's/^/  - 오디오 길이: /' >> "$REPORT"
        fi
    fi
else
    note "- ⏭ audio.cpp 또는 모델 없음 → 건너뜀"
fi

# ── 3. Piper 한국어 모델 비교 (kss vs melotts 권장) ──────────────────────
section "3. Piper 한국어 벤치"
if [ -f "$REPO/maya/models/tts/piper/ko_KR-kss-medium.onnx" ]; then
    run_step "piper_bench" 900 "$PY" -c '
import os, sys, time, io, wave
os.environ["MAYA_TTS_PREWARM"] = "0"
os.environ["MAYA_TTS_KEEPALIVE_S"] = "0"
sys.path.insert(0, "integration")
from tts_local import LocalTTS
t = LocalTTS(engine="piper")
print("engine:", t.engine)
for s in ["네.", "안녕하세요, 오늘 하루 어떠셨어요?",
          "내일 아침 아홉 시에 회의가 있으니까 조금 일찍 시작하는 게 좋겠어요."]:
    t0 = time.time(); pcm = t.synthesize(s); dt = time.time() - t0
    sec = len(pcm) / 2 / 16000
    print(f"  {dt:5.2f}s  {sec:5.2f}s  RTF {dt/max(sec,0.01):5.2f}  | {s[:30]}")
'
    grep -E "RTF|engine" "$LOGDIR/piper_bench.log" | sed 's/^/  - /' >> "$REPORT"
else
    note "- ⏭ piper 모델 없음"
fi

# ── 4. TTS 엔진 종합 벤치 (say / piper) ─────────────────────────────────
section "4. TTS 폴백 엔진 비교"
run_step "tts_engines" 600 "$PY" -c '
import os, sys, time
os.environ["MAYA_TTS_PREWARM"] = "0"
os.environ["MAYA_TTS_KEEPALIVE_S"] = "0"
sys.path.insert(0, "integration")
from tts_local import LocalTTS
for eng in ("say", "piper"):
    try:
        t = LocalTTS(engine=eng)
        t0 = time.time(); pcm = t.synthesize("안녕하세요"); dt = time.time() - t0
        sec = len(pcm) / 2 / 16000
        print(f"{eng:6s} engine={t.engine:6s} {dt:.2f}s {sec:.2f}s RTF {dt/max(sec,0.01):.2f}")
    except Exception as e:
        print(f"{eng} 실패: {e}")
'
grep -E "RTF|실패" "$LOGDIR/tts_engines.log" | sed 's/^/  - /' >> "$REPORT"

# ── 5. 코덱 검증 (Opus 왕복) ────────────────────────────────────────────
section "5. Opus 코덱 왕복 검증"
run_step "opus_check" 300 "$PY" -m app.codec.opus_stream
grep -E "프레임|비율|디코드|frames/s" "$LOGDIR/opus_check.log" | sed 's/^/  - /' >> "$REPORT"

# ── 6. LLM 게이트웨이 재점검 ────────────────────────────────────────────
section "6. LLM 게이트웨이 상태"
run_step "gateway_probe" 420 "$PY" integration/gateway_probe.py --runs 3
grep -E "성공|판정" "$LOGDIR/gateway_probe.log" | sed 's/^/  - /' >> "$REPORT"

# ── 7. 기억(voicemem) 한국어 회귀 ───────────────────────────────────────
section "7. 한국어 기억 회귀 (raw 저장 + 검색)"
run_step "vm_ko_raw" 900 "$PY" integration/vm_ko_raw_memory.py --reuse
grep -E "top1|저장된 기억" "$LOGDIR/vm_ko_raw.log" | sed 's/^/  - /' >> "$REPORT"

# ── 8. 문서 인덱스 갱신 ─────────────────────────────────────────────────
section "8. 생성된 산출물"
{
    echo ""
    echo '| 파일 | 크기 |'
    echo '|------|------|'
    for f in "$LOGDIR/cv3_ko.wav" "$LOGDIR/voice_b_sample.wav"; do
        [ -f "$f" ] && echo "| \`$f\` | $(du -h "$f" | cut -f1) |"
    done
} >> "$REPORT"

# ── 종료 ────────────────────────────────────────────────────────────────
echo "" >> "$REPORT"
echo "- 종료: $(date +%H:%M:%S)" >> "$REPORT"
[ -n "${CAFFEINATE_PID:-}" ] && kill "$CAFFEINATE_PID" 2>/dev/null
say "완료 — $REPORT"
