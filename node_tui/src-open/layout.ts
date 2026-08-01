import type {BaseRenderable, BoxRenderable} from '@opentui/core';

export const alwaysSeparate = new WeakSet<BoxRenderable>();

const previousByParent = new WeakMap<
  BaseRenderable,
  {frameId: number; previous: WeakMap<BaseRenderable, BaseRenderable | undefined>}
>();

export function setPreLayoutSiblingMargin(
  element: BoxRenderable,
  margin: (previous?: BaseRenderable) => number,
): void {
  element.onLifecyclePass = () => {
    const parent = element.parent;
    if (!parent) return;
    const cached = previousByParent.get(parent);
    const previous = cached?.frameId === element.ctx.frameId
      ? cached.previous
      : previousSiblings(parent, element.ctx.frameId);
    const value = margin(previous.get(element));
    if (element.marginTop !== value) element.marginTop = value;
  };
}

function previousSiblings(parent: BaseRenderable, frameId: number) {
  const previous = new WeakMap<BaseRenderable, BaseRenderable | undefined>();
  parent.getChildren().forEach((child, index, children) => {
    previous.set(child, children[index - 1]);
  });
  previousByParent.set(parent, {frameId, previous});
  return previous;
}
