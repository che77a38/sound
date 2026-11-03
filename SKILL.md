---
name: sound
description: Find, download, analyze and mix sound effects for video from Freesound, Wikimedia Commons and Mixkit, using download counts, ratings, descriptions, duration and licenses to select clips. Covers BGM beds, ffmpeg loudness and duration measurement, and dialogue cleanup/ducking.
allowed-tools: Bash(python3 ~/.claude/skills/sound/scripts/*), Bash(python3 .claude/skills/sound/scripts/*)
---
Use these sources for all sound-effect searches and downloads. You can't hear the clips, so lean on what people who did hear them left behind: download counts, ratings, and the uploader's description.

Resolve the script paths against this skill's own directory, which is `~/.claude/skills/sound/` for a global install and `.claude/skills/sound/` inside a project. Both work as-is with the commands below.

```
S=~/.claude/skills/sound/scripts/sfx.py     # global install
V=~/.claude/skills/sound/scripts/vocal.py
# in a project instead: S=.claude/skills/sound/scripts/sfx.py

python3 $S all "<query>" --license cc0      # fan out across every source, merged ranking
python3 $S freesound "<query>"             # ↓downloads ★rating(votes) license description
python3 $S commons "<query>"               # Wikimedia Commons: real duration + license per file
python3 $S mixkit "<query>"                # Mixkit Free License, no attribution needed
python3 $S music "<query>" --duration long # BGM beds rather than one-shot effects
python3 $S get <dir> fs:<id> wm:<id> mk:<id>
python3 $S analyze <file> --loudness --silence
```
`--urls` on a search adds preview links for the user to audition.

## Picking a source

- **Start with `all`** when you have no strong reason to pick one source. It queries all three concurrently and interleaves the results so no single source crowds out the others; a source that fails is skipped, not fatal.
- **`freesound`** is the only source with download counts and ratings, so it is the one to trust when the clip needs to be *proven* rather than merely plausible.
- **`commons`** (Wikimedia Commons) reports a real duration and a license for every file, and covers field recordings, bells, ambience and spoken-word material that Freesound does not. Use it when you need a specific length, a public-domain file, or something outside the stock-SFX vocabulary.
- **`mixkit`** is the fallback when Freesound has nothing that fits. Mixkit Free License allows commercial use without attribution. Only preview MP3s are fetchable without a session; the preview is a real audio file, usable for auditioning and cutting.
- **`music`** searches for BGM beds instead of one-shot effects, and filters by length.

## Reading the signals

- Prefer the most-downloaded clip that fits the cue, and quote the numbers when proposing it. A high rating count beats a high average: ★4.4(380) is stronger evidence than ★4.9(6).
- The Freesound description is the closest thing to listening: "built-in mic" or "made with my mouth" means lo-fi; a named recorder (Zoom, Sound Devices) and a clear source ("cork from a wine bottle") mean you know what you'll get. Reject clips whose description shows a different source than the cue, however popular.
- Licenses: **CC0 / PDM** are free to use with no credit. **BY** needs a credit (the `get` command prints it). **BY-SA** adds a share-alike obligation. **BY-NC** blocks commercial use — filter it out with `--license cc0|by` for anything client work. **Mixkit Free License** needs no attribution.
- Popularity says nothing about fit: a widely downloaded "thud" can be a 3 s cinematic rumble. Check the duration, and measure it before trusting a top hit. Use `--urls` when the user wants to audition candidates.

## Measuring a clip

Download counts say a clip is popular, not that it fits the scene. Measure it:

```
python3 $S analyze sfx/whoosh.mp3 --loudness --silence
```
Reports duration, mean/max volume, and where the silence sits. Use it to cut a clip to a scene, to check a transition lands on the right length, or to catch a file whose useful content is a 0.2 s hit inside 4 s of dead air.

## Cleaning up and mixing audio

For dialogue, narration or a voice-over sitting under a music bed:

```
python3 $V clean voice.wav clean.wav --deesser --compressor --target-lufs -16
python3 $V duck clean.wav bed.wav mixed.wav --bgm-gain -12
python3 $V mix bed.wav clean.wav mixed.wav --bgm-gain -18
python3 $V loudnorm in.wav out.wav --target-lufs -16
python3 $V trim in.wav out.wav --start 1.0 --end 4.0
python3 $V loop bed.wav long_bed.wav --duration 30
```

- `clean` runs a dialogue chain in mixer order: denoise, then EQ, then de-esser, then compression, then loudness normalisation. Denoise runs first because everything downstream would otherwise work on the noise floor.
- `duck` uses sidechain compression, so the bed reacts to the voice itself and keeps working when the speech timing shifts. Prefer it over `mix` for narration.
- `mix` is an even blend with no ducking — right for a bed that should sit quietly under a whole segment.
- **-16 LUFS** is the usual target for spoken content; -14 is for streaming music.
- Every command prints the measured loudness of its output, so the change is verifiable rather than assumed.

## Licensing and credits

Every clip carries its own license. `--license cc0` is the safe default for commercial work; `--license by` allows clips that need a credit. The default `any` can return BY-NC, BY-SA and other terms — check the license column before using anything in a commercial deliverable.

For anything not CC0, `get` prints the author for attribution. Keep the source URL and license with the deliverable.

## Requirements

- `python3` (standard library only — no pip install needed).
- `ffmpeg`/`ffprobe` for `analyze` and everything in `vocal.py`. Check with `which ffmpeg`; on macOS `brew install ffmpeg`.
- Network access to freesound.org, commons.wikimedia.org and mixkit.co.

## Troubleshooting

- Freesound times out: `python3 ~/.claude/skills/sound/scripts/clash_freesound.py --fix`; if that fails, read `references/local-setup.md` next to this file.
- Freesound caps ~80 KB/s per connection: put all keys in one `get`.
- Commons search matches the file *description*, so an unusual phrase returns nothing — retry with a plainer word. Its `filetype:audio` filter is what keeps it from returning images.
- Mixkit returns "?" for duration only if its waveform JSON is unreachable; the `analyze` command measures the downloaded file directly, so use that instead.
- Mixkit has no per-item download stats, so its ranking is the site's own relevance order, not a popularity signal.
