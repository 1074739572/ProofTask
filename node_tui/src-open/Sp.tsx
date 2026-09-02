/**
 * Typed colored inline text run for <text> children. OpenTUI's runtime
 * accepts fg/bg on <span> (see TextNodeOptions in @opentui/core), but
 * SpanProps is typed as ComponentProps<{}, TextNodeRenderable>, so the
 * attributes are missing from the JSX types. One cast here keeps every
 * call site type-clean.
 */
export function Sp(props: {fg?: string; bg?: string; children?: any}): any {
  return <span {...({fg: props.fg, bg: props.bg} as any)}>{props.children}</span>;
}
