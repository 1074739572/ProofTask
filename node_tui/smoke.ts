import { createCliRenderer, Text } from "@opentui/core";

const renderer = await createCliRenderer({ exitOnCtrlC: true });
renderer.root.add(Text({ content: "OpenTUI smoke ok" }));
setTimeout(() => renderer.destroy(), 50);
