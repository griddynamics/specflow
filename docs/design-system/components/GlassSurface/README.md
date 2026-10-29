---
type: Design
title: "GlassSurface component"
description: "The Liquid Glass material: a translucent, blurred, saturated layer with a light-catching rim and a top-left specular sheen; every GD Liquid Glass component is made of it."
status: stable
tags: [design-system]
---
The Liquid Glass material: a translucent, blurred, saturated layer with a light-catching rim and a top-left specular sheen; every GD Liquid Glass component is made of it.

**Provide:** children; a backdrop worth refracting (put the screen on `.gdg-scene` or `.gdg-wash`, never on flat white).

**Variants**
- `regular` (`glass-fill`, `blur-md`): navigation, tab bars, cards, controls. Carries `ink` text; `ink-2` only at 24px+.
- `strong` (`glass-fill-strong`, `blur-lg`): sheets, dialogs, menus, forms and any text-heavy panel over imagery. Every text token passes on it.
- `subtle` (`glass-fill-subtle`): inset tracks and chips inside another glass layer; never the only layer under text.
- `tint` (`glass-tint`): the yellow selection lens.
- `ribbed`: adds the fine vertical ribbing from the brand artwork. One hero surface per screen at most.
- `floating`: `shadow-glass-float`, for layers above other glass.

**Do** keep glass to the functional layer (navigation, controls, overlays) floating above content. **Don't** stack more than two glass layers, put glass on glass-on-glass, or make long-form reading content glass: use `ground-alt` with `radius-0`.
