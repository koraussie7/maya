#!/usr/bin/env bash
# Lyra-Vex 자가 클론용 reference 녹음 헬퍼.
#
# 사용 전:
#   1) macOS 환경설정 > 사운드 > 입력 → 사용하려는 마이크 선택
#   2) 조용한 방, 마이크 입구 ~15cm 거리
#   3) 아래 SCRIPT_KO 를 끝까지 또박또박 읽음 (30~60초 분량)
#
# 결과:
#   integration/lyra_self_clone/ref.wav (16kHz/16bit/mono)
#
# 시스템 TTS가 미리 같은 문장을 들려주므로, 발화 속도/억양을 자연스럽게 따라가면 됩니다.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/ref.wav"
TTS_PREVIEW="$HERE/_preview.m4a"

# 시스템 TTS (한국어 음성) — 사파리/말풍선이 있으면 사용, 없으면 건너뜀
say -v Yuna "안녕하세요. 오늘 하루 어땠어요? 오늘은 제 목소리를 복제해서 음성비서에 사용할 거예요. 다음 문장도 또박또박 읽어주세요. 오늘 점심은 뭐 드셨어요? 피곤하시죠? 조금만 더 힘내요. 저는 항상 곁에 있을게요. 수고하셨어요." -o "$TTS_PREVIEW" || true

# 마이크 입력 → WAV (44100Hz stereo → sox 로 16kHz mono 변환)
echo ""
echo "[1/3] 마이크 권한 요청이 뜨면 '허용' 클릭"
echo "[2/3] 다음 20초간 자연스럽게 다음 대본을 또박또박 읽으세요:"
echo "       ─────────"
cat <<'KOEOT'
안녕하세요. 오늘 하루 어땠어요?
오늘은 제 목소리를 복제해서 음성비서에 사용할 거예요.
다음 문장도 또박또박 읽어주세요.
오늘 점심은 뭐 드셨어요?
피곤하시죠? 조금만 더 힘내요.
저는 항상 곁에 있을게요. 수고하셨어요.
KOEOT
echo "       ─────────"
echo ""
read -r -p "준비됐으면 Enter 누르고 20초 대기 →  Enter 한 번 더로 녹음 종료" _

# sox가 있으면 사용, 없으면 afconvert
if command -v sox >/dev/null 2>&1; then
  echo "▶ sox 녹음 시작 (20초, 16kHz mono)"
  sox -q -d -r 16000 -c 1 -b 16 "$OUT" trim 0 20
else
  echo "▶ afconvert fallback (44.1kHz stereo → 16kHz mono)"
  TMP="$HERE/_raw.wav"
  afconvert -f WAVE -d LEI16@44100 -c 1 "$TMP" || true
  afconvert -f WAVE -d LEI16@16000 -c 1 "$OUT" "$TMP" || true
  rm -f "$TMP"
fi

echo ""
echo "▶ 녹음 결과: $OUT"
ls -lh "$OUT"
afinfo "$OUT" || true
