# MBTA train illustration prompt

This is a train-specific companion to `prompt.template.md`. It preserves the
same AvianVisitors illustration language without applying bird anatomy rules.

## Prompt

Create one isolated {com_name} vehicle, using IMAGE 1 as the exact equipment and livery reference. This asset will appear among bird illustrations in a live nature collage, so it must look as though the same Japanese printmaker painted both the birds and this train.

IDENTITY: Preserve the unmistakable geometry of the specific MBTA vehicle in IMAGE 1: cab and nose shape, windows, roof equipment, trucks, coupler, pilot, doors, vents, and the silver-gray body with the broad magenta-purple MBTA band and thin yellow pinstripe. Preserve the round black-and-white T medallion as a simple recognizable emblem. Do not invent a steam locomotive, subway car, trolley, European train, bullet train, or generic modern passenger train.

EQUIPMENT ACCURACY: If IMAGE 1 is the F40PH-3C locomotive, retain its broad angled full-width cab/nose, long vented engine body, roof fans, four-axle locomotive trucks, front pilot, and locomotive proportions; it must not become a passenger coach. If IMAGE 1 is the CTC-5 bi-level control car, it MUST remain visibly DOUBLE-DECK: two distinct passenger levels, two horizontal rows of windows, a tall blunt vestibule/cab end, and bi-level coach proportions; it must not collapse into a single-level coach or locomotive.

COMPOSITION: Show EXACTLY ONE complete standalone vehicle from a strong three-quarter side view matching IMAGE 1, fully inside the frame. Include the entire front end AND the entire closed rear end, roof, wheels, trucks, pilot, and couplers. Do not include any attached coach or any partial second vehicle. Nothing may continue out of frame. Aim for a compact horizontal silhouette around 2:1, never a very long thin train. No cropped ends. Leave visible cream padding beyond both the front and rear couplers.

STYLE: Match IMAGE 3 as a style reference: confident dark ink outlines, flat painted color zones, restrained mineral-pigment washes, slight handmade paper-print variation, and sparse purposeful detail. Simplify the train into approximately 35-50 confident shapes and strokes. Keep the large silver, charcoal, magenta-purple, yellow, and black regions clean and readable. Abstract tiny mechanical detail rather than rendering photographic texture. It must be a painted kachō-e-inspired woodblock illustration, not a photograph, not glossy 3D game art, not vector clip art, and not anime.

BACKGROUND AND SEGMENTATION: Place the vehicle alone on a perfectly flat, uniform, saturated #00ff00 chroma-key green background. The green is only a temporary extraction aid; do not let it tint, reflect onto, or appear anywhere inside the vehicle. No shadows, gradients, texture, floor plane, or lighting variation in the green. No tracks, ballast, platform, building, poles, wires, sky, landscape, people, smoke, cast shadow, contact shadow, reflection, caption, border, signature, or watermark. Keep clear green separation around the entire silhouette, including beneath and between the wheels, so deterministic chroma-key removal preserves every pale silver body panel.

TEXT: Do not attempt small lettering or serial numbers. The only mark to retain is a simplified circular MBTA T emblem; it must read as a graphic livery shape rather than generated prose.

OUTPUT: One vehicle only, generous even padding, high resolution, uniform #00ff00 background filling the whole rectangular canvas.
