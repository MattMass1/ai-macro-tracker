# Macro-Bit food sprite visual contract

## Grid and mass

Author at 32 x 32 logical pixels. New masters use passive filled rects and orthogonal stepped polygons, integer coordinates only. No smooth paths, circles, strokes, transforms, shadows, labels, emoji or embedded images. Keep at least two logical transparent pixels around every edge. Most shapes occupy 20-26 logical pixels; long foods and small seed clusters may have less area for recognition rather than forced identical bounding boxes.

Use a dark olive silhouette around natural fills, typically about two logical pixels thick, with one or two restrained highlight/detail colors. Some small highlights are one logical pixel. The eight retained originals define the style; preserve their bytes instead of normalizing them. Broccoli's inherited diagonal stem edge is explicitly retained and is not a template for new art.

## Palette

- Outline: #4a5036
- Leaf/body greens: #57754b, #8ea363
- Cream/bright highlight: #f4efd5, #fffbeb
- Grain/gold: #d9b34d, #ebcf79
- Brown: #886344, #6b543e
- Red/fish/skin tones: #b8614c, #cc8266, #dfb185
- Neutral vessel/fish blue-gray: #81918b

Additional restrained natural shades distinguish food structure, not merely color-swapped identities. Avoid saturated neon. Food colors are illustrative and must not be interpreted as preparation, freshness or nutritional data.

## Export and display

The source viewBox is `0 0 32 32`; intrinsic dimensions remain 256 x 256 for consistency with v1. New raster masters are cell-center rasterized at32, then repeated at2x/4x/8x. Binary alpha only, no matte or smoothing. All96 SVG/256 PNG masters were cross-checked at actual resolution against Chrome; renderer evidence is in CHROME-CHECK.json. The unchanged broccoli256 is a legacy exception to enlargement from32, not to original byte identity.

Use32px or64px in chat when possible. Set nearest-neighbor/pixelated interpolation and preserve transparency. Contact sheets show128px and32px exports on both #faf9ed light and #263a2e dark plates. Dark outlines can merge into the dark plate; recognizable fill silhouettes, authoritative labels and large/small checks matter more than decorative contrast. Do not use sprite colors for text.

Preview text uses #263a2e on #eef1e8 or #faf9ed, reversed #f4efd5 on #263a2e; links use #165639 on #eef1e8. Ratios are measured in QA. Native body labels remain readable system text. Jersey 15 Regular400 is used only for headings without synthetic bold. Do not bake food names, quantities or nutrition into the icon.

## Recognition and composition

Use distinct whole-food silhouettes where possible; a cut section, vessel or preparation convention may make a food recognizable. Rice uses a bowl, tuna an open tin, chia/quinoa generic unbranded containers, pasta a noodle plate. These are recognition conventions, not observed serving sizes or cooking states. Seed/grain/vessel families and similar leaves still require labels. No artwork alone identifies an allergen, ingredient list, portion, nutrition value or specific recipe.

Retain generic neutral food and meal fallbacks. A plate/fork means unknown food, a covered serving dish means unknown/mixed meal; neither suggests ingredients. Art absence must never block logging. Future additions should fill meaningful coverage gaps after real usage review rather than add portion variants, unreviewed look-alikes or arbitrary recolors.
