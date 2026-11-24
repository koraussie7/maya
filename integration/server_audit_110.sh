#!/usr/bin/env bash
# 110 서버 보안 점검 (읽기 전용). 서버에서만 실행한다.
#   ssh 110 'bash -s' < integration/server_audit_110.sh
set -u

echo "=== 1. 비밀번호 인증 성공 접속 (30일) ==="
n=$(journalctl -u ssh --since "30 days ago" 2>/dev/null | grep -cE "Accepted (password|keyboard)")
echo "count=$n"
journalctl -u ssh --since "30 days ago" 2>/dev/null | grep -E "Accepted (password|keyboard)" | tail -5

echo
echo "=== 2. 최근 24시간 접속 (Accepted, 키 이름) ==="
journalctl -u ssh --since "24 hours ago" 2>/dev/null | grep "Accepted" | awk '{print $1, $2, $3, $9, $11}' | tail -10

echo
echo "=== 3. 쉘 있는 계정 / uid 0 ==="
awk -F: '($3 >= 1000 && $7 !~ /(nologin|false)$/) || $3 == 0 {print $1, $3, $7}' /etc/passwd

echo
echo "=== 4. sudoers NOPASSWD ==="
grep -rE "NOPASSWD" /etc/sudoers /etc/sudoers.d/ 2>/dev/null | head -5

echo
echo "=== 5. cron / systemd timer ==="
crontab -l 2>/dev/null | grep -v '^#' | grep -v '^$' | head -10
ls /etc/cron.d/ 2>/dev/null
systemctl list-timers --all --no-pager 2>/dev/null | head -8

echo
echo "=== 6. 비정상 경로에서 실행 중인 프로세스 ==="
ps aux | awk '$11 ~ /^\/(tmp|var\/tmp|dev\/shm)/ {print $1, $2, $11}' | head -10

echo
echo "=== 7. 리스닝 포트 (외부 노출 후보) ==="
ss -tlnp 2>/dev/null | awk 'NR>1 {print $4, $6}' | grep -vE "127\.0\.0\.1|::1" | head -20

echo
echo "=== 8. 열린 역터널(원격 포워딩) ==="
ps aux | grep -E "[s]sh .*-R |[a]utossh" | head -5

echo
echo "=== 9. 최근 변경된 실행 파일 (7일, 상위 위험 경로) ==="
find /usr/local/bin /usr/bin /root /etc/cron.d -maxdepth 2 -type f -mtime -7 2>/dev/null | head -15

echo
echo "=== 10. sshd 유효 설정 ==="
sshd -T 2>/dev/null | grep -Ei "^(permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|maxauthtries)"
