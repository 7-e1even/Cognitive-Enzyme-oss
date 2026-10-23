#!/bin/bash
# RSI 经验流看门狗：每组进程死了且没做完就按原参数重拉(断点续跑);全部做完出配对报表后退出。
# 用法:nohup scripts/rsi-watchdog.sh >> logs/rsi-watchdog.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
D=tests/eval/hardbench/data/rsi
PY=.venv-eval/bin/python
export CE_RERANK_RETRIES=6 CE_RERANK_BACKOFF_CAP=20 CE_RERANK_TIMEOUT=60
export CE_GLM_SLOTS=10

# tag 目标题数 arm 并发 sets
SPECS=(
  "phys 372 self      5 physelite"
  "phys 372 teacher   5 physelite"
  "phys 372 seed      5 physelite"
  "phys 372 seed_self 5 physelite"
  "phys 372 self_hard    3 physelite"
  "phys 372 teacher_hard 3 physelite"
  "phys 372 base2        3 physelite"
  "phys 372 self_v2      3 physelite"
  "phys 372 self_v3      5 physelite"
  "phys 372 teacher_v3   5 physelite"
  # reflect 只做 base 错题(172 道)+ 切换前前两批已做的 6 道 base 对题 = 178 行
  "phys 178 reflect      10 physelite"
  "math 300 base      4 imo_answer,olymmath,amo,beyondaime"
  # math 记忆组暂停:base 81% 天花板下说明不了什么,GLM 槽位让给 phys 的对照组;恢复时取消注释即可断点续跑
  # "math 300 self      5 imo_answer,olymmath,amo,beyondaime"
  # "math 300 teacher   5 imo_answer,olymmath,amo,beyondaime"
  # "math 300 seed      5 imo_answer,olymmath,amo,beyondaime"
  # "math 300 seed_self 5 imo_answer,olymmath,amo,beyondaime"
)

done_rows() {  # 已完成(非 error)行数
  $PY - "$D/$1.$2.jsonl" <<'EOF'
import json, sys
try:
    print(sum(1 for l in open(sys.argv[1], encoding="utf-8") if l.strip() and not json.loads(l).get("error")))
except OSError:
    print(0)
EOF
}

running() { pgrep -f -- "tests.eval.hardbench.rsi --tag $1 .*--arm $2 --" >/dev/null; }

echo "$(date '+%F %T') watchdog 启动"
for i in $(seq 1 1440); do          # 上限 48h(每次循环睡 120s)
  all_done=1
  for spec in "${SPECS[@]}"; do
    read -r tag target arm conc sets <<< "$spec"
    n=$(done_rows "$tag" "$arm")
    if [ "$n" -ge "$target" ]; then continue; fi
    all_done=0
    if ! running "$tag" "$arm"; then
      extra=""; envs=""
      case "$arm" in
        seed|seed_self) extra="--seed-db ce_rsi_seed_experts";;
        # 评测不受 hook 9 秒限制:开两段判定、判官超时放宽到 15 秒
        reflect) extra="--only base_wrong"; envs="CE_JEV_TWO_STAGE=1 CE_JEV_TIMEOUT=15";;
      esac
      [ "$tag" = "math" ] && extra="$extra --limit 300"
      echo "$(date '+%F %T') 重拉 $tag/$arm(已完成 $n/$target)"
      env $envs nohup $PY -m tests.eval.hardbench.rsi --tag "$tag" --sets "$sets" --arm "$arm" $extra \
        --concurrency "$conc" >> "logs/rsi-$tag-$arm.out" 2>&1 &
      sleep 5
    fi
  done
  [ "$all_done" = "1" ] && break
  sleep 120
done

echo "$(date '+%F %T') 全部完成,出报表"
$PY -m tests.eval.hardbench.rsi --tag phys --report > "$D/phys.md"  2>> logs/rsi-watchdog.err
$PY -m tests.eval.hardbench.rsi --tag math --report > "$D/math.md" 2>> logs/rsi-watchdog.err
echo "$(date '+%F %T') 报表写在 $D/phys.md 与 $D/math.md,看门狗退出"
