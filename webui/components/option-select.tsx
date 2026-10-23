"use client"

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

const ALL = "__all__"

export interface Option {
  value: string
  label: string
}

/** 下拉筛选:空字符串表示「全部」(Radix 的选项值不能为空,这里统一换成哨兵值)。 */
export function OptionSelect({
  value,
  onChange,
  options,
  allLabel,
  label,
  className,
  size = "sm",
  disabled,
}: {
  value: string
  onChange: (value: string) => void
  options: Option[]
  /** 给了就多一个「全部」选项,选中时 onChange("") */
  allLabel?: string
  label: string
  className?: string
  size?: "sm" | "default"
  disabled?: boolean
}) {
  return (
    <Select value={value || ALL} onValueChange={(next) => onChange(next === ALL ? "" : next)} disabled={disabled}>
      <SelectTrigger size={size} className={className} aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {allLabel ? <SelectItem value={ALL}>{allLabel}</SelectItem> : null}
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
