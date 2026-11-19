---
name: resolve
description: Drive DaVinci Resolve Studio from an agent through Blackmagic's DaVinci Resolve MCP — the rules every call follows (timeouts that don't stop scripts, sandbox vs unsafe, one Resolve shared with the human and other sessions, save and restore state, check every return value) and the log of what failed before (imports, moves that copy, appends, subtitles, markers, multicam angles, audio edits, renders, frame grabs, LUTs, Fusion, restarts, hangs), plus the one media-pool layout every project follows: which bin a timeline, a camera file or an asset goes in, how timelines and versions are named (the format in the name: the long 9x16 cut first, the 16x9 and the shorts made from it), the snapshot made before a big edit, keywords and smart bins. Use before any Resolve MCP call (run_script, run_script_unsafe, get_image, get_thumbnails, describe_grade), when a Resolve script silently does nothing, returns False/None/[] or times out, when Resolve hangs, won't launch or the MCP says it is not running, when creating a bin, naming or versioning a timeline, setting up a 9:16 or 16:9 timeline, filing or tagging clips or tidying a project's media pool, or when running /videos:resolve.
---

The stages that touch Resolve (ingest, rough, tighten, audio, grade, formats, master, wide, clips) follow these rules. The full log of what works and what failed, by area: [resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md). Read the section for the area you are about to touch before writing the script.

## Before the first call

1. Tools deferred → load the ones you need in one ToolSearch call (`select:mcp__DaVinci_Resolve__run_script,…`).
2. `get_resolve_status`. Not running → `launch_resolve`. "Did not respond within 60s" while a Resolve process exists → App lifecycle in the log.
3. One read-only probe (`run_script`, `timeout` 30): project name, current timeline, page, `project.IsRenderingInProgress()`, number of render jobs. Another project is open than the one you expect → stop and ask; don't switch projects while the human works.

## Every script

```python
pm = resolve.GetProjectManager(); project = pm.GetCurrentProject()
assert project.GetName() == PROJECT, project.GetName()
if project.IsRenderingInProgress():
    result = {"skipped": "a render is running"}          # never raise SystemExit: its message is lost
else:
    mp = project.GetMediaPool()
    tl0, page0, folder0 = project.GetCurrentTimeline(), resolve.GetCurrentPage(), mp.GetCurrentFolder()
    tls = [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)]
    tl = next((t for t in tls if t.GetName() == NAME), None)   # by name, never "the current one"
    done = {}
    try:
        ...  # the change; record what happened in `done`
    finally:
        if tl0: project.SetCurrentTimeline(tl0)           # None right after a relaunch
        if page0 and resolve.GetCurrentPage() != page0: resolve.OpenPage(page0)
        mp.SetCurrentFolder(folder0)
    result = {"timeline": NAME, **done}
```

- Pass `timeout` on every call (default 10 s, max 60). A timeout does NOT stop the script: it keeps running inside Resolve and its changes land. After a timeout, wait, read the state back in a separate call, rerun only what is missing. Scripts are idempotent (skip what is done).
- Budget wall-clock time inside loops (`time.time() - t0 < 40`, counting 10 s per frame render), return `left`, call again until 0. Probe files (ffprobe) outside Resolve and pass the results in.
- `run_script` for reads (sandbox: no `open`, `os`, nor anything that imports them); `run_script_unsafe` when a script reads or writes files or runs a program. Keep logic in a file and `exec(open(path, encoding="utf-8").read())` it, with config globals set before the exec.
- Check every return value: False, None, `[]` and `""` mean nothing happened, usually without an error (unknown attributes and setting names return None or `""`). Count after the change: items placed, markers, cues, jobs.
- Never trust the stubs for setter values; set render keys one at a time and read each return.
- Small results: big dumps go to a JSON file, the result carries counts and the path.

## Sharing Resolve

One Resolve serves every agent session and the human: one project, current timeline, page, media-pool folder, render queue and frame renderer.

- One agent changes Resolve at a time; the others read. Frame renders too: three agents rendering frames at once wedged the renderer for half an hour.
- Nothing changes while a render runs (imports return `[]`, markers return False).
- Nothing changes while the human is active in Resolve either, throwaway test timelines included: a timeline a script creates or duplicates becomes what they see, and changes made beside theirs have come back undone a minute later without an error. The log tells (`Application state changed to Active` with no later `Inactive`, page changes or saves between your calls). After a change, read the state back in a later call before reporting it done.
- Never `DeleteAllRenderJobs`; delete only your own job ids after `Complete`. After a render, `OpenPage("edit")`: on Deliver, track toggles return True and change nothing.
- Add new tracks disabled (or enabled alone, on request) and leave the human's toggles as found. A big change starts with a snapshot of the timeline into the archive (below); delete only your own tests, and replace a build only after comparing the timeline with the last cut list (hand edits → ask).
- `DuplicateTimeline` and `ImportTimelineFromFile` make the new timeline current: restore the previous one.
- Files on a timeline are held open: re-render to a new path, then `ReplaceClip`.
- See Resolve's state through the API, `get_image` / `get_thumbnails`, or a capture of the Resolve window alone; never a desktop screenshot.

