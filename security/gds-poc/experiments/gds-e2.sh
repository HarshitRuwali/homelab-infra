#!/bin/bash
# E2 orchestration on t7920. Attacker=395, victim=396. SMT off (verified separately).
A_LLA="fe80::be24:11ff:fe63:5062%vmbr0"   # 395
V_LLA="fe80::be24:11ff:fe97:d1b3%vmbr0"    # 396

wait_ssh() {
  local lla="$1" i
  for i in $(seq 1 24); do
    ssh-keygen -R "$lla" >/dev/null 2>&1
    if timeout 5 ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=3 admin@"$lla" "true" 2>/dev/null; then return 0; fi
    sleep 5
  done
  echo "SSH-TIMEOUT $lla"; return 1
}

echo "== [prep] 395 -> x86-64-v3, restart"
qm stop 395 >/dev/null 2>&1 || true
qm set 395 -cpu x86-64-v3
qm start 395
wait_ssh "$A_LLA" && echo "395 up (v3)"

echo "== [prep] push binaries"
/root/gds-ssh.sh 395 "cat > ~/e2_attacker && chmod +x ~/e2_attacker" < /root/e2_attacker && echo "attacker pushed"
/root/gds-ssh.sh 396 "cat > ~/e2_victim && chmod +x ~/e2_victim" < /root/e2_victim && echo "victim pushed"

echo "== PHASE A: same-core baseline (victim idle), attacker on core 37, 6s"
/root/gds-ssh.sh 395 "timeout 8 ~/e2_attacker 6; echo rc=\$?" | tee /tmp/attA.txt

echo "== PHASE C: attack (both pinned to core 37)"
/root/gds-ssh.sh 396 "timeout 10 ~/e2_victim 8" & VPID=$!
sleep 2
/root/gds-ssh.sh 395 "timeout 8 ~/e2_attacker 6; echo rc=\$?" | tee /tmp/attC.txt
wait $VPID

echo "== PHASE B: cross-core control (attacker -> core 38, victim still on 37)"
qm stop 395 >/dev/null 2>&1 || true
qm set 395 -affinity 38
qm start 395
wait_ssh "$A_LLA" && echo "395 up (core 38)"
/root/gds-ssh.sh 396 "timeout 10 ~/e2_victim 8" & VPID=$!
sleep 2
/root/gds-ssh.sh 395 "timeout 8 ~/e2_attacker 6; echo rc=\$?" | tee /tmp/attB.txt
wait $VPID

echo "== DONE"
