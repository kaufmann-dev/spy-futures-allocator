# Mobile login vertical centering

- Fixed: 2026-08-12 14:43:45 UTC (+0000)
- Starting commit: `23de1a2d76d086b92a00118b11137b33d80882d6`

## Symptom

On tall mobile browser viewports, the Pocket ID login card appeared far below the top of the page, leaving a large empty area above it.

## Cause

The standalone login document made the full-height `body` a grid with `place-items: center`. That centered the card on both axes, so mobile viewport height and browser chrome placed it too low on the screen.

## Changes

- Preserve centered login placement on wider screens.
- Top-align the login card with compact vertical padding on viewports up to `40rem` wide.
- Use the dynamic viewport height unit on mobile while retaining the existing viewport-height fallback.
- Include the self-theming logo only as a favicon in the login document head.
- Add a focused regression test for the mobile layout rules.