## Where things go, what they are called

One layout in every project; full rules and who files what: the log's section Organization. The helper is `${CLAUDE_PLUGIN_ROOT}/scripts/resolve_bins.py` (`import resolve_bins as B` after `sys.path.insert`, then `importlib.reload(B)`).

```
01 Timelines/WIP/Latest     working timelines = the video + its format: "Teleprompter demo 9x16", "Teleprompter demo 16x9"
                            shorts from the long vertical: "Teleprompter demo 9x16 S01 price objection"
01 Timelines/WIP/Archive    earlier versions: "Teleprompter demo 9x16 v02 2026-03-04 rough"
01 Timelines/Final          approved cuts: "… 9x16 v05 2026-03-09 approved"      01 Timelines/Scratch = tests
02 Footage/<YYYY-MM-DD place or subject>/<take>/   camera and audio files as shot; <shoot>/Multicam
03 Assets/Images · Videos/<Video>/<fmt> · Music · SFX · Voice/<Video> · Subtitles/<Video>
```

- Formats: the long `<Video> 9x16` (1080×1920) is cut first and approved; `<Video> 16x9` and the shorts are made from the approved one, never cut on their own. The format token always matches the timeline's resolution. Names: `B.tl_name(video, fmt, short, label)`, read back with `B.parse(name)`; an old working timeline without a token → `B.add_format(project, name)`.
- Before a big edit (rebuild, restructure, a pass over the whole timeline): `result = B.snapshot(resolve, project, "<Video> <fmt>", "<state>")`. A copy goes to the archive as `<Video> <fmt> vNN YYYY-MM-DD <state>`; the working timeline keeps its name and its place. Never put a version, date or stage in a working timeline's name, never delete or edit an archived one.
- Camera and audio clips keep their file name as clip name. The camera goes in the `Angle` metadata. Never rename a pool clip for good.
- New bins come from `B.path_bin(mp, "03 Assets/Videos/<Video>")`, never at the pool root. Moves go through `B.move(resolve, project, items, path)`: a plain `MoveClips` can copy instead of moving while returning True.
- Tag what you add: `B.tag(item, "<Video>", "<kind>")` (kinds in the log); a timeline also gets `B.timeline_words(name)` (video, format, `short`). Resolve shows one smart bin per keyword, so the video's name lists everything that belongs to it and `9x16` every vertical timeline.
- After a stage that added things: `result = B.audit(project)` → `ok` True.

## Verify

- After a build: counts against the plan, then `get_thumbnails source=timeline` (renders the current timeline; subtitles don't show). Subtitles: compare each cue's text with the SRT.
- After a render: `GetRenderJobStatus` = `Complete`, then ffprobe each file for a video stream, its frame count and its sound. The first job of a batch can come out without video: queue a 1-second throwaway job first.
- An effect is checked against the same frame without it, or against the source frame, never from one frame alone.

## When something fails

| Symptom | Log section |
|---|---|
| `Script timed out` / a call runs for minutes | Running scripts |
| Import returns `[]`, marker or toggle does nothing, wrong project or timeline | One Resolve, many callers; Media pool; Tracks |
| A move doubled the clips or timelines, a bin exists twice, a rename returned False | Media pool |
| Where does this go, what do I call it, how do I keep the old version | Organization |
| A 9x16 timeline, framing a vertical, the 16x9 or a short from the approved 9x16 | Organization § Formats; Timelines § Another format |
| A vertical render came out landscape | Rendering |
| Append places nothing, subtitles at the end, 1-frame-short items | Timelines; Subtitles |
| Render without video, wrong size or bitrate, WAV comes out `.mov` | Rendering |
| `Failed to render frame` | Frames and visual checks |
| ExportLUT False, empty node graph | Grade and LUTs |
| Fusion comp built but nothing renders, keyframes overshoot | Fusion, effects and templates |
| Not running, splash hang, Quit ignored, Resolve closed by Windows | App lifecycle |

A failure that isn't in the log: once solved (or given up), add it to its section in `references/resolve.md` as `✗ tried → what happened → what to do instead`, generic (no project names, people, drives or machine names).
