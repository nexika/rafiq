# Rafiq's own offline voice

Without the internet, Rafiq speaks with Piper. Piper's stock voices belong to other speakers, and none of them is
Egyptian, so Egyptian Arabic is read by the Jordanian voice. This guide makes a Piper voice of Rafiq's own: one
speaker records sentences, a GPU fine-tunes an existing Piper voice on them once, and the result runs on any
laptop's CPU, offline (#250).

## 1. The speaker

Use one real person who has agreed in writing that their voice is Rafiq's, to be recorded, trained on and given
to users as a model. For `ar-EG`, choose an Egyptian speaker. The lists are written as an Egyptian says them.

## 2. Record

Use a quiet room with soft things around (a wardrobe, curtains, a sofa), a USB or headset microphone, and the
same distance from it every time. Then:

```bash
rafiq record-voice ar-EG        # or en
```

Each sentence shows on the screen. Press Enter, read it, and press Enter again. Then keep it (Enter), play it
(`p`) or record it again (`r`). `s` skips a sentence and `q` stops. The next session starts at the first
sentence not yet recorded. Rafiq records again on its own when a take clipped, was too quiet, or had no speech.

The recordings go to `~/.config/rafiq/data/voice/<tag>/`: `wavs/<id>.wav` (22050 Hz, mono, 16-bit) and
`metadata.csv` with `<id>.wav|text` lines, which is what Piper's trainer reads. The sentences come from
`rafiq/voice_sentences/<tag>.txt` and then Rafiq's own phrases. `--sentences FILE` reads another list, one
sentence per line.

Read the way Rafiq should sound: calm, friendly, at an even pace, not like a news anchor. Stop when your voice
gets tired. A take recorded tired sounds different from the others, and the voice learns that too.

The shipped list takes about 15 minutes to read. Piper's fine-tunes have worked from about an hour of audio. Add
your own sentences with `--sentences` until `rafiq record-voice` says about 60 minutes.

## 3. Train (once, on a rented GPU)

This follows [Piper's training guide](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md).
Copy the recordings to a Linux machine with an NVIDIA GPU (8 GB of VRAM is enough), then:

```bash
sudo apt-get install build-essential cmake ninja-build espeak-ng
git clone https://github.com/OHF-voice/piper1-gpl.git && cd piper1-gpl
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[train]' && ./build_monotonic_align.sh
```

Start from Piper's medium voice in the same language, from
[rhasspy/piper-checkpoints](https://huggingface.co/datasets/rhasspy/piper-checkpoints):
`ar/ar_JO/kareem/medium` for Arabic, `en/en_US/ryan/medium` for English. Download its `.ckpt` file, then:

```bash
python3 -m piper.train fit \
  --data.voice_name rafiq-masri \
  --data.csv_path ~/voice/ar-EG/metadata.csv \
  --data.audio_dir ~/voice/ar-EG/wavs \
  --data.espeak_voice ar \
  --model.sample_rate 22050 \
  --data.cache_dir ~/voice/cache \
  --data.config_path ~/voice/rafiq-masri.onnx.json \
  --data.batch_size 32 \
  --model.warmstart_ckpt kareem-medium.ckpt \
  --trainer.max_epochs 2000
```

`--data.espeak_voice` is `en-us` for English. `--model.warmstart_ckpt` copies the old voice's weights and starts
a fresh learning-rate schedule, so `--trainer.max_epochs 2000` is the whole run. That is Piper's recipe for under
five hours of audio. Piper's guide puts an epoch at about 9 seconds plus 4 seconds per hour of audio on an H100,
so an hour of recordings takes about 7 hours there. On an RTX 3090 it's about 25 seconds per hour of audio per
epoch, so about 14 hours. Then export:

```bash
python3 -m piper.train.export_onnx --checkpoint lightning_logs/version_0/checkpoints/<last>.ckpt \
  --output-file ~/voice/rafiq-masri.onnx
```

## 4. Use it

Put `rafiq-masri.onnx` and `rafiq-masri.onnx.json` together in `~/.config/rafiq/data/piper/`, and point the
language at them in `~/.config/rafiq/config.toml`:

```toml
[voice.piper_voices]
ar-EG = "~/.config/rafiq/data/piper/rafiq-masri.onnx"
```

Try it with `rafiq say "صباح الخير، أنا رفيق"`. Rafiq uses Piper whenever the cloud voices can't be reached,
so from then on it speaks offline in its own voice.

Piper reads Arabic with standard-Arabic sounds: it doesn't know that ق is often a hamza in Egyptian or that ج is
said "g". The recordings teach the model some of this, but listen to many sentences before giving the voice to
anyone.
