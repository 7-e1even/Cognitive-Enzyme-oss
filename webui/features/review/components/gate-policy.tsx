import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

import { ACTIVATE_CONFIDENCE_FLOOR, QUALITY_GATE } from "@/features/enzymes/lib/options"

const RULES = [
  { title: "自动通过", description: `质量门五维平均分 ≥ ${QUALITY_GATE.pass} 的新经验直接启用。` },
  { title: "精炼后存入", description: `${QUALITY_GATE.refine}–${QUALITY_GATE.pass} 分且模型给出改进版本时,存入改进后的表述。` },
  { title: "隔离待审", description: `${QUALITY_GATE.quarantine}–${QUALITY_GATE.pass} 分的存进库里但不参与召回,等人在这里决定。` },
  { title: "直接拒收", description: `低于 ${QUALITY_GATE.quarantine} 分不存入,只把改进建议退回给提交方。` },
  { title: "免疫巡检", description: "巩固时把长期低精度、长期没用到、超出容量或互相矛盾的记忆送回隔离。" },
  { title: "通过之后", description: `启用并把置信度提升到至少 ${ACTIVATE_CONFIDENCE_FLOOR},之后随反馈继续学习。` },
]

/** 质量门规则:阈值读自与后端一致的常量。 */
export function GatePolicy({ className }: { className?: string }) {
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>质量门规则</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 @xl:grid-cols-2">
        {RULES.map((rule) => (
          <div key={rule.title} className="min-w-0">
            <div className="text-sm font-medium">{rule.title}</div>
            <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{rule.description}</p>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
