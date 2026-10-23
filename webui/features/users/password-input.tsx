"use client"

import { DicesIcon, EyeIcon, EyeOffIcon } from "lucide-react"
import { useState } from "react"

import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"

import { generatePassword } from "./lib/users"

/** 密码输入框:可显示 / 隐藏,可一键生成随机密码(生成后自动显示,方便抄给对方)。 */
export function PasswordInput({
  id,
  value,
  onChange,
  onBlur,
  placeholder,
  invalid,
  disabled,
  autoComplete = "new-password",
}: {
  id?: string
  value: string
  onChange: (value: string) => void
  onBlur?: () => void
  placeholder?: string
  invalid?: boolean
  disabled?: boolean
  autoComplete?: string
}) {
  const [reveal, setReveal] = useState(false)
  return (
    <InputGroup>
      <InputGroupInput
        id={id}
        type={reveal ? "text" : "password"}
        autoComplete={autoComplete}
        spellCheck={false}
        className="font-mono"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
      />
      <InputGroupAddon align="inline-end">
        <InputGroupButton size="icon-xs" aria-label={reveal ? "隐藏密码" : "显示密码"} disabled={disabled} onClick={() => setReveal((v) => !v)}>
          {reveal ? <EyeOffIcon /> : <EyeIcon />}
        </InputGroupButton>
        <InputGroupButton
          size="xs"
          disabled={disabled}
          onClick={() => {
            onChange(generatePassword())
            setReveal(true)
          }}
        >
          <DicesIcon /> 生成
        </InputGroupButton>
      </InputGroupAddon>
    </InputGroup>
  )
}
