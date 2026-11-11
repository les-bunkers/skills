---
name: grade
description: Color grade stage — one look per set or shoot, graded by the human in DaVinci Resolve, exported as a 65-point .cube LUT and wired into ffmpeg pipelines (clips, formats) with a frame-exact check. Use when grading a video, exporting or applying a Resolve look/LUT, matching a pipeline's color to Resolve, or running /videos:grade.
argument-hint: "[video name] [set]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Resolve calls go through Blackmagic's DaVinci Resolve MCP (Resolve Studio; tools `run_script`, `describe_grade`, `generate_lut`).

## Rule

Grade per set: one look = one shoot, set or episode. Lighting differs between sets, so a look never crosses them. A set without its own grade stays ungraded until the human grades it.

## Do

1. The human grades in Resolve: an adjustment clip on V2 of the set's timeline, or the clip itself. The agent never invents the look; it may propose a starting LUT with `generate_lut` when asked.
2. `describe_grade` on that item. A LUT is exact only for color nodes (balance, log, curves, HSL qualifiers). Windows, masks, blur, noise reduction, grain and vignettes don't survive a LUT → renders that need them stay in Resolve.
3. Export through `run_script`:
   `tl.GetItemListInTrack("video", 2)[0].ExportLUT(resolve.EXPORT_LUT_65PTCUBE, r"<project>/_look/<set>_65.cube")`
4. Wire it:
   - ffmpeg pipelines: bt709 limited → `rgb48le` full → `lut3d=<cube>:interp=tetrahedral` → bt709 limited, on the source frame before any crop, output tagged bt709:
     `-vf "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb48le,lut3d=file='<cube>':interp=tetrahedral,scale=in_range=pc:out_color_matrix=bt709:out_range=tv,format=yuv420p" -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv`
     (in the filter string escape `:` in a Windows path as `\:` and use forward slashes)
   - Resolve renders: the grade stays in the timeline; the master render carries it.
5. Verify: one ffmpeg + LUT frame against `project.ExportCurrentFrameAsStill` of the same frame → mean absolute difference ≈ 1/255 (measured 1.0; 4.9 without the LUT). Decoding as bt601 by mistake shows up as ~1.3 extra.
6. Grade changed → re-export under the same name; pipelines that track the `.cube` as an input re-master on their next run.
7. `videos.py set <slug> grade "done <date> <set>_65.cube"`.

Project color used for the measured numbers: DaVinci YRGB unmanaged, Rec.709 in and out.
