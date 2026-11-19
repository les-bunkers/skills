# DaVinci Resolve through the MCP: what works, what failed

The rules and the failure log behind `/videos:resolve`. Everything here was observed on DaVinci Resolve Studio 21.2 (Windows) through Blackmagic's own MCP server, in roughly 830 tool calls over 18 agent sessions of real editing work (multicam talking-head shoots, podcasts, course projects with thousands of clips). Each area lists what works, then the attempts that failed in this form:

- ✗ **what was tried** → what happened → **what to do instead**

"Silently" means the call returned success (or a plausible value) and nothing changed. A newer Resolve may fix some of these: run `get_whats_new` with the version you know and retest before relying on a workaround.

## The server and its tools

- Blackmagic's MCP ships with Resolve Studio 21.x (`ResolveMCP.exe` / bundle `DaVinciResolve.mcpb` in the Resolve install folder). It has no skill and no prompts: its tool descriptions, `search_scripting_api`, `get_scripting_api`, `get_scripting_docs` and `get_whats_new` are the docs. Community Resolve MCP servers ship their own skill files for their own tools; don't mix them with this one.
- The server stays up when Resolve is closed or hung. Its tools then answer `DaVinci Resolve is not running, start it with the launch_resolve tool.` and `get_resolve_status` returns `{"running": false, "version": "21.2"}`. Hung Resolve: see App lifecycle.
- ✗ **Several agent sessions started in the same minute** → some listed the server under "failed to connect" with no reason → `/mcp` to reconnect, or a new session.
- Every session spawns its own `ResolveMCP` process; all of them drive the same Resolve (see One Resolve, many callers).

### Finding the API

- `search_scripting_api` takes `pattern`, a case-insensitive regex matched against names and descriptions of types, functions, constants and dict fields.
  - ✗ **`{"query": …}`** → `A non-empty pattern is required` → `{"pattern": …}`.
  - ✗ **`"def AddTrack"`** (stub source lines) → `Nothing in the scripting API matches` → `"^AddTrack$"`, `"AddTrack|DeleteTimelines"`, `"COMPOSITE_"`. Broad patterns truncate ("N more matches omitted").
- `get_scripting_api` `types` takes top-level names only. ✗ **One guessed name** (`"ClipInfo"`, `"MediaPoolClipInfo"`) → the whole call fails with `is not a top-level definition` → search first, then pass the exact names it printed (`AppendClipInfo`, `ImportClipInfo`, `TimelineItemPosition`, `RenderSettings`, `TransitionOptions`).
- `get_scripting_docs` `section` matches README headings. ✗ **A class or method name** (`"AddTransition"`, `"GalleryStillAlbum"`) or `"Looking up render settings"` → `Section … not found` → class docs come from `get_scripting_api`; render keys from `get_scripting_api types=["RenderSettings"]`; `as_file: true` writes the whole document to a temp file you can grep. `FusionToolReference.md` holds every Fusion tool's input IDs.
- `get_whats_new` `since` = the newest version you already know (`"21.2"`). ✗ **`since: "20.0"`** → ~35 KB of changelog in context.
- ✗ **Trusting the stubs for setter values**: `RenderSettings.VideoQuality` is documented as an int bitrate or `"High"`; both make `SetRenderSettings` return False on H.264 NVIDIA → set keys one at a time and read each return value.

## Running scripts

- `run_script` = sandboxed Python 3.14; `run_script_unsafe` = full OS access (files, subprocess, network). Both inject `resolve` and `project`; set `result` to return data.
  - ✗ **`open(...)` in `run_script`** → `NameError: name 'open' is not defined`.
  - ✗ **`import glob`** (or anything that imports `os`) → `PermissionError: import not allowed: os` → `run_script_unsafe`.
- **Timeout: default 10 s, max 60.** ✗ **A trivial read with no `timeout`** (track counts, 89 items × 5 getters) → `Script timed out after 10s` → always pass `timeout`.
- **A timeout does not stop the script.** The MCP stops waiting; the Python keeps running inside Resolve and its changes land: a timed-out relink applied 1,947 relinks, a timed-out track script added its track to every timeline, frame exports appeared on disk two minutes after the timeout. After any timeout: wait, read the state back in a separate call, rerun only what is missing. Write scripts idempotent (skip what is done).
- Resolve stays busy after a timed-out script: ✗ **the next quick read** → `timed out after 10s` → retry with `timeout` 60 after a pause.
- ✗ **Blocking Resolve calls inside a script** (frame renders waiting on a cold frame) → the call ran well past its own timeout; Claude Code moved it to the background after 120 s and the "timed out" result arrived minutes later → budget wall-clock time inside the script, counting the worst case of each blocking call (10 s per frame render): `while work and time.time() - t0 < 40: …`, return `left`, call again until `left == 0`. Same for long loops (330 Fusion comps or 25 `ReplaceClip` + ffprobe checks each took two calls).
- Subprocess time counts against the cap: ✗ **`ffprobe -count_frames` on 95-minute files inside the script** → cannot fit → probe outside Resolve first and pass the results in.
- Keep logic in a file and `exec(open(path, encoding="utf-8").read())` it from `run_script_unsafe`; set config globals before the exec (`MODE = "build"; ONLY = "<timeline>"`).
- Side effects before a traceback stay applied. ✗ **A readback crashed on the last line** (`round(None)`, a misspelled method) → the result said "error" although the import, build or deletion before it had happened → mutate, then read back defensively, and return a dict of what was done. Diagnostics go in a separate call after the mutating one.
- Proxy objects answer every unknown attribute with None instead of raising: `hasattr` is always True, and ✗ **`project.SaveProject()`** → `TypeError: 'NoneType' object is not callable` (it lives on the project manager: `resolve.GetProjectManager().SaveProject()`). ✗ **`dir(resolve)`** lists no constants (`COMPOSITE_*` absent) → `getattr(resolve, "COMPOSITE_NORMAL")` with names from the stubs (values are floats).
- Unknown setting and property names return `""`, `None` or `False`, never an error: ✗ **`GetSetting("colorSpaceInputGamma")`** → `""`; ✗ **`GetClipProperty("Gamma")`** → None; ✗ **`TimelineItem.GetProperty("Volume")` / `("Pan")`** → False (the names are `AudioVolume`, `AudioPan`…) → check names in the README sections "Looking up Project and Clip properties" and "Looking up Timeline item properties".
- ✗ **`raise SystemExit("render running")`** → `(script completed with no output or result)`, message lost → set `result = {"skipped": "…"}` and stop without raising.
- ✗ **A 69,000-character `result`** → over the tool-output cap, spilled to a file in the agent's tool-results folder → write big dumps to JSON from `run_script_unsafe` and return counts plus the path.
- ✗ **`GetClipProperty("File Path").split("/")`** on Windows → `IndexError` (Resolve returns backslashes) → normalise separators first.

