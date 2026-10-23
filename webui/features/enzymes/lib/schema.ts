import { z } from "zod"

import { SEVERITY_VALUES, STATUS_VALUES } from "./options"
import { regexError } from "./patterns"

/**
 * 一条记忆的合法形态。
 *
 * 新建表单整体交给 react-hook-form 的 resolver,详情抽屉逐行只校验正在编辑的那一个字段;
 * 两处读同一份规则,不会对「什么算合法」给出两种答案。提示语写给操作者看。
 */

const CONFIDENCE_RANGE = "置信度是 0 到 1 之间的数字,例如 0.65。"

const patternItemSchema = z
  .object({
    type: z.enum(["keyword", "regex"]),
    value: z.string().trim().min(1, "触发条件不能为空。"),
  })
  .superRefine((item, ctx) => {
    if (item.type !== "regex") return
    const error = regexError(item.value)
    if (error) ctx.addIssue({ code: "custom", message: `${item.value}:${error}` })
  })

const listSchema = z.array(z.string().trim().min(1))

export const enzymeFieldSchema = z.object({
  then: z.string().trim().min(1, "建议不能为空,它就是召回时注入给智能体的内容。"),
  because: z.string().trim().max(2000, "原因请控制在 2000 字以内。"),
  patterns: z.array(patternItemSchema).min(1, "至少需要一个触发条件,否则这条记忆永远不会被召回。"),
  domain: z.string().trim().max(64, "领域名称不超过 64 个字符。"),
  tags: listSchema.max(20, "标签最多 20 个。"),
  references: listSchema,
  conditions: listSchema,
  negations: listSchema,
  severity: z.enum(SEVERITY_VALUES),
  status: z.enum(STATUS_VALUES),
  /** 编辑时是输入框里的文字,提交时才读成数字;清空不等于 0。 */
  confidence: z
    .string()
    .trim()
    .min(1, CONFIDENCE_RANGE)
    .pipe(z.coerce.number<string>({ error: CONFIDENCE_RANGE }).min(0, CONFIDENCE_RANGE).max(1, CONFIDENCE_RANGE)),
})

export type EnzymeField = keyof typeof enzymeFieldSchema.shape

export const enzymeCreateSchema = enzymeFieldSchema.pick({
  patterns: true,
  then: true,
  because: true,
  domain: true,
  tags: true,
  references: true,
  severity: true,
})

export type EnzymeCreateInput = z.input<typeof enzymeCreateSchema>
export type EnzymeCreateValues = z.output<typeof enzymeCreateSchema>
