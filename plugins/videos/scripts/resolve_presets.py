"""Master render presets with a real bitrate. Runs inside DaVinci Resolve through the Resolve MCP (`run_script_unsafe`);
the XML patch functions also run from a shell (tests). Stdlib only.

Why: through the scripting API, `SetRenderSettings({"VideoQuality": <kbps>})` returns False on every NVIDIA encoder
(H.264, H.265, AV1) and the stock presets render at their own rate (~11 Mbps at 4K, below YouTube's 35-45 Mbps for
4K uploads). A preset's encoder settings live in its XML: `RecordQuality` (0 = "Restrict to", the bitrate mode) and
`encoder_command_param_map` (NVENC options: `rc` = rate control, `bitrate` in kbps). Stock presets cannot be exported,
so ensure() saves a copy of the stock preset with the codec set, exports it, patches those fields, deletes the copy and
imports the patched file under the master preset's name. Measured on a 4K talking-head timeline (10 s):
`rc` vbr-bitrate at 80000 -> 50.9 Mbps (VBR spends less on easy frames), cbr at 80000 -> 84.5 Mbps, H.264 vbr-bitrate
at 20000 -> 15.8 Mbps; the stock presets -> 11 Mbps whatever VideoQuality says.

    import sys, importlib; sys.path.insert(0, r"<plugin>/scripts")
    import resolve_presets as RP; importlib.reload(RP)
    result = RP.ensure(resolve, project, "uhd")         # {"preset": "videos master 4K HEVC 80M", "made": True, ...}
    project.LoadRenderPreset(result["preset"])

Masters (the human approved these, 2026-11):
  uhd       16:9 at 3840x2160 (landscape, long side >= 3840): H.265 NVIDIA Main10, VBR 80 Mbps, AAC 320k. YouTube's top
            recommendation is 45 Mbps; about twice that gives its re-encode more than it needs.
  hd        every other size (1080x1920 9x16, shorts, HD 16:9): H.264 NVIDIA High, VBR 20 Mbps, AAC 320k. A 9:16
            crop of 4K holds 1215x2160 real pixels and Reels / TikTok show 1080x1920 at most; 20 Mbps stays under the
            25 Mbps third-party guides list for the Instagram API, so one file serves every platform.
No NVIDIA encoder -> ensure() returns an error and render_finals.py falls back to the stock preset (and says so).
"""
import os
import re
import tempfile
import uuid

MASTERS = {
    "uhd": {"name": "videos master 4K HEVC 80M", "base": "YouTube - 2160p", "codec": "H265_NVIDIA",
             "subtype": "hvc1_nvenc", "profile": "Main10", "kbps": 80000, "rc": "vbr-bitrate"},
    "hd": {"name": "videos master H264 20M", "base": "YouTube - 1080p", "codec": "H264_NVIDIA",
                 "subtype": "avc1_nvenc", "profile": None, "kbps": 20000, "rc": "vbr-bitrate"},
}
PARAM_MAP = re.compile(r"(<DbKey>encoder_command_param_map</DbKey>\s*<DbVal>)([0-9a-f]*)(</DbVal>)")


def kind_for(width, height):
    """Which master a timeline of this size renders with."""
    return "uhd" if max(width, height) >= 3840 and width > height else "hd"


def decode_params(hx):
    """encoder_command_param_map: u32 pair count, then per string u32 byte length + UTF-16BE text, key/value pairs."""
    b = bytes.fromhex(hx)
    out, i = [], 4
    while i < len(b):
        n = int.from_bytes(b[i:i + 4], "big")
        out.append(b[i + 4:i + 4 + n].decode("utf-16-be"))
        i += 4 + n
    return dict(zip(out[0::2], out[1::2]))


def encode_params(d):
    b = len(d).to_bytes(4, "big")
    for k, v in d.items():
        for s in (k, v):
            u = s.encode("utf-16-be")
            b += len(u).to_bytes(4, "big") + u
    return b.hex()


def read(xml):
    """What a preset XML says about its video encoding."""
    m = PARAM_MAP.search(xml)
    params = decode_params(m.group(2)) if m and m.group(2) else {}
    sub = re.search(r"<RecordFormatSubType>([^<]*)<", xml)
    q = re.search(r"<RecordQuality>([^<]*)<", xml)
    rate = re.search(r"<DbKey>h264_datarate</DbKey>\s*<DbVal>([^<]*)<", xml)
    return {"subtype": sub.group(1) if sub else None, "record_quality": q.group(1) if q else None,
            "datarate": rate.group(1) if rate else None, "rc": params.get("rc"), "bitrate": params.get("bitrate")}


