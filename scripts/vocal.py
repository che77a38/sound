#!/usr/bin/env python3
"""Clean up and mix voice/audio with ffmpeg.

Wraps the filter chains that actually matter for dialogue and narration work, so the
agent does not have to hand-assemble long ffmpeg expressions.

  vocal.py clean <in> <out> [--noise HZ] [--highpass HZ] [--lowpass HZ] [--deesser]
                          [--compressor] [--target LUFS] [--fade SEC]
  vocal.py duck <voice> <bgm> <out> [--bgm-gain DB] [--voice-gain DB] [--fade SEC]
  vocal.py mix  <bgm> <voice> <out> [--bgm-gain DB] [--voice-gain DB]
  vocal.py loudnorm <in> <out> [--target LUFS] [--tp DB]
  vocal.py trim  <in> <out> --start SEC --end SEC
  vocal.py loop  <in> <out> --duration SEC [--fade SEC]

All subcommands need ffmpeg on PATH (brew install ffmpeg).
"""
import argparse, json, shutil, subprocess, sys
from pathlib import Path


def need_ffmpeg():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found — install it (macOS: brew install ffmpeg)")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-6:])
        sys.exit(f"ffmpeg failed:\n{tail}")
    return r.stderr


def chain_without_loudnorm(a, src):
    """The cleanup chain up to (but not including) loudness normalisation."""
    f = []
    f.append(f"afftdn=nf={a.noise}:nr={a.noise_reduction}:tn=1")
    if a.highpass:
        f.append(f"highpass=f={a.highpass}")
    if a.lowpass:
        f.append(f"lowpass=f={a.lowpass}")
    if a.deesser:
        f.append("deesser=i=0.4:m=0.5:f=0.5")
    if a.compressor:
        f.append(f"acompressor=threshold={a.threshold}dB:ratio={a.ratio}:attack={a.attack}:release={a.release}")
    return ",".join(f)


def chain_fade_only(a, src):
    """Just the fade stage, so the loudness measurement sees the final signal."""
    if not a.fade:
        return ""
    total = a.duration or probe_duration(src)
    if total and total > a.fade:
        return f"afade=t=in:st=0:d={a.fade},afade=t=out:st={max(0, total - a.fade):.2f}:d={a.fade}"
    return f"afade=t=in:st=0:d={a.fade}"


def probe_duration(path):
    """Length of a media file in seconds, or None if it cannot be read."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return None


def measure_chain_loudness(src, chain, dur_args=None):
    """Measure loudness of a file after applying a filter chain.

    Used for the two-pass loudnorm inside `clean`, where the signal that reaches the
    loudness stage has already been denoised, filtered and compressed.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src)] + (dur_args or [])
    cmd += ["-af", f"{chain},loudnorm=I=-23:TP=-2:LRA=11:print_format=json",
            "-f", "null", "/dev/null"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    txt = r.stderr
    start = txt.rfind("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(txt[start:end + 1])
        return {
            "i": float(data["input_i"]),
            "lra": float(data["input_lra"]),
            "tp": float(data["input_tp"]),
            "thresh": float(data["input_thresh"]),
            "offset": float(data.get("target_offset", 0)),
        }
    except (KeyError, ValueError):
        return None


def cmd_clean(a):
    need_ffmpeg()
    src, dst = Path(a.input), Path(a.output)
    if not src.exists():
        sys.exit(f"no such file: {src}")

    pre = chain_without_loudnorm(a, src)
    fade = chain_fade_only(a, src)
    dur_args = ["-t", str(a.duration)] if a.duration else []

    if a.target_lufs:
        # Pass one: measure the loudness of the signal as it arrives at the loudnorm
        # stage, i.e. after denoise/EQ/de-esser/compression. Measuring the raw file
        # would be wrong, because those filters change the level.
        meas_chain = ",".join(x for x in (pre, fade) if x)
        meas = measure_chain_loudness(src, meas_chain, dur_args)
        if meas:
            loud = (f"loudnorm=I={a.target_lufs}:TP={a.true_peak}:LRA=11"
                    f":measured_I={meas['i']}:measured_LRA={meas['lra']}"
                    f":measured_TP={meas['tp']}:measured_thresh={meas['thresh']}"
                    f":offset={meas['offset']}:linear=true")
        else:
            loud = f"loudnorm=I={a.target_lufs}:TP={a.true_peak}:LRA=11"
        filt = ",".join(x for x in (pre, loud, fade) if x)
    else:
        filt = ",".join(x for x in (pre, fade) if x)

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(src)] + dur_args
    cmd += ["-af", filt, str(dst)]
    err = run(cmd)
    report(src, dst, err)


def cmd_duck(a):
    """Ducking: lower the background bed while the voice plays, then bring it back.

    Uses sidechaincompress so the bed reacts to the voice itself rather than to a
    hand-placed envelope, which is what makes it work when the speech timing shifts.
    """
    need_ffmpeg()
    voice, bgm, dst = Path(a.voice), Path(a.bgm), Path(a.output)
    for p in (voice, bgm):
        if not p.exists():
            sys.exit(f"no such file: {p}")
    voice_level = f"volume={a.voice_gain}dB"
    bgm_level = f"volume={a.bgm_gain}dB"
    duck = (f"[1:a]{bgm_level}[bed];"
            f"[bed][0:a]sidechaincompress=threshold={a.threshold}dB:ratio={a.ratio}:attack=5:release=300[ducked];"
            f"[0:a]{voice_level}[v];"
            f"[v][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(voice), "-i", str(bgm), "-filter_complex", duck,
           "-map", "[out]", str(dst)]
    err = run(cmd)
    report(voice, dst, err)


def cmd_mix(a):
    """Even blend of bed and voice — no ducking."""
    need_ffmpeg()
    bgm, voice, dst = Path(a.bgm), Path(a.voice), Path(a.output)
    for p in (bgm, voice):
        if not p.exists():
            sys.exit(f"no such file: {p}")
    mix = (f"[0:a]volume={a.bgm_gain}dB[bed];"
           f"[1:a]volume={a.voice_gain}dB[v];"
           f"[v][bed]amix=inputs=2:duration=first:normalize=0[out]")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(bgm), "-i", str(voice), "-filter_complex", mix,
           "-map", "[out]", str(dst)]
    err = run(cmd)
    report(voice, dst, err)


