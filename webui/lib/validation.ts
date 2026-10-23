import type { z } from "zod"

/**
 * Client-side validation, shared by the two shapes this console edits in.
 *
 * A form sheet fills a whole record and commits it once, so it runs the object
 * schema through react-hook-form's resolver. A detail sheet edits one row at a
 * time and commits each on its own, so it cannot run the object schema at all —
 * a half-filled draft would light up every other row alongside the one being
 * touched. Both read the same schema; only the grain differs.
 */

/**
 * The message one field's rule produces, or undefined when the value passes.
 *
 * Reads the rule off the object schema rather than taking a second, separate
 * one, so a form sheet and its detail sheet cannot drift apart on what a valid
 * value is.
 */
export function fieldError<Shape extends Record<string, z.ZodType>>(
  schema: z.ZodObject<Shape>,
  field: keyof Shape & string,
  value: unknown
): string | undefined {
  const rule = schema.shape[field]

  if (!rule) {
    return undefined
  }

  const result = rule.safeParse(value)

  return result.success ? undefined : result.error.issues[0]?.message
}

/**
 * Whether every named field currently passes.
 *
 * For the pages that commit a batch of rows at once — the tool matrix, the
 * routing board — where the gate is "nothing on screen is wrong" rather than
 * one field's message.
 */
export function fieldsValid<Shape extends Record<string, z.ZodType>>(
  schema: z.ZodObject<Shape>,
  values: Partial<Record<keyof Shape & string, unknown>>
): boolean {
  return Object.entries(values).every(
    ([field, value]) =>
      fieldError(schema, field as keyof Shape & string, value) === undefined
  )
}