def patch(xml, kbps, rc):
    """Bitrate mode at `kbps` with NVENC rate control `rc`; a fresh DbId so the import never collides; no file prefix
    or target folder carried over from the copy."""
    m = PARAM_MAP.search(xml)
    if not m or not m.group(2):
        raise ValueError("no encoder_command_param_map: not an NVIDIA encoder preset")
    params = decode_params(m.group(2))
    params["rc"], params["bitrate"] = rc, str(int(kbps))
    xml = xml[:m.start(2)] + encode_params(params) + xml[m.end(2):]
    xml, n = re.subn(r"(<DbKey>h264_datarate</DbKey>\s*<DbVal>)[^<]*(<)", rf"\g<1>{int(kbps)}\2", xml)
    if not n:
        raise ValueError("no h264_datarate field")
    xml = re.sub(r"<RecordQuality>[^<]*</RecordQuality>", "<RecordQuality>0</RecordQuality>", xml)
    xml = re.sub(r'(<SyRecordInfo DbId=")[^"]+(")', rf"\g<1>{uuid.uuid4()}\2", xml)
    xml = re.sub(r"<RecordPrefix>[^<]*</RecordPrefix>", "<RecordPrefix/>", xml)
    return re.sub(r"<RecordTargetDir>[^<]*</RecordTargetDir>", "<RecordTargetDir/>", xml)


def _export(resolve, name, folder):
    """ExportRenderPreset takes a FOLDER and writes "<name>.xml" in it (a path ending in .xml becomes a folder)."""
    os.makedirs(folder, exist_ok=True)
    if not resolve.ExportRenderPreset(name, folder):
        return None
    p = os.path.join(folder, name + ".xml")
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else None


def ensure(resolve, project, kind, workdir=None, rebuild=False):
    """The master preset of `kind` ("uhd" / "hd") exists in this Resolve, with its bitrate read back from the
    preset itself. Returns {"preset", "made", "check"} or {"error", ...}. Loads other presets on the way: load the one
    you render with afterwards."""
    spec = MASTERS[kind]
    name = spec["name"]
    work = workdir or os.path.join(tempfile.gettempdir(), "resolve-presets")
    have = name in (project.GetRenderPresetList() or [])
    if have and not rebuild:
        return {"preset": name, "made": False}
    if have:
        project.DeleteRenderPreset(name)
    tmp = name + " tmp " + uuid.uuid4().hex[:6]
    if not project.LoadRenderPreset(spec["base"]):
        return {"error": f"stock preset {spec['base']!r} not found"}
    project.SetCurrentRenderFormatAndCodec("mp4", spec["codec"])  # its return is unreliable: the XML says what took
    if spec["profile"]:
        project.SetRenderSettings({"EncodingProfile": spec["profile"]})
    if not project.SaveAsNewRenderPreset(tmp):
        return {"error": "SaveAsNewRenderPreset failed", "name": tmp}
    try:
        xml = _export(resolve, tmp, os.path.join(work, "base"))
    finally:
        project.DeleteRenderPreset(tmp)
    if xml is None:
        return {"error": "ExportRenderPreset wrote nothing", "folder": work}
    got = read(xml)
    if got["subtype"] != spec["subtype"]:
        return {"error": f"codec {spec['codec']} did not take (preset says {got['subtype']!r}): no NVIDIA encoder?"}
    try:
        new = patch(xml, spec["kbps"], spec["rc"])
    except ValueError as e:
        return {"error": str(e)}
    path = os.path.join(work, name + ".xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    if not resolve.ImportRenderPreset(path) or name not in (project.GetRenderPresetList() or []):
        return {"error": "ImportRenderPreset failed", "file": path}
    back = _export(resolve, name, os.path.join(work, "check"))
    check = read(back) if back else {}
    ok = (check.get("record_quality") == "0" and check.get("rc") == spec["rc"]
          and check.get("bitrate") == str(spec["kbps"]) and check.get("subtype") == spec["subtype"])
    if not ok:
        project.DeleteRenderPreset(name)
        return {"error": "imported preset reads back different: deleted", "check": check}
    return {"preset": name, "made": True, "check": check}
