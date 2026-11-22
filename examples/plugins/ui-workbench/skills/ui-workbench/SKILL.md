---
name: ui-workbench
description: Explore distinct web interface directions in an offline comparison board, then review a chosen design using desktop, mobile, and interaction evidence. Use for interface redesign, design direction selection, or a visual and usability review.
---

# UI Workbench

Choose **Explore** when the design direction is undecided. Choose **Review** for a specific interface. For a small, well-defined change to an established interface, work directly rather than forcing a comparison exercise.

## Explore

1. Capture the decision in one sentence: who is using this interface, what they must do, and which constraint changes what a good result looks like.
2. Prototype three plausible first screens that differ in hierarchy, layout, and interaction. Keep the same content and primary task across directions so the trade-offs are comparable. Write the drafts in the current workspace, apart from the actual application. Use self-contained UTF-8 HTML with inline styles and embedded raster assets; comparison previews do not run scripts.
3. Create a JSON manifest alongside the prototypes. It has `title`, optional `brief`, and `candidates` (2–6 entries). Each entry has `title`, workspace-confined `path` relative to the manifest, and optional `summary` explaining the trade-off. Call `ui_workbench__compare` with `{"manifest":"exploration/manifest.json"}` and give the user the actual returned `comparison.html` path.
4. Recommend a direction for the brief, citing one meaningful strength and limitation of each. Let the user choose before implementing a direction unless they explicitly delegated the choice. Reuse their current stack and established components for implementation.

## Review

- **Visual pass:** inspect the real desktop and narrow layout. Compare visual order with task order; check text wrapping, spacing rhythm, contrast, and meaningful empty/error/loading states. Use measured overflow, broken-image counts, and contrast candidates as leads, not as an accessibility certification.
- **Usability pass:** identify the most important task and try one Complete → Recover sequence where relevant (for example, submit with a missing field, correct it, submit successfully). Use test data. Check keyboard order, visible focus, labels, feedback, and whether the layout remains usable after the state change. Do not change live data or modify the UI during a review-only request.
- `ui_workbench__status` shows which integrations are configured, not whether they are reachable. Use `ui_workbench__capture` on a trusted workspace page or your own loopback preview, then inspect the returned screenshots with `ViewImage`. If no local browser driver is configured, work from supplied screenshots and source evidence, and label interactive behavior unverified. For an independent second pass, delegate a bounded visual or usability review if delegation is available; provide only the specific target and evidence needed.
- Report issues by user impact. Include the exact viewport/state and evidence, a focused change, and a way to reproduce or verify the fix. Keep observed facts, hypotheses, and untested cases separate.

## Optional images

Use `ui_workbench__media` only if the task needs sourced imagery; keep the returned rights information and attribution with any downloaded asset you reuse. `ui_workbench__image` sends the prompt and any selected reference image to the configured provider and may incur charges. Only request it when that external processing suits the user's task; never treat a missing provider as permission to use a different one.
