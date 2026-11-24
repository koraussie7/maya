#!/bin/zsh
# kilo_key_save.sh — Kilo Gateway API 키를 macOS Keychain 에 저장한다.
#
# 보관 위치 비교
#   Keychain(이 스크립트)  ✅ 파일로 안 남음, 암호화, 앱별 접근 제어
#   ~/.kilo_key (평문)     🟠 chmod 600 이지만 백업/동기화에 섞임
#   옵시디언 볼트 .md      ❌ 평문 + Dropbox/iCloud/Git 동기화로 유출 위험 — 쓰지 말 것
#
# 사용
#   ./kilo_key_save.sh                  # 키 입력(화면에 안 보임)
#   ./kilo_key_save.sh --check          # 저장된 키 존재/길이만 확인
#   ./kilo_key_save.sh --test           # 키로 Kilo Gateway 실제 호출 테스트
#   ./kilo_key_save.sh --delete         # 삭제
#
# 다른 스크립트에서 읽기
#   KILO_API_KEY="$(security find-generic-password -s kilo-gateway-api -w)"
set -u

SERVICE="kilo-gateway-api"
ACCOUNT="${USER}"
BASE_URL="https://api.kilo.ai/api/openrouter"

usage() {
  cat <<'HELP'
kilo_key_save.sh — Kilo Gateway API 키를 macOS Keychain 에 저장

사용법
  ./kilo_key_save.sh              키 입력 후 저장 (입력은 화면에 안 보임)
  ./kilo_key_save.sh --from-clipboard   클립보드의 키를 키체인에 저장(대화형 입력 없음)
  ./kilo_key_save.sh --check      저장된 키 존재/길이만 확인
  ./kilo_key_save.sh --test       키로 Kilo Gateway 실제 호출 테스트
  ./kilo_key_save.sh --delete     저장된 키 삭제

다른 스크립트에서 읽기
  KILO_API_KEY="$(security find-generic-password -s kilo-gateway-api -w)"

보관 위치
  Keychain(이 스크립트)  ✅ 파일로 안 남음
  ~/.kilo_key (평문)     🟠 백업/동기화에 섞임
  옵시디언 볼트 .md      ❌ 동기화 시 유출 위험 — 쓰지 말 것
HELP
  exit 0
}

case "${1:-}" in
  -h|--help) usage ;;
  --from-clipboard)
    # 클립보드 → 키체인 직접. 키를 화면/로그/파일에 남기지 않는다.
    KEY="$(pbpaste 2>/dev/null | tr -d '\r\n')"
    if [ -z "$KEY" ]; then echo "클립보드가 비었습니다 — 키를 복사한 뒤 다시 실행하세요"; exit 1; fi
    if [ ${#KEY} -lt 16 ] || ! printf '%s' "$KEY" | grep -qE '^[A-Za-z0-9_.:/-]+$'; then
      echo "클립보드 내용이 API 키 형식이 아닙니다(${#KEY}자) — 취소"; unset KEY; exit 1
    fi
    security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 || true
    security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$KEY" -U >/dev/null 2>&1
    if P="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"; then
      printf '저장 완료(클립보드): %s (길이 %d, 끝 …%s)\n' "$SERVICE" "${#P}" "${P: -4}"
      echo "클립보드는 비우는 것을 권합니다:  pbcopy < /dev/null"
      unset KEY P
    else
      echo "저장 실패"; unset KEY; exit 1
    fi
    exit 0 ;;
  --check)
    if P="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"; then
      printf '저장됨: %s (길이 %d, 끝 …%s)\n' "$SERVICE" "${#P}" "${P: -4}"
    else
      printf '저장된 키 없음: %s\n' "$SERVICE"; exit 1
    fi
    exit 0 ;;
  --delete)
    security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 \
      && echo "삭제 완료" || { echo "삭제할 항목 없음"; exit 1; }
    exit 0 ;;
  --test)
    P="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)" || {
      echo "키 없음 — 먼저 ./kilo_key_save.sh 로 저장하세요"; exit 1; }
    echo "게이트웨이 확인: $BASE_URL/v1/models"
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
      -H "Authorization: Bearer $P" "$BASE_URL/v1/models")"
    echo "  /v1/models → HTTP $code"
    [ "$code" = "200" ] || { echo "실패: 키가 유효하지 않거나 네트워크 문제"; exit 1; }
    echo "  (모델 목록 200 = 키 인증 통과)"
    exit 0 ;;
esac

# ── 대화형 저장 ────────────────────────────────────────────────
echo "Kilo Gateway API 키를 입력하세요 (입력 내용은 화면에 표시되지 않습니다)."
echo "  발급: Kilo Code → Settings → Providers → Kilo Gateway"
printf 'key: '
if [ -t 0 ]; then
  stty -echo
  IFS= read -r KEY
  stty echo
else
  IFS= read -r KEY   # 파이프 입력(자동화) — stty 불필요
fi
printf '\n'

if [ -z "${KEY:-}" ]; then
  echo "빈 값 — 취소"; exit 1
fi
# 실수로 붙여넣은 개행/공백 제거
KEY="${KEY#"${KEY%%[![:space:]]*}"}"
KEY="${KEY%"${KEY##*[![:space:]]}"}"

if [ ${#KEY} -lt 16 ]; then
  echo "키가 너무 짧습니다(${#KEY}자) — 취소"; exit 1
fi

# 기존 항목이 있으면 지우고 다시 넣는다(중복 방지)
security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 || true
security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$KEY" -U >/dev/null 2>&1

if P="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"; then
  printf '저장 완료: %s (길이 %d, 끝 …%s)\n' "$SERVICE" "${#P}" "${P: -4}"
  unset KEY P
else
  echo "저장 실패"; exit 1
fi

echo
echo "다른 스크립트에서 사용:"
echo '  export KILO_API_KEY="$(security find-generic-password -s '"$SERVICE"' -w)"'
echo '  export OPENAI_API_KEY="$KILO_API_KEY"'
echo '  export OPENAI_BASE_URL="'"$BASE_URL"'"'
