"""Stage 4.5 选择规则的核心实现——线上选择(pipeline._apply_jev_dosage)、离线评估老师
(app/distill/arena)、无标签时按老师概率选目标(app/distill/export 的 sft 格式)以及判定记录里的
规则快照(app/distill/records)共用。

阈值本身是运行参数 selection.*(默认值、说明与取值范围只在 app/settings.py 里定义一次,后台可改)。
默认值 2026-09-25 定稿:留一领域交叉验证(真实基准 5 个领域 + 建议类评测集)选出,让单选题的
相对判断更有话语权——知识类记忆(缺陷、判例、论文)按"照做会不会更好"天然拿不到高 noul,
但单选题选得很准(旧值 CHOICE_MIN 0.4 / SUPPLEMENT_P 0.9 / CHOICE_OVERRIDE 0.8 / RESCUE_Q 0.9)。
"""

from __future__ import annotations

from app import settings

# 键 → (环境变量, 默认值),由运行参数注册表派生(评测报表与导出沿用这个形状)
THRESHOLDS: dict[str, tuple[str, float]] = {
    spec.key.split(".", 1)[1]: (spec.env, spec.default)
    for spec in settings.SPECS_LIST if spec.group == "selection"
}


# 类比识别默认开启(2026-09-25):三组真实经验流 A/B 都更好——法律判例流 本地 命中 24.8% → 44.5%、
# 托管检索栈 35.5% → 43.5%(Precision 均不降);Firefox 流 熟悉情境 60.2% → 67.7%(Precision -1.6)。
ANALOGY_DEFAULT = settings.SPECS["exemplar.analogy"].default


def analogy_mode() -> str:
    """exemplar.analogy(CE_EXEMPLAR_ANALOGY):on(默认,类比识别入选)| shadow(只问不用,评测对照)| off。"""
    return settings.get("exemplar.analogy")


def thresholds() -> dict[str, float]:
    """当前生效的阈值(后台值 > 环境变量 > 默认值)。"""
    return {k: settings.get(f"selection.{k}") for k in THRESHOLDS}


def select(p: dict[str, float], q: dict[str, float], none: float,
           rescued: set[str] | frozenset[str] = frozenset(),
           t: dict[str, float] | None = None, limit: int = 6) -> list[str]:
    """核心规则(主选 + 补充、none 门、救援门)的纯函数版,离线评估老师与导出用;线上另有
    危急级 / 类比 / 晋升 / 探索通道。p / q:候选键 → noul / 单选概率(键顺序即候选池顺序)。"""
    if not p:
        return []
    t = t or thresholds()

    def qv(k: str) -> float:
        return q.get(k, 0.0)

    def eligible(k: str) -> bool:
        return k not in rescued or p[k] >= t["rescue_p"] or qv(k) >= t["rescue_q"]

    none_ok = none < t["none_max"]
    top = max(p, key=lambda k: (qv(k), p[k]))
    sel: list[str] = []
    if ((qv(top) >= t["choice_min"] and p[top] >= t["primary_p"])
            or (none_ok and p[top] >= t["supplement_p"])
            or qv(top) >= t["choice_override"]) and eligible(top):
        sel.append(top)
    if none_ok and sel:
        q_max = max(qv(k) for k in p)
        sel += [k for k in sorted(p, key=lambda k: -p[k])
                if k not in sel and eligible(k) and p[k] >= t["supplement_p"] and qv(k) >= 0.1 * q_max]
    return sel[:limit]