## One Resolve, many callers

All sessions and the human share one Resolve: one open project, one current timeline, page and media-pool folder, one render queue, one frame renderer. Every failure below came from that:

- ✗ **Assuming the injected `project`** → a script built against the wrong project: the human had opened another project (from another disk database) for another session → assert `project.GetName()` at the top of every script.
- ✗ **`GetCurrentTimeline()` across calls** → V1 read 89 items, then 67; the agent told the human they were editing, when they had only clicked another timeline → look timelines up by name and put the name in every result.
- ✗ **A quick read while another session ran a 60 s script** → `timed out after 30s` (calls queue) → retry later.
- ✗ **Heavy analysis of the same 4K media while Resolve builds a new timeline** (thumbnails, waveforms) → a full join check was terminated twice and its background rerun killed for low memory before measuring anything → wait until Resolve settles, or analyse the extracted audio instead of the camera files.
- ✗ **Imports, markers or track toggles while another session renders** → refused or silently ignored (sections below) → `project.IsRenderingInProgress()` before any change.
- ✗ **Three agents grabbing frames at once** → the frame renderer wedged for half an hour (Frames).
- ✗ **`DeleteAllRenderJobs()`** → wipes everyone's queued jobs; ✗ **leaving completed jobs** → 28 stale jobs from other sessions the next day → delete only your own job ids after `Complete`.
- ✗ **A script that soloed its new tracks** → switched off the human's tracks and broke other sessions' A/B checks → add new tracks disabled (or enabled alone, on request) and leave every other toggle as found.
- ✗ **`DuplicateTimeline(name)` to test on a copy** → the copy became the current timeline: the human's view switched → save and restore the current timeline around it.
- ✗ **Renaming and filing timelines while the human worked in the same project** → inside the call everything read back right (new names, bins, keywords); a minute later, from a separate call, all of it was back as before the script, and in another run a timeline the script had just created was gone. No error anywhere. The log showed the window active, with page changes and a burst of project saves between the calls that no script had made. Cause not established (the human's undo is suspected; the key press was not seen) → no changes at all while the human is active, test timelines included: the log shows it (`Application state changed to Active` with no later `Inactive`, `Main view page is changed` or `Start saving project` lines you did not cause). Read the state back in a later call before reporting a change as done.
- Resolve keeps every media file used on a timeline open. ✗ **Re-rendering a placed file to the same path** → `EPERM: operation not permitted, rename …` on Windows; ✗ **and sometimes no error at all**: a batch overwrote two placed inserts in place and the clips in the edit changed under the editor → ALWAYS render a placed file to a new file or folder, then `MediaPoolItem.ReplaceClip(new_path)` (items stay where they are) and delete the old pool items once nothing uses them.
- A project with thousands of clips and 4K multicams holds 6–11 GB of RAM: ✗ **parallel render chains and local preview servers** got killed for low memory → close the big project before heavy batches.

Etiquette that follows: one agent changes Resolve at a time, the others read; every changing script saves and restores the current timeline, page, timecode, media-pool folder and render format; never open, close or switch projects while the human works.

## Organization: where things go, what they are called

One layout for every stage, every agent session and the human. `scripts/resolve_bins.py` holds it as code (`path_bin`, `move`, `snapshot`, `archive`, `multicam`, `tag`, `audit`); `resolve_build.py` and the audio cleanup file their output with it. A project the human organized another way on purpose keeps their layout: follow it and say so in the record.

```
01 Timelines/
  WIP/Latest/      working timelines: the video + its format       "Teleprompter demo 9x16"   "Teleprompter demo 16x9"
                   shorts cut from the long vertical               "Teleprompter demo 9x16 S01 price objection"
  WIP/Archive/     earlier versions                                 "Teleprompter demo 9x16 v02 2026-03-04 rough"
  Final/           approved cuts                                    "Teleprompter demo 9x16 v05 2026-03-09 approved"
  Scratch/         tests and throwaways
02 Footage/
  2026-03-02 Studio/           one folder per shoot: "<YYYY-MM-DD> <place or subject>"
    <take or source id>/       camera files as shot; extra microphone files in its sub-bin "Audio"
    Multicam/                  that shoot's multicam clips
03 Assets/
  Images/  Music/  SFX/        shared material
  Videos/<Video>/[<set>/]      graphics, overlays, B-roll and screen recordings made for one video
  Voice/<Video>/               cleaned, mastered or generated voice tracks
  Subtitles/<Video>/           SRT items
```

The numbers keep the three bins in this order in every sort mode. Nothing sits at the pool root, there is no fourth top-level bin and no "/" in a bin name (`B.clean`).

**Timeline names**

- In Latest a timeline carries the video's plain name, the one the human says out loud, followed by its format: `<Video> 9x16`, `<Video> 16x9`, `<Video> 4x3`. No date, version, stage or "final" in it: the name never changes while the cut does, so the record, scripts and the human's open tab keep pointing at the right thing. The format token is part of the name because the two cuts of one video live side by side and Resolve refuses two timelines with one name; it always matches the timeline's resolution (`B.audit` checks).
- A short cut from the long vertical: `<Video> 9x16 S<NN> <topic>` ("Teleprompter demo 9x16 S01 price objection"). NN counts up per video, two digits, never reused; the topic is two to four words the human would use. Sorted by name, the shorts sit right under the long 9x16 they come from.
- Two variants waiting for the human's choice: `<Video> <fmt> - <variant>` ("Teleprompter demo 9x16 - split panels"), both in Latest until they pick one.
- A frozen version: `<working name> vNN YYYY-MM-DD <state>` ("Teleprompter demo 9x16 v03 2026-03-04 tight"). NN is a whole number on two digits that counts up per working timeline across Archive and Final and is never reused (versions archived before the name carried a format count too). The date is the day it was frozen. The state is one or two words: `rough`, `tight`, `graded`, `mastered`, `approved` (those five are read back from the timeline's keywords when no state is passed), or what the edit is about to change (`before captions`).
- `B.tl_name(video, fmt, short=None, label=None, variant=None)` builds a working name, `B.parse(name)` reads one back (video, fmt, short, label, variant, version, date, state), `B.with_format(name, fmt)` swaps the token. A working timeline named before formats were named ("Teleprompter demo", 16:9): `B.add_format(project, name)` renames it from its resolution ("Teleprompter demo 16x9") and retags it; archived names stay as they are. Then update the record's `resolve:`.

**Formats: the long 9x16 first**

A video is cut once, as the long vertical (`<Video> 9x16`, 1080×1920): rough, tighten, audio, grade, layers, all on that timeline, until the human approves it. The approved 9x16 is frozen in Final, and only then two pieces of work start side by side, both made from it:

- `<Video> 16x9`: the same cut, sound, angles and grade on a 16:9 timeline, the framing and the layers redone for the wide frame (Timelines § Another format from the approved 9x16).
- The shorts, `<Video> 9x16 S01 …`: each a range of the approved 9x16 with everything on it, plus its own hook and end card.

The vertical goes first because it is the harder frame: a face and a caption fit a 9:16 crop only when every shot is framed for it, while a 16:9 frame holds whatever the vertical showed. Approving one cut keeps the 16x9 and the shorts from drifting apart from it.

**Before a big edit: snapshot**

A rebuild, a restructure, a re-time, a pass over the whole timeline or anything hard to undo by hand starts with a snapshot. `DuplicateTimeline` makes a copy; the COPY takes the version name and moves to `WIP/Archive`; the current timeline is restored. One marker or one cut needs none.

```python
import sys, importlib; sys.path.insert(0, r"<plugin>/scripts"); import resolve_bins as B; importlib.reload(B)
result = B.snapshot(resolve, project, "Teleprompter demo 9x16", "rough")   # {"snapshot": "Teleprompter demo 9x16 v02 2026-03-04 rough", …}
```

- A rebuild from a cut list (`resolve_build.py` with `replace`) files the timeline it replaces in the archive the same way (`B.archive`): hand edits survive in that copy.
- An approved cut: `B.snapshot(resolve, project, name, "approved", to=B.FINAL)`, per format and per short. The approved 9x16 is what the 16x9 and the shorts are made from. For a delivered video also export the project (`pm.ExportProject(name, path)`) next to the masters.
- Nothing in Archive or Final is edited or deleted. Delete only your own tests in Scratch.

**Clip names**

- Camera and audio clips keep their file name as clip name. The name links the clip to the file on disk, the transcript and the sync data; a renamed pool is unreadable to the next person. The camera goes in the clip's `Angle` metadata, the take in `Take`, the scene in `Scene`. A multicam needs ordered names only while it is created (Multicam).
- Footage bins mirror the shoot's folders on disk below the shoot level. Timelines and assets are filed by video.

**Keywords and smart bins**

- Every item carries keywords (`B.tag(item, …)`): the video's name, the shoot, and its kind. Kinds: `timeline` with `latest` / `archive` / `final` / `scratch`, the state, the format (`9x16` / `16x9` / `4x3`) and `short` for a short (`B.timeline_words(name)` gives video, format and `short` from the name); `camera`, `program`, `audio track`, `multicam`; `graphic`, `photo`, `reference`, `music`, `sfx`, `voice`.
- Resolve lists one smart bin per keyword under Smart Bins → Keywords in the media pool (User preferences → Editing → Automatic Smart Bins → keywords; the list is hidden → media pool "…" menu → Show Smart Bins). A click on the video's name shows its timelines in every format, its footage and assets across all bins; `latest` shows every working timeline, `9x16` every vertical one, `short` every short.
- A smart bin matches the keyword exactly as typed: use the words above, same spelling every time, and the video's name exactly as the working timeline carries it before its format token.
- Smart bins with other rules have no scripting call: the human creates those.

**Who files what**

| Made by | Goes in | Name | Keywords |
|---|---|---|---|
| ingest: camera and program files | `02 Footage/<shoot>/<source id>` | file name | shoot, `camera` |
| ingest: extra microphone files | `…/<source id>/Audio` | file name | shoot, `audio track` |
| ingest: multicam clip | `02 Footage/<shoot>/Multicam` | `MC <source id>` | shoot, `multicam` |
| rough, tighten: the build | `01 Timelines/WIP/Latest` | `<Video> 9x16` | video, `timeline`, `latest`, `9x16`, state |
| rough, tighten: its SRT | `03 Assets/Subtitles/<Video>` | file name | none (refused) |
| audio: cleaned and mastered tracks (one set serves every format) | `03 Assets/Voice/<Video>` | file name | video, `voice` |
| layers, B-roll, screen recordings | `03 Assets/Videos/<Video>/<fmt>[/<set>]` (layers are drawn per format) | file name | video, `graphic`, format |
| master: the approved cut, frozen | `01 Timelines/Final` | `<Video> 9x16 vNN <date> approved` | video, `timeline`, `final`, `9x16`, `approved` |
| wide: the 16:9 from the approved 9x16 | `01 Timelines/WIP/Latest` | `<Video> 16x9` | video, `timeline`, `latest`, `16x9` |
| clips: a short from the approved 9x16 | `01 Timelines/WIP/Latest` | `<Video> 9x16 S01 <topic>` | video, `timeline`, `latest`, `9x16`, `short` |
| anything you are only trying | `01 Timelines/Scratch` | free | `scratch` |

`result = B.audit(project)` after a stage that added things: it counts strays at the root, timelines outside `01 Timelines`, version names in Latest, archive names off the pattern, working timelines without a format token (`latest_without_format` → `B.add_format`), names whose format differs from the timeline's resolution (`format_not_resolution`), renamed footage clips and items without keywords. `ok` True = nothing to fix.

## Media pool and import

- `mp.ImportMedia([path, …])` with plain path strings; it imports into the current folder → `mp.SetCurrentFolder(bin)` first, with the bin the layout names (Organization; `resolve_bins.path_bin(mp, "03 Assets/Videos/<Video>")` finds or makes it), and restore the folder after.
  - ✗ **`ImportMedia([{"FilePath": p}])`** (the stub's dict form) → imports nothing.
  - ✗ **Importing while Resolve sits on Deliver or renders** → `[]` for every file type (`GetCurrentPage()` returns None mid-render); `MediaStorage.AddItemListToMediaPool` same → wait for `IsRenderingInProgress()` False, `OpenPage("edit")`, import, restore the page and `mp.SetCurrentFolder(folder0)`.
- ✗ **`mp.MoveClips(items, folder)` and `mp.MoveFolders(folders, target)` returned True and copied** → every clip, timeline and bin appeared in the target under a new unique id while the originals stayed where they were (983 pool items became 1,945, 59 timelines became 118). Cause: Resolve's window still believed Alt was held, left over from an Alt+Tab away from it, and Alt-drag duplicates in the media pool. `resolve.Fusion().GetModifiers(True)` showed `AltModifier: True`; the page and the current folder made no difference → read the modifiers before any move, clear a stuck one with a click or a mouse move inside the Resolve window (a `WM_MOUSEMOVE` posted to the main window does it), and prove every move: the same `GetUniqueId()` sits in the target and the pool and timeline counts are unchanged (`resolve_bins.move` reads the modifiers and checks the ids; for a bin, `folder.GetParentFolder().GetUniqueId()`).
  - Cleaning up after such a copy: every item whose id was absent from a pool listing taken before is a copy → `DeleteTimelines` the copy timelines first, then `DeleteClips`, then `DeleteFolders`. A timeline item that had no pool clip linked itself to the copy of its file while the copy existed, and went back to unlinked when the copy was deleted → compare the timelines' source ids with the listing afterwards.
- A bin has no rename call → create the new bin, move the content (checked), delete the empty old one. `Folder.GetUniqueId()` and `GetParentFolder()` exist.
- ✗ **`mp.AddSubFolder(parent, name)` with a name that exists** → no error: a second bin with the same name, and it becomes the current folder → look the bin up by name first and restore the current folder (`resolve_bins.path_bin`).
- Keywords: `item.SetMetadata("Keywords", "a, b")`, one comma-separated string, read back with `GetMetadata("Keywords")`. ✗ **On Subtitle items** → False (56 of 56): they stay untagged, filed by bin only.
- ✗ **`SetClipProperty("Clip Name", x)`** → returned False and the clip was renamed → read back with `GetName()`, never trust the return.
- ✗ **`GetClipProperty("Usage")` to decide what is unused** → the count did not match the uses found by walking the timelines → walk every timeline's items and compare `GetMediaPoolItem().GetUniqueId()`.
- ✗ **Overwriting an imported file on disk** → the pool item keeps the old content and length → `item.ReplaceClip(item.GetClipProperty("File Path"))` re-reads it.
- ✗ **`ReplaceClip` on a tagged pool clip** → its keywords came back empty (one clip swapped in a bin of 60 tagged ones: the only one left without keywords) → read `GetMetadata("Keywords")` before the swap and set it back after.
- `ReplaceClip(new)` swaps media under every cut when the frame count matches. ✗ **Guarding the swap on equal file size** → every WAV "differed" by 4 header bytes → compare ffprobe streams (type, codec, rate, channels, sample or frame count).
- Relink after a drive move: `mp.RelinkClips(clips, folder)` per folder group, in batches, `SaveProject()` between batches. ✗ **2,500 clips in one call, then back-to-back heavy batches** → timeouts, then Resolve stopped responding and Windows closed it (App lifecycle).
- Counting offline media with `os.path.exists(GetClipProperty("File Path"))`: ✗ image sequences carry range paths (`name_[2-4].png`) and always look missing; multicam, compound and generator items have an empty path → treat `[` paths as sequences, count empty paths apart.
- ✗ **VP9-in-MP4 downloads (YouTube)** → "Online" in the pool but Media Offline on most rendered frames → transcode to H.264 (`ffmpeg -c:v h264_nvenc` or libx264, constant frame rate) and `ReplaceClip`; cuts survive. AV1 decodes fine.
- WAV clips take the project frame rate at import. ✗ **`SetClipProperty("FPS", …)` on a WAV** → False → on a timeline with another rate, frame-based `startFrame` lands on wrong source times → wrap the audio in a mov with a tiny black video at the timeline rate (`ffmpeg -f lavfi -i color=black:s=64x36:r=<fps> -i a.wav -map 0:v -map 1:a -t <dur> -c:v libx264 -preset ultrafast -crf 51 -c:a pcm_s24le out.mov`) and append with `mediaType: 2`.
- Project library from an old machine: it lives per user under `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Resolve Disk Database`, not under ProgramData. Copy it, then Project Manager → Project Libraries → Add; accept the upgrade only on the copy (an upgraded library no longer opens in older Resolve). `pm.GetDatabaseList()` then lists it.

## Timelines

- `CreateEmptyTimeline(name)` fails on an existing name → the working timeline keeps the video's plain name (Organization): file the old one in the archive under its version name first (`resolve_bins.archive`), or delete it when it is your own test (`mp.DeleteTimelines([t])`). The timeline lands in the current media-pool folder → `mp.SetCurrentFolder(resolve_bins.path_bin(mp, "01 Timelines/WIP/Latest"))` before, restore after.
- `ImportTimelineFromFile(drt)` names the timeline after the FILE, lands in the current bin, links existing media and becomes current.
- Frame rate and resolution: project settings change only while the project has no timelines. Per timeline: on the new empty timeline `SetSetting("useCustomSettings", "1")`, then `timelineFrameRate`, `timelineResolutionWidth` / `Height` (works, e.g. a 1080×1920 timeline in a UHD project).
- A 9x16 timeline from 16:9 sources: also `SetSetting("timelineInputResMismatchBehavior", "scaleToCrop")` before any clip goes on, so every source fills the frame (centre crop: a 3840×2160 source shows its middle 1215×2160) instead of shrinking into a letterbox; read every setting back. `resolve_build.py --format 9x16` does this on a timeline made under a temporary name and renames it only once the size reads back right; its result `format` says `want` / `got` (the multicam angle switch re-imports the timeline from a .drt: `got` is read after it).
- Framing a vertical: the centre crop is a start. Per V1 piece, Inspector `Pan` (and `ZoomX` / `ZoomY` for a punch-in) puts the face where the project's framing rule wants it; `TimelineItem.SmartReframe()` tracks the subject inside the already filled frame (Resolve's own doc: set the size and `scaleToCrop` first). Check with `get_thumbnails` across each camera: a face cut by the frame edge or a speaker off the crop on a side camera = framing to fix before review.
- `AppendToTimeline([{mediaPoolItem, startFrame, endFrame, recordFrame, trackIndex, mediaType}])`:
  - `endFrame` is exclusive (100 → 124 = 24 frames). ✗ **`endFrame: n - 1`** → all 51 inserts one frame short.
  - `recordFrame` is absolute (`tl.GetStartFrame() + offset`, 86400 at 01:00:00:00 / 24 fps) and may be fractional on any clip; `startFrame` / `endFrame` round to whole source frames.
  - No `mediaType` = linked picture and sound; `1` = picture, `2` = sound. `trackIndex` needs the track to exist (`AddTrack`).
  - ✗ **One bad item in the list** (a piece shorter than ~2 frames rounds to zero length) → the whole batch fails (0 of 196 placed) → give every piece ≥ 1 frame; append one by one when hunting the bad item.
- `InsertGeneratorIntoTimeline("Adjustment Clip")` is an insert edit at the playhead on V1 (it ripples the timeline by 120 frames) → put the playhead past the last clip, insert, then `tl.MoveItems([it], {"trackIndex": k, "startFrame": f})`. The adjustment clip stays 120 frames: `SetRightOffset` returns False, `GetLeftOffset` / `GetRightOffset` None.
- Reading cuts back (after a human moved one): `GetSourceStartFrame()` / `GetSourceEndFrame()` (end exclusive, same as `endFrame`), `GetStart()` / `GetEnd()` minus `tl.GetStartFrame()` = record frames. Read the track whose source the transcript was made on.
  - ✗ **Audio cross fades** come back from `GetItemListInTrack("audio", k)` as 1-frame items whose source getters all return None (104 "items" for 89 pieces) → skip items with `GetSourceStartFrame() is None`.
  - ✗ **Integer getters on sub-frame pieces** disagree (`GetSourceStartFrame()` 696 vs `GetLeftOffset()` 697) → use the fractional forms (`GetLeftOffset(True)`, `GetStart(True)`, `GetEnd(True)`).
  - Items of a multicam clip return multicam frames → subtract the angle's offset inside the multicam.
- ✗ **Placing new tracks from the cut list after the human trimmed the timeline** → 98 items against 65 real ones → mirror the timeline's current items, never the plan.

### Another format from the approved 9x16

Not yet run end to end: read every step back, and log what fails here.

- 16x9, route A (keeps everything the human did by hand: angles, J-cuts, fades, audio tracks, grades on the clips): `src.DuplicateTimeline("<Video> 16x9")` from the approved `<Video> 9x16` (the copy becomes current: restore), file it in Latest (`B.move`) and tag it (`B.timeline_words`). Then `SetSetting("timelineResolutionWidth", "3840")` / `Height` `"2160"` (or the project's own 16:9 size) and read back with `B.fmt_of`. Reset the vertical framing on every picture item of the cut (`SetProperty("Pan", 0)`, `"Tilt"`, `"ZoomX"` / `"ZoomY"` 1.0, or the project's wide framing rule), turn off the 9x16 layers tracks (they are drawn for the tall frame), place the 16x9 layers on their own tracks. 9:16 layouts that stack cameras (two panels one above the other) become their 16:9 counterpart (side by side), on the same frames.
- 16x9, route B (when route A's size change returns False or reads back wrong on a timeline with clips): read the cuts of the approved 9x16 (`resolve_build.py --mode read_cuts --name "<Video> 9x16"`), build `--format 16x9` from that cut list, place the mastered voice track at frame 0 (the cut is the same, so the master lines up), redo the grade from the project's LUTs, then the 16x9 layers. Hand edits that are not in the cut list (fades, extra audio tracks, nudged angles) have to be compared with the 9x16 and redone.
- A short: `src.DuplicateTimeline("<Video> 9x16 S01 <topic>")` from the approved 9x16, filed and tagged, then `tl.DeleteClips(items outside the range, True)` (ripple) on every track, an item that straddles an edge trimmed with `SetLeftOffset` / `SetRightOffset` and read back (`GetStart(True)`, `GetEnd(True)`). Its hook goes on a layers track at frame 0, its end card after the last word. Count after: length = the range plus the end card, no gap at the start, the markers of other shorts gone.
- The approved 9x16 itself never changes for this: copies only.

## Tracks and clips

- ✗ **`SetTrackEnable` on the Deliver page** (where a render leaves Resolve) → returns True, changes nothing → `resolve.OpenPage("edit")` first and after every render. `TimelineItem.SetClipEnabled` works on any page.
- ✗ **`GetIsTrackEnabled` on a timeline that isn't current** → False for every track; `DeleteTrack` on one → False → `SetCurrentTimeline` first (restore after), or read a DRT export (`Sm2TiTrack` `Flags`: 0 on, 2 off).
- ✗ **Appending to a new subtitle track** (`AddTrack("subtitle")`) → returned a 1-item list, added nothing: the track was created disabled → `SetTrackEnable("subtitle", k, True)` before appending.
- A mono clip on a mono track plays 3 dB lower on the stereo bus (pan law) than a dual-mono stereo clip → `AddTrack("audio", "stereo")` and a 2-channel file for A/B tests.
- No getter for track mute or solo (neither the API nor the DRT flags carry it) → the human checks the mixer.
- `SetClipsLinked([v1, a1, a2], True)` per segment so trims move together.

## Subtitles

- An `.srt` imports as a Subtitle media item. ✗ **Appending it with a `recordFrame`** → all cues land after the last clip → append subtitles first on the empty timeline, or append then `tl.MoveItems(cues, {"trackIndex": k, "startFrame": start + round(first_cue_s * fps)})` (moves the first cue there, the rest relative; 193 cues, max error 0.49 frame).
- ✗ **Shipping a subtitle track without reading it back** → a generator bug (one character per line) went into review renders → after every append compare each cue's `GetName().replace(" ", " ")` with the SRT text (Resolve shows SRT line breaks as U+2028).
- Re-importing a changed SRT: `DeleteClips([old])` then `ImportMedia`; subtitle items already on timelines survive. Clamp the last cue to the timeline end (it overhung by 0.3 s).
- ✗ **`GetLeftOffset(True)` on subtitle items** → None.
- A subtitle track has one style for every cue and no per-word animation; designed social captions belong in graphics layers ([layers](layers.md)).

## Markers

- `tl.AddMarker(frame, color, name, note, duration)`; `frame` = offset from the timeline start. Colors: Blue Cyan Green Yellow Red Pink Purple Fuchsia Rose Lavender Sky Mint Lemon Sand Cocoa Cream.
- ✗ **A marker on a frame that already has one** → False, nothing added (2 of 17 lost) → move to the next free frame (`while f in tl.GetMarkers(): f += 1`) and count markers afterwards.
- ✗ **Adding markers while any render runs** → False on every call, no error (0 of 20; 20 of 20 right after the render ended) → check `IsRenderingInProgress()` first.

## Multicam

- `mp.CreateMulticamClip(items, {"name", "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_AUDIO, "angleNameMode": resolve.MULTICAM_ANGLE_NAME_CLIP, "useFullClipExtents": True, "createBinForSourceClips": False, "frameRate": …})` → ~5 s for five 4K angles.
  - ✗ **Defaults** → sources moved into a new bin (`createBinForSourceClips` True) and the clip cut to the overlap of all angles.
  - ✗ **Ordering the list** → ignored: angle order = clip NAME sort → give the pool clips ordered names for the call (`SetClipProperty("Clip Name", "1 …")`; files untouched).
  - ✗ **Leaving those ordered names on the clips** → 173 camera clips no longer showed which file they were, and every search by file name failed → the multicam keeps the angle names it was made with: after the clips got their file names back, timeline items still read `<multicam> - 2 <angle>` and a DRT export listed the same angles → rename, create, restore the file names in a `finally`, camera in the `Angle` metadata (`resolve_bins.multicam`).
- Offsets: with full extents the multicam starts at the earliest angle → a source frame sits at `+ max(0, −min(d_a) × fps)` inside it (d_a = audio lag of angle a against the program). Check the multicam's frame count against the computed span; one frame off → compare thumbnails of both. ✗ **An audio cross-correlation window of ±0.5 s** → missed a 0.656 s lead → widen on failure.
- No API call sets a multicam item's active angle.
  - ✗ **`PerformMulticamSmartSwitch` with explicit settings** → None in 0 s, nothing switched (one key is invalid, not isolated); `PerformMulticamSmartSwitch({})` → Resolve chooses the angles itself (77 s for 40 s of timeline).
  - ✗ **Putting the other cameras on V2 / V3** → breaks the editor's multicam workflow → switch through a `.drt` round trip: `scripts/resolve_multicam_angles.py` ([engine](engine.md) § Resolve).
  - ✗ **DRT field `CurrentSelectorIdx`** → thumbnail data, not the angle. The angle is in the item's `FieldsBlob` (`"Camera N"`); raw (0x80) on appended items, zstd (0x81) on items SmartSwitch wrote.
  - ✗ **OTIO export to read angles** → each multicam item becomes a stack holding every angle as an enabled track; no field names the active one.
- Multicam items have no file path: ✗ **tools that map timeline items to files** skip every one → render the track as it plays (Audio Only preset) and work on that mixdown.

## Audio edits

- Sub-frame audio edits work when the user preference "Align audio edits to frame boundaries" is OFF: append rounded inward, then `item.SetLeftOffset(x, True, False)` / `SetRightOffset(x, True, False)` and `tl.MoveItems([it], {"startFrame": f}, True, True)` take fractional frames; read back with `GetStart(True)` / `GetLeftOffset(True)`.
- `item.AddTransition({"type": "Cross Fade +3 dB", "category": "audio", "position": "end", "alignment": "center", "duration": 1})` on abutting items. ✗ **Expecting a centred 1-frame fade** → it lands on [join − 1, join] → put both audio edges half a frame past the splice. `SetFades` takes whole frames only.
- Level control by API is gain only: `Timeline.NormalizeAudioLevel`, `TimelineItem.AnalyzeAudioLevels`, `AudioVolume`, Voice Isolation and Dialogue Leveler properties (readable on the current timeline only; ✗ **`GetVoiceIsolationState` on other timelines** → every track "off", untrusted).
  - ✗ **Reaching a Fairlight compressor or limiter** → not exposed; gaining talking heads to −14 LUFS pushed peaks to +6…+11 dBTP → master the audio outside Resolve and place it as its own track.
  - ✗ **Reading Fairlight FX** (`GetFairlightPresets()` → `{}`, the Fusion `Fairlight` stub is empty) → a DRT export holds the FX names and normalised preset values only (no units, no insert point). Applying works: save a Fairlight preset once by hand, then `project.ApplyFairlightPresetToCurrentTimeline(name)`.

## Rendering

- Start from a stock preset, then override only what you need: `LoadRenderPreset("YouTube - 2160p")`, `SetRenderSettings({"TargetDir", "CustomName", …})`, `AddRenderJob()`, `StartRendering([job_id], False)`, poll `GetRenderJobStatus(job_id)` until `Complete` (ffprobe reads a duration from a file Resolve is still writing).
  - ✗ **A hand-built job** (`SetCurrentRenderFormatAndCodec("mp4", "H264_NVIDIA")` + settings) after audio-only renders → audio-only file while the job said `IsExportVideo: True`; software H.264 the same → load a stock preset first.
  - ✗ **The first job of a batch, a lone job included** → sometimes no video stream (AAC + data, done in < 1 s) on two different timelines and both H.264 encoders → queue a 1-second throwaway job first (`MarkIn` start, `MarkOut` start + 24) and ffprobe every output for a video stream.
  - ✗ **`StartRendering` on the first call** → sometimes False → retry up to 5 times, 1 s apart.
  - ✗ **One preset, then a job per timeline** → job 1 at the preset size, jobs 2+ at each timeline's resolution → per timeline: `SetCurrentTimeline` → `LoadRenderPreset` → `SetRenderSettings({"FormatWidth", "FormatHeight", …})` → `AddRenderJob`; read `GetRenderJobList()` sizes before starting.
  - A 9x16 timeline: the stock YouTube presets are landscape (1920×1080, 3840×2160) → after the preset set `FormatWidth` / `FormatHeight` to the timeline's size and read the job's size back; a job whose size differs from its timeline is deleted, never started (`render_finals.py` does both; not yet seen on a real vertical render: log the result here).
- Bitrate: ✗ **`VideoQuality` as an int or `"High"` on H.264 NVIDIA** → False; ✗ **on a stock H.264 preset** a 40 Mbps request returned True and rendered 10.6 Mbps → for a high-bitrate H.264 render ProRes 422 (`LoadRenderPreset("H.264 Master")` + `SetCurrentRenderFormatAndCodec("mov", "ProRes422")`) and encode with ffmpeg. `H.264 Master` alone = `.mov`, ~123 Mbps at UHD. `ReplaceExistingFilesInPlace` → False. `YouTube - 2160p` = mp4, ~11 Mbps, keyframe every 24 frames.
- Audio only: `LoadRenderPreset("Audio Only")` + `{"SelectAllFrames": False, "MarkIn", "MarkOut", "ExportVideo": False, "ExportAudio": True, "AudioFormat": "wav", "AudioCodec": "lpcm", "AudioSampleRate": 48000, "AudioBitDepth": 24}` → stereo WAV in under a second.
  - ✗ **`GetRenderCodecs("wav")`** → `{}` (WAV is an audio format: `GetAudioRenderFormats()`, `GetAudioRenderCodecs("wav")` = `{"Linear PCM": "lpcm"}`); ✗ **`SetCurrentRenderFormatAndCodec("wav", …)` without the preset** → `StartRendering` False.
  - ✗ **"Audio Only" loaded right after video jobs, without `AudioFormat` / `AudioCodec`** → `.mov` (pcm_s24le) on 2 of 4 jobs → always pass both; check the extension before feeding the file on.
- ✗ **One script reading `GetRenderJobList()` + `GetRenderPresetList()` + `GetCurrentRenderFormatAndCodec()` + `project.GetSetting()`** → `Script timed out after 30s` with no other caller seen → one getter per call (`GetRenderJobList()` alone returned at once).
- `GetRenderJobList()` showed `TimelineStartTimecode "00:48:00:00"` for a 23.976 timeline starting at frame 86400 (86400 read at 30 fps: a 30 fps timeline had been current before); setting `"01:00:00:00"` returned True and changed nothing in the output. Read job fields with that in mind.
- `MarkIn` / `MarkOut` are absolute frames and `MarkOut` is INCLUSIVE (a 253-frame timeline renders `MarkIn` 86400 → `MarkOut` 86652), unlike `AppendToTimeline`'s `endFrame`.
- `GetCurrentRenderFormatAndCodec()` → ✗ `{"format": "unknown", "codec": ""}` before anything was chosen in the session and right after loading "Audio Only" → save it before loading a preset, restore it only when it was known.
- After rendering Resolve stays on Deliver → `OpenPage("edit")`; restore the render format; delete your job ids.
- Speeds seen with NVENC on a high-end card: UHD 23.976 → 1080p at 15–25× real time; UHD → 720p with burned-in subtitles ≈ 8×; a 10-second UHD short in ~2.3 s (`GetRenderJobStatus` → `TimeTakenToRenderInMs`).
- Checking renders: ✗ **"render with A1 vs render with A2 correlate at 1.0" as a sync proof** → identical content can also mean the toggle failed → rebuild the expected mix from the sources and the cut list and least-squares fit the render. ✗ **Comparing a render's brightness to the source with ffmpeg** → a "15 % brighter" false alarm (the render is tagged bt709 limited, the source untagged) → compare raw Y planes.

## Frames and visual checks

- `get_thumbnails source=timeline` after every build (a grid over the timeline; the playhead never moves); `get_image` for one frame (`roi_*` crops a region at full detail). Both render the CURRENT timeline only (they take no timeline name). Subtitle tracks render in neither.
- `Timeline.ExportFrame(frame, {"outputType": "file", "filePath": …, "maxDimension": …})`, `TimelineItem.ExportFrame` and `MediaPoolItem.ExportFrame` rendered frames of timelines that were NOT current (24 frames on 8 timelines, ~0.25 s each) → grab frames of any timeline without touching the human's view. Earlier "current timeline only" failures happened while the renderer was wedged (below): try without switching first.
- Every frame render has a 10 s cap.
  - ✗ **Cold heavy frames** (UHD multicam, Fusion, noise reduction) → `Failed to render frame … 10 second timeout` → retry inside one `run_script_unsafe` (within the time budget), or `SetCurrentTimecode` + `project.ExportCurrentFrameAsStill(path)` up to 6 tries; once cached they return in 0.1–3 s.
  - ✗ **A smaller `max_dimension`** (1024 → 640 → 480), as the error suggests → no change.
  - ✗ **`get_thumbnails` with one slow frame** → the whole grid fails, no partial result → fewer frames, or single frames.
  - ✗ **Three agents rendering frames on one Resolve** → for half an hour every uncached frame failed at exactly 10 s, plain H.264 clips too, while cached frames still came back; `MediaPoolItem.ExportFrame` failed at exactly 3 s → retries never recovered; unsolved (a restart was not tried) → one agent renders frames at a time; fall back to ffmpeg frames of the source (ungraded).
  - ✗ **Counting every failed frame as a renderer timeout** → part of an audit's failures were items whose media sat on drives that were offline → `os.path.exists(item.GetMediaPoolItem().GetClipProperty("File Path"))` before any frame export; report offline media apart from timeouts.
  - ✗ **`ExportFrame` into a folder that doesn't exist** → `File specified by 'filePath' is not accessible` (looks like a render failure) → create the folder first.
- ✗ **`get_image source=media_pool_clip` with a name shared by an `.srt` item of the same stem** → `Item is not a clip or a generator` → use the full clip name with its extension, or a timeline source.
- ✗ **Judging a subtle effect from one frame** (a 1.18× zoom "looked right" and had not rendered) → compare the same frame with the effect on and off, or against the source frame, and measure the difference.
- ✗ **A desktop screenshot to see Resolve's state** → captures whatever window is on top (the human's private apps) → read state through the API, `get_image`, or a capture of the Resolve window alone (PrintWindow on its process's main window).

## Grade and LUTs

- `describe_grade` reads an item's nodes. A LUT is exact only for colour nodes; windows, masks, blur, noise reduction, grain and vignettes don't survive a LUT.
- `item.ExportLUT(resolve.EXPORT_LUT_65PTCUBE, path)` on a V2 adjustment clip of the current timeline → True, and ffmpeg + that LUT matched Resolve's graded still to 1/255 (`project.ExportCurrentFrameAsStill`).
  - ✗ **`ExportLUT` on items of another project** (65- and 33-point, current and non-current timelines, grades inside multicam or compound clips, Edit page) → False every time, no file. The cause is not isolated: log `resolve.GetCurrentPage()` and the item type when it fails, and try from the Color page.
  - ✗ **`GetNodeGraph()` / `describe_grade` on multicam or compound items** → one empty node; the real grade sits inside → decode it from a DRT export.
  - ✗ **`GalleryStillAlbum.ExportStills(...)` on the Edit page** → False, no files.
  - Fallback when no exact LUT comes out: fit a 33-point LUT from frame pairs (the ungraded source frame and Resolve's graded still of the same frame, several frames across the set) → 0.5–1.6/255 off on 7 looks. It carries colour nodes only (no Midtone Detail, noise reduction, beauty, windows or qualifiers), so say "fitted" wherever it is used.

## Fusion, effects and templates

- Resolve FX: `Effect` objects expose only `GetInfo`, enable, index and `GetFusionComp`; parameters can be neither read nor set. `AddEffect("ofx", id)` adds defaults → clone a look with `ref.CopyAttributes([targets], {"videoPlugins": True, "fusionEffects": True, "position": True, "allZoomAttributes": True})`.
- Templates (`.setting`) go under `Fusion/Templates/Edit/{Transitions,Effects,Titles,Generators}/<folder>/` in Resolve's per-user support folder (`fu.MapPath("Templates:")`) and appear after a Resolve restart. Then `tl.InsertFusionTitleIntoTimeline(<display name>)` (at the playhead, 120 frames; ✗ **14 titles inserted with the playhead 72 frames apart** → they collided and the timeline was redone → move the playhead ≥ 120 frames between inserts, or move/trim each title right after inserting it and read positions back), `item.AddTransition({"category": "fusion", "type": <display name>, …})`, `item.AddEffect("fusion", "Fusion/Effects/<folder>/<name>")` (ids from `resolve.GetEffectsList("video")`). Effect templates on adjustment clips render (zoom templates verified). Macro inputs: `item.GetEffects("fusion")[0].GetFusionComp().FindTool("MacroTool1").GetInput("Input10")` / `SetInput`.
- Keyframed zooms and moves: the API has no keyframe setter for Edit-page Inspector properties (`SetProperty` sets a static value).
  - ✗ **`TimelineItem.AddFusionComp()` on a multicam item** → comp created, wired and keyed, `GetInput` reads the right values, and nothing renders (`get_image`, still export and a Deliver render all unchanged); the Fusion-page route (`OpenPage("fusion")` + `fu.GetCurrentComp()`) the same.
  - ✗ **`AddFusionComp()` on an adjustment clip** → didn't render; `SetProperty("ZoomX", …)` on an adjustment clip → False.
  - `AddFusionComp()` on plain file clips renders (330 clips with a mask). The clip comp works in SOURCE coordinates, before the Inspector crop and zoom.
  - What works: Fusion effect templates on adjustment clips, clip comps on plain clips, and real Inspector keyframes on adjustment clips written into a DRT (zooms and pans on two timelines; rendered frames matched the computed zoom to 4 decimals). Route: duplicate the timeline, drop one placeholder adjustment clip per region past the end of the copy (no ripple), export the copy as DRT, then offline set each placeholder's start and duration and write its transform filter (filter 4: params 40 / 41 pan / tilt, 42 / 43 zoom X / Y; keyframes = 16-byte entries: u32 source frame, u16 0, u16 flags 12, f64 value; an ease = one key per frame from your own curve), `ImportTimelineFromFile`, compare the item, subtitle and marker counts with the source timeline, delete the copy. The encoding was read from a DRT of a timeline keyframed by hand: export one of yours and compare before trusting it on another Resolve version.
- ✗ **`ConnectInput` with a wrong input id** (`"GarbageMatte"`; the id is `"Garbage.Matte"`) → no error, nothing connected → list ids with `[i.GetAttrs()["INPS_ID"] for i in tool.GetInputList().values()]` and read the connection back (`tool["Garbage.Matte"].GetConnectedOutput()`). Tool names differ from their type (`AddTool("EllipseMask")` → `Ellipse1`).
- ✗ **BezierSpline keys with `LH` / `RH` handles** → stored absolute, read relative: values overshoot → one key per frame from your own easing, or keys without handles (linear). `AddModifier("Size", "BezierSpline")` leaves a key at frame 0 = the current value → overwrite it. Clip comp frame 0 = the item's first frame.
- ✗ **Addressing comps by `comp.GetAttrs()["COMPS_Name"]`** (`Composition58`) → not the item's comp name (`Composition 1`) → use `GetFusionCompNameList()`, `GetFusionCompByName()`, `RenameFusionCompByName()`.
- ✗ **`DeleteFusionCompByName` on the active comp** → False; with duplicate names, rename and delete hit the first → rename old comps uniquely, `LoadFusionCompByName(new)`, then delete. `LoadFusionCompByName` on the already active comp returns False (harmless).
- ✗ **`ExportFusionComp(path, 1)`** on transition items and on an adjustment clip's comp → False → `item.GetFusionCompByIndex(1).Save(path)` (works on transitions). Export a macro with `tool.SaveSettings(path)` on the macro itself: ✗ **on an inner group** → a `.setting` without `MainInput1/2` + `MainOutput1` that won't work as a transition.
- ✗ **Reading every input of every tool** (`GetInputList()` + values across nested macros) → 30 s timeout → list names once, read only the macro's inputs (< 1 s per item).
- ✗ **`bmd.readfile`, `io.open`, `GetRegList(CT_Tool)` inside `resolve.Fusion().Execute(lua)`** → the chunk aborts silently (`fu.GetData` → None) → validate `.setting` files in Python.
- Fonts installed per user while Resolve runs render in Text+ without a restart.
- Untested so far: crop units and Pan/Tilt scale on vertical timelines, transition easing through the API, OGraf / Lottie graphics.

## Alpha layers

- `item.SetClipProperty("Alpha mode", "Straight")` on imported ProRes 4444 layers works and reads back ([layers](layers.md)).
- `CompositeMode` constants: `getattr(resolve, "COMPOSITE_…")` (see Running scripts).
- ✗ **Checking a layer with ffmpeg `overlay` of a seeked input on a lavfi colour source** → empty composites (timestamps don't line up), and `metadata=print` is hidden by `-v error` → read the alpha plane directly (`ffmpeg -ss <t> -i layer.mov -frames:v 1 -vf format=rgba -f rawvideo -`) or check the composite in Resolve with `get_image`.

## Reading a project: exports

- `tl.Export(path, resolve.EXPORT_DRT, resolve.EXPORT_NONE)` works on timelines that are not current (0.1–2.6 s each). A `.drt` is a zip of XML; tag names like `ListMgt::Tag` need `re.sub(r"<(/?)(\w+)::(\w+)", r"<\1\2__\3", raw)` before ElementTree. Binary fields: u32 2, u32 length, then 0x80 raw or 0x81 zstd payload (protobuf-like). Clip `Start` may read `86745|<hex>` (sub-frame).
- `EffectFiltersBA` filters: 4 = transform (40 Pan, 41 Tilt, 42/43 Zoom, 47 Rotation), 2 = composite (0 mode, 1 opacity), 6 = crop, 50/109 = a Resolve FX instance, 100 = Push transition, 124/126/134 = audio volume, EQ, pitch. Keyframes = param field 10: `ffffffe0` + 16-byte entries (u32 frame, u16 0, u16 flags, f64 value).
- Inner transforms and grades of compound and multicam clips have no API handle (✗ **`GetProperty("ZoomY")` on a compound clip** → defaults) → DRT.
- ✗ **FCPXML 1.10 export** → a folder bundle (`name.fcpxml/Info.fcpxml`; the path's file size reads 0) with no Edit-page keyframes → DRT.
- ✗ **OTIO export** → multicams flattened to stacks of every angle (Multicam).

## App lifecycle

- Restart from an agent: ✗ **`resolve.Quit()` with a project open** → True, nothing happens. Working path: `pm.SaveProject()`, `pm.CloseProject(project)` (a big project times out the call at ~40 s but the close completes; wait until the window title shows the empty project), post `WM_CLOSE` to the Resolve main window (Windows), then `launch_resolve`. ✗ **`launch_resolve` while the old process is still exiting** → answers "running" → check the process start time and call it again; wait until the window responds. The relaunch reopens the last project.
- ✗ **After a relaunch** → `project.GetCurrentTimeline()` is None → code that restores "the current timeline" must handle None.
- ✗ **Resolve stuck on its splash screen** ("LOADING PROJECTS"; the debug log under Resolve's per-user `Support/logs` ends with Fusion init "not completed" … "Entering sleep mode") → process alive and idle, MCP says "not running", `launch_resolve` → `Resolve did not respond within 60s`, an external `scriptapp("Resolve")` → None → with no project open nothing is lost: kill that process by its PID and `launch_resolve` (~35 s).
- ✗ **Back-to-back heavy batches on a 2,600-clip project** (two 60 s timeouts in a row, then a disk scan) → Resolve stopped responding and Windows closed it (Application Hang event 1002); the autosave was 24 s older than the kill → keep batches small, `SaveProject()` between them, check what saved before continuing.
- Switching projects: `pm.LoadProject(name)` … `pm.CloseProject(p)` → reload the first one took 14 s; only when nobody else is working in Resolve.

## Adding to this log

A new failure goes under its area as `✗ **tried** → what happened → what to do instead`, with numbers and the exact error text. Keep it generic: no project names, people, drives or machine names.