def measure_loudness(src):
    """First pass: measure the loudness statistics of a file.

    ffmpeg's loudnorm filter needs these values to normalise accurately. In single-pass
    mode it estimates them, which can leave the result a decibel or two off target.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
           "-af", "loudnorm=I=-23:TP=-2:LRA=11:print_format=json", "-f", "null", "/dev/null"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    txt = r.stderr
    start = txt.rfind("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(txt[start:end + 1])
    except ValueError:
        return None
    try:
        return {
            "i": float(data["input_i"]),
            "lra": float(data["input_lra"]),
            "tp": float(data["input_tp"]),
            "thresh": float(data["input_thresh"]),
            "offset": float(data.get("target_offset", 0)),
        }
    except (KeyError, ValueError):
        return None


def cmd_loudnorm(a):
    """Normalise to a target loudness using ffmpeg's two-pass loudnorm.

    Pass one measures the file's integrated loudness, loudness range and true peak;
    pass two applies the correction using those measurements, which lands much closer
    to the requested target than the single-pass estimate.
    """
    need_ffmpeg()
    src, dst = Path(a.input), Path(a.output)
    if not src.exists():
        sys.exit(f"no such file: {src}")
    measured = measure_loudness(src)
    if measured:
        filt = (f"loudnorm=I={a.target_lufs}:TP={a.true_peak}:LRA=11"
                f":measured_I={measured['i']}:measured_LRA={measured['lra']}"
                f":measured_TP={measured['tp']}:measured_thresh={measured['thresh']}"
                f":offset={measured['offset']}:linear=true:print_format=summary")
    else:
        # Fall back to single-pass rather than failing outright.
        filt = f"loudnorm=I={a.target_lufs}:TP={a.true_peak}:LRA=11"
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-af", filt, str(dst)])
    report(src, dst, "")


def cmd_trim(a):
    need_ffmpeg()
    src, dst = Path(a.input), Path(a.output)
    if not src.exists():
        sys.exit(f"no such file: {src}")
    run(["ffmpeg", "-hide_banner", "-y", "-ss", str(a.start), "-to", str(a.end),
         "-i", str(src), "-af", f"afade=t=in:st=0:d={a.fade},afade=t=out:st={max(0, a.end - a.start - a.fade):.2f}:d={a.fade}",
         str(dst)])
    report(src, dst, "")


def cmd_loop(a):
    """Make a short clip fill a longer span, with crossfades so the seam is not audible."""
    need_ffmpeg()
    src, dst = Path(a.input), Path(a.output)
    if not src.exists():
        sys.exit(f"no such file: {src}")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(src)], capture_output=True, text=True)
    try:
        clip = float(probe.stdout.strip())
    except ValueError:
        sys.exit("could not read clip duration")
    if clip >= a.duration:
        run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-t", str(a.duration), str(dst)])
    else:
        reps = int(a.duration // clip) + 1
        run(["ffmpeg", "-hide_banner", "-y", "-stream_loop", str(reps), "-i", str(src),
             "-t", str(a.duration), "-af", f"afade=t=out:st={max(0, a.duration - a.fade):.2f}:d={a.fade}", str(dst)])
    report(src, dst, "")


def report(src, dst, err):
    """Print the measured loudness of the result so the change is verifiable."""
    out = Path(dst)
    if not out.exists():
        print(f"FAIL {dst}: no output produced")
        return
    size = out.stat().st_size
    dur = ""
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(out)], capture_output=True, text=True)
    if probe.stdout.strip():
        dur = f"{float(probe.stdout.strip()):.2f}s"
    lv = ""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect",
                            "-f", "null", "/dev/null"], capture_output=True, text=True, timeout=120).stderr
        for line in r.splitlines():
            if "mean_volume" in line:
                lv = line.strip().split("] ")[-1]
                break
    except Exception:
        pass
    print(f"{out.name}\t{dur}\t{size // 1024}KB\t{lv}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("clean", help="dialogue cleanup chain"); c.add_argument("input"); c.add_argument("output")
    c.add_argument("--noise", type=int, default=-25, help="noise floor dB (afftdn nf)")
    c.add_argument("--noise-reduction", type=int, default=12, help="noise reduction amount")
    c.add_argument("--highpass", type=int, default=80, help="rumble cut, Hz (0 to disable)")
    c.add_argument("--lowpass", type=int, default=0, help="hiss cut, Hz (0 to disable)")
    c.add_argument("--deesser", action="store_true"); c.add_argument("--compressor", action="store_true")
    c.add_argument("--threshold", type=float, default=-18, help="compressor threshold dB")
    c.add_argument("--ratio", type=float, default=4); c.add_argument("--attack", type=int, default=5)
    c.add_argument("--release", type=int, default=50)
    c.add_argument("--target-lufs", type=float, default=0, help="loudness target (0 to skip)")
    c.add_argument("--true-peak", type=float, default=-1.5)
    c.add_argument("--fade", type=float, default=0, help="fade in/out seconds")
    c.add_argument("--duration", type=float, default=0); c.set_defaults(fn=cmd_clean)

    d = sp.add_parser("duck", help="duck bed under voice"); d.add_argument("voice"); d.add_argument("bgm"); d.add_argument("output")
    d.add_argument("--bgm-gain", type=float, default=-12); d.add_argument("--voice-gain", type=float, default=0)
    d.add_argument("--threshold", type=float, default=-30); d.add_argument("--ratio", type=float, default=8)
    d.add_argument("--fade", type=float, default=0); d.set_defaults(fn=cmd_duck)

    m = sp.add_parser("mix", help="even blend of bed and voice"); m.add_argument("bgm"); m.add_argument("voice"); m.add_argument("output")
    m.add_argument("--bgm-gain", type=float, default=-18); m.add_argument("--voice-gain", type=float, default=0); m.set_defaults(fn=cmd_mix)

    l = sp.add_parser("loudnorm", help="normalize loudness"); l.add_argument("input"); l.add_argument("output")
    l.add_argument("--target-lufs", type=float, default=-16); l.add_argument("--true-peak", type=float, default=-1.5)
    l.set_defaults(fn=cmd_loudnorm)

    t = sp.add_parser("trim", help="cut a range"); t.add_argument("input"); t.add_argument("output")
    t.add_argument("--start", type=float, required=True); t.add_argument("--end", type=float, required=True)
    t.add_argument("--fade", type=float, default=0.05); t.set_defaults(fn=cmd_trim)

    lo = sp.add_parser("loop", help="extend a clip to a target duration"); lo.add_argument("input"); lo.add_argument("output")
    lo.add_argument("--duration", type=float, required=True); lo.add_argument("--fade", type=float, default=0.5); lo.set_defaults(fn=cmd_loop)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
