export function resultLabels(
  artifact: { pack?: string } | undefined,
  displays: Record<string, { data_labels?: Record<string, string> }>,
): Record<string, string> {
  return artifact?.pack ? displays[artifact.pack]?.data_labels || {} : {}
}
