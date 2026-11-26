# Diagram
Source: diagrams.net · 1 page

## Flow

### Shapes
- **Start** (step)
- **Reviewer** (actor)
- **Approved?** (decision): "Yes, I reviewed" & signed
- **Archive** (store)
- **SPEC.md** (document)
  - link: https://example.com/spec?a=1&b=\[2\]
  - owner: ops

### Arrows
- Start → Approved?
- Approved? → Archive: yes · late
- Archive → (unlabelled shape): to nowhere
- (unlabelled shape) → SPEC.md
- SPEC.md → (unlabelled shape)

### Notes
- \# Legend · \[click\](http://x) \*bold\* \_it\_
