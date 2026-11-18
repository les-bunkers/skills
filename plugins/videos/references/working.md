# Working beside other agents, sessions and the human

Video work runs long, shares one machine, one NLE and one render queue, and often several agents at once. Every rule below replaced a collision that cost a render, a clip or the human's time.

## Before you start on something

- **Is someone already on it?** Read the record's recent log and notes, the project's handoff notes, and the modification times of the video's files over the last hour. A file that changed minutes ago and that you did not write = another session; message it or ask the human before writing. Say you are on it: `videos.py note <slug> "working: <agent or session> <what> since <time>"`, and a closing note when you stop.
- **Is a tool for this already being built?** Before writing a detector, checker or pipeline for a stage, check the handoffs and other sessions' recent files and ask. An agent once wrote its own breath detector while the human was building a better one.
- **Snapshot inputs that others may edit** (cut lists, source scripts) into your scratch folder before a long port or test, and work from the copy.

## Shared scripts and generators

- One owner edits a shared builder at a time; everyone else copies it or waits. Edits stay backward-compatible: re-render one older plan after the change and compare.
- Never edit a script while a job that uses it runs. Python imports a module once at launch (a batch started before the edit keeps the old code, one started after gets the new: a re-render batch nearly wrote into the wrong folder that way). Bash reads a script as it goes, so an edit mid-run executes garbage.
- A change that holds for the whole pipeline goes into every plugin and repository it touches. Sweep the companion plugins too.
- A procedure that worked becomes a script or a skill step, linked from the `/videos` entry point and the stage file, before you report done. The human should never have to ask "where's the skill for this?".

## Render lock, memory, background jobs

- One render lock for every GPU-heavy job: renders, encodes, local video or image generation. Release it between items (between takes for generation), and pause ~60 s between your own renders so a queued agent gets a turn. A step longer than the lock's heartbeat window keeps a background heartbeat going while its process lives.
- Free RAM before heavy stages: ≥ 10 GB when browser renders, speech models or local generation share the machine (4 GB was not enough); cap browser render workers (3). Under memory pressure, children fail to start (`0xC0000142`): retry once after 30 s. A memory-pressure reap stops the agent's own background tasks but can leave their children running.
- Launch background work through the agent's background-task feature, not `&` in a shell. Check it by process command line (Windows: `Get-CimInstance Win32_Process | ? CommandLine -match …`; Git Bash `ps` does not list native processes, which once led to a duplicate chain writing into the same log). Stop your own trees by PID with their children (`taskkill /T /F /PID`, `kill -- -<pgid>`); never by process name.
- Exit codes hide behind pipes (`| tail`, a trailing `echo`): check the output file, its size and frame count before using it.
- Watches: `tail -f` never exits; use `tail --pid=<pid> -f` or a loop that ends with the job. A sub-agent's own watch can expire silently: the coordinator checks results itself.
- The human needs the machine (a game, a call): stop chains, encoders and headless browsers by PID, release locks, leave light review servers up, and write where to resume.
- Long runs: give the expected finish as a clock time when you launch. Write the handoff early in an autonomous run: a usage limit can stop the session at any minute.

## Briefs for sub-agents

- Say what not to touch (shared scripts, other videos, the human's timelines) and what counts as done: an explicit READY or QC PASS line with the CI verdicts.
- Video CI applies to every file the sub-agent shows, uploads or hands back ([ci](ci.md)); a red file is never reported as passed.
- Ask for results as data files (JSON) or in the final message. Some harnesses refuse report `.md` files from sub-agents.
- Changes from the human that land while a sub-agent renders waste the render: queue them for after it, or stop it first.
- A sub-agent's "no word dropped" or "QC PASS" is a claim: check it on the output (a fork once reported QC PASS over a red framing check; another said no caption was dropped while 22 clips had lost words).
- Model and tool picks: check live model cards and licences before recommending, never from memory. Weights for a commercial deliverable need a licence that allows it (one cleanup model ran for days before its non-commercial licence was noticed).

## Reporting to the human

- Say where everything is: NLE project, bin, exact timeline names and the marker colours to jump by; file paths of reports; the local URL with the server's PID.
- Answer the human's direct question first, in the first lines.
- Costs: per unit (per video, per take, per run), "$0" when nothing paid ran. Mark prices that are content (a hook reading "37 $ an hour") as text, so nobody reads them as spend.
- A long research result → a visual page on localhost, not thousands of words.
- Your own leftovers (test media, a defect you made) → clean or fix them without asking.
- When the human names a deadline, drop side projects that do not serve it.
