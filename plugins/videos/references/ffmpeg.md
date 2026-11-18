# ffmpeg traps seen in the pipeline

Every entry cost a failed run at least twice. `✗ **tried** → what happened → what to do instead`.

## Silent failures

- ✗ **`ffmpeg … 2>/dev/null && echo ok`, or a pipe into `| tail`** → "ok" printed while nothing was written (a pad that could not fit, a seek past the end), or exit 0 from `tail` hiding ffmpeg's failure → test that the output file exists and has the expected frame count before reporting success; keep stderr, or `set -o pipefail`.
- ✗ **Trusting the first render's frame count** → compare ffprobe's count (`-count_packets -show_entries stream=nb_read_packets`) with the expected range on every render that goes back onto a timeline.
- `Fontconfig error: Cannot load default config file` on Windows builds = noise (no fontconfig file); the output is still written. Pass `fontfile=` to drawtext to silence it.

## Filter graphs

- ✗ **A Windows path inside a filter option** (`metadata=print:file=<drive>:\…\luma.txt`, `lut3d=file=…`, `fontfile=…`) → the drive colon and backslashes break the filter parser (`No option name near '/Windows/Fonts/…'`, `Error parsing filterchain`, or the file never appears) → forward slashes and the colon escaped (`C\:/Windows/Fonts/arial.ttf`), or `file=-` and parse stdout. An agent shell that eats one level of backslashes changes the escape on the way: put such commands in a script file instead of an inline string.
- ✗ **Reusing a stream label twice** (`[a3]` as input of two filters) → exit −22 → `split` (video) / `asplit` (audio) first.
- ✗ **`vstack` / `hstack` of different widths or heights** → `Input 1 width 1099 does not match input 0 width 845`; odd sizes round (`scale=-2:533` beside `300:-2` → 533 vs 534) → scale every input to one even size first: `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2`.
- ✗ **`pad` to a size smaller than the input** → `Padded dimensions cannot be smaller than input dimensions` → scale down first (same recipe).
- ✗ **`select`+`tile` per input, then a stack, in one graph with `-frames:v 1`** → exit 127, no message at `-v error` → one tile per file (`-fps_mode passthrough -update 1`), then stack the images. A contact sheet built with `select` can also show other frames than the ones named: when frames are evidence, extract each with `-ss <t> -i <file> -frames:v 1`.
- ✗ **`-frames:v 8` + `tile` to one image** → image2 `Cannot write more than one file with the same name` → `-frames:v 1` or `-update 1`.
- ✗ **`boxblur=24:4` on a small region** (a redaction over a few digits) → `Invalid chroma_param radius value 24, must be >= 0 and <= 14` → radius at most half the plane, chroma plane half the region: `lr = max(1, min(24, min(w, h) // 2 - 1))`, `cr = max(1, min(lr, min(w, h) // 4 - 1))`, `boxblur=luma_radius=lr:luma_power=4:chroma_radius=cr:chroma_power=4`.

## Rates, sizes, codecs

- Camera files of one shoot can run at another rate than the program (ISO cameras at 1199/50 beside a 24000/1001 program). ✗ **`fps=23.976`** → frames dropped and duplicated (931 vs 935 per track) → restamp: `setpts=N/(24000/1001)/TB` with `-fps_mode passthrough`. ✗ **Adding `-r 24000/1001` to that** → `One of -r/-fpsmax was specified together a non-CFR -vsync/-fps_mode. This is contradictory.` → drop `-r`.
- ✗ **Odd frame sizes into H.264 / HEVC** (a panel 833 px high) → encoder error → even sizes everywhere.
- ✗ **A strip a few pixels wide through NVENC** (30 px) → `Frame Dimension less than the minimum supported value` → encode it wider and mask, or use libx264.
- ✗ **An SVG as input** → `Decoding requested, but no decoder found for: svg` on common Windows builds → rasterise it first (a browser, or the PNG export of the logo).
- ✗ **`ffprobe a.mp4 b.mp4`** → `Argument 'b.mp4' provided as input filename, but 'a.mp4' was already specified` → one file per call.
- Comparing colour or brightness between a render and its source: decode both as raw Y planes with the same matrix; a bt709 limited tag on one side and none on the other made a "15 % brighter" false alarm.

## Machine load

- Several encodes at once next to a browser render or an NLE build compete for the GPU encoder, disk and memory: renders slowed, and memory-pressure kills took out background jobs. One heavy encoder job at a time beside a browser render; stop by process id when the human needs the GPU, and write where to resume.
