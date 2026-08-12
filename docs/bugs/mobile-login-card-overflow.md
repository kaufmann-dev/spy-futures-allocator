# Mobile login card overflow

- Fixed: 2026-08-12 14:48:04 UTC (+0000)
- Starting commit: `1a0d163312cc04c466e3f19630bfeddbaeccad3d`

## Symptom

On mobile, the Pocket ID login card extended beyond the viewport, hiding its left and right borders. A preceding attempted fix also top-aligned the card instead of keeping it centered.

## Confirmed root cause

The card used the default content-box model with a width of `calc(100% - 3rem)`, then added `4rem` of horizontal padding and its borders outside that width. Its rendered width therefore exceeded the mobile viewport. The preceding mobile media rule separately changed `place-items` from centered to top-aligned.

## Changes

- Apply `border-box` sizing to the login document so padding and borders stay inside declared widths.
- Give the page one rem of padding and constrain the card to the remaining available width.
- Center the complete card on both axes at every viewport size.
- Use dynamic viewport height with the existing viewport-height fallback.
- Replace the incorrect top-alignment regression test with centered and contained layout assertions.
