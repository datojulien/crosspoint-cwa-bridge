# Raspberry Pi integration benchmark

Date: 2026-08-13

- Bridge version: `0.5.0`
- Optimizer version: `crosspoint-standard-v1`
- Runtime: Linux ARM64 container, Python `3.13.15`

## Safety and selection

The selector inspected EPUB ZIP metadata with the Calibre library mounted
read-only. It found 1,002 valid candidates and copied two inputs to an isolated,
private working directory under sanitized filenames. Titles, author metadata,
source paths, and book contents were not logged or added to the project.

- Ordinary source `3b64723b8f0c`: 2,082,393 bytes, 4 images, 25.5% image
  payload.
- Image-heavy source `5d50ea10576f`: 99,482,759 bytes, 68 images, 98.8% image
  payload.

The benchmark used a local authenticated mock of the upstream EPUB response and
an isolated bridge cache. It measured the native optimizer in a fresh process
for resource statistics, then measured a cold HTTP request and an immediate
cache hit through the bridge. The temporary copies, outputs, work directory,
and isolated cache were removed after the run. The production bridge cache was
not used.

## Results

All sizes below use MiB (1,048,576 bytes). Optimizer time and HTTP time were
measured in separate runs, so small differences between optimizer time and cold
HTTP latency are expected.

| Source | Profile | Source MiB | Output MiB | Saved | Optimizer s | Cold TTFB s | Cold total s | Hit TTFB s | Hit total s | Peak RSS MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Ordinary | X3 | 1.99 | 1.33 | 33.0% | 0.615 | 0.558 | 0.559 | 0.007 | 0.008 | 90.8 |
| Ordinary | X4 | 1.99 | 1.32 | 33.7% | 0.582 | 0.530 | 0.531 | 0.008 | 0.010 | 90.7 |
| Image-heavy | X3 | 94.87 | 3.52 | 96.3% | 6.178 | 6.511 | 6.520 | 0.014 | 0.017 | 93.8 |
| Image-heavy | X4 | 94.87 | 3.08 | 96.8% | 6.007 | 6.216 | 6.218 | 0.013 | 0.015 | 93.4 |

Every direct optimizer output size matched its corresponding cold HTTP output,
and every cache-hit response matched the cold response byte count. Generated
EPUB validation is part of the optimizer's publication path.

## Conclusion

The ordinary first response is under 0.6 seconds. Even the 94.87 MiB stress
case completes and begins responding in under 6.6 seconds, leaving a substantial
margin below CrossPoint's roughly 60-second socket-operation timeout. Peak
worker memory stayed under 94 MiB. These measurements do not justify prewarming,
streaming ZIP generation, or another timeout workaround for version 1.
