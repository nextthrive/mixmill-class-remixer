"""Build a tiny synthetic library for MixMill's smoke suite.

Usage: python tests/make_fixtures.py <output_dir>

Layout produced:
  COMBAT/COMBAT 2/COMBAT2.mp4        (dual audio + chapters) + music/ (3 songs)
  COMBAT/COMBAT 10/COMBAT10.mp4      (dual audio)            + music/ (3 songs)
  PUMP/PUMP 5/PUMP5.mp4              (single audio)          no music folder
"""
import shutil
import subprocess
import sys
from pathlib import Path

from reportlab.pdfgen import canvas

SONG_SECONDS = 8
SONGS = ["01 Warmup", "02 Power", "03 Cooldown"]


def run(*cmd):
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"command failed: {' '.join(map(str, cmd))}\n{proc.stderr[-2000:]}")


def make_choreography_notes(folder: Path, title: str):
    """Small, text-searchable stand-in for standard release choreography notes."""
    path = folder / f"{title.replace(' ', '')}ChoreographyNotes.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(566.929, 1014.8))
    pdf.setTitle(f"{title} Choreography Notes")
    pdf.setFont("Helvetica-Bold", 26)
    pdf.drawString(44, 930, f"{title} CHOREOGRAPHY NOTES")
    pdf.setFont("Helvetica", 12)
    for i, name in enumerate(SONGS, start=1):
        title = name.partition(" ")[2] or name
        pdf.drawString(44, 880 - i * 28, f"{i:02d}. {title.upper()}")
    pdf.showPage()
    for i, name in enumerate(SONGS, start=1):
        title = name.partition(" ")[2] or name
        for page_kind in ("CHOREOGRAPHY", "TECHNIQUE AND COACHING"):
            pdf.setFont("Helvetica-Bold", 24)
            pdf.drawString(44, 940, f"{i:02d}. {title.upper()}")
            pdf.setFont("Helvetica-Bold", 15)
            pdf.drawString(44, 900, page_kind)
            pdf.setFont("Helvetica", 11)
            pdf.drawString(44, 865, f"Synthetic smoke-test notes - 0:{SONG_SECONDS:02d}mins")
            pdf.showPage()
    for page_kind in ("EXPRESS CHOREOGRAPHY", "EXPRESS COACHING"):
        pdf.setFont("Helvetica-Bold", 24)
        pdf.drawString(44, 940, "03A. COOLDOWN EXPRESS")
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(44, 900, page_kind)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(44, 865, "Synthetic alternate notes - 0:05mins")
        pdf.showPage()
    for page_kind in ("BONUS CHOREOGRAPHY", "BONUS COACHING"):
        pdf.setFont("Helvetica-Bold", 24)
        pdf.drawString(44, 940, "B5. BONUS TRACK")
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(44, 900, page_kind)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(44, 865, "Synthetic bonus notes - 0:08mins")
        pdf.showPage()
    pdf.save()


def make_release(root: Path, program: str, title: str, stem: str,
                 seed_base: int, with_music: bool, with_chapters: bool,
                 dual_audio: bool = True):
    rel = root / program / title
    tmp = rel / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    songs = []
    for i, name in enumerate(SONGS):
        song = tmp / f"{name}.m4a"
        # pink noise with a unique seed per song: self-similar audio that
        # cross-correlates sharply at the true offset and nowhere else
        run("ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anoisesrc=color=pink:seed={seed_base + i}:duration={SONG_SECONDS}",
            "-c:a", "aac", "-b:a", "96k", song)
        songs.append(song)
    full = tmp / "full.wav"
    run("ffmpeg", "-y", "-i", songs[0], "-i", songs[1], "-i", songs[2],
        "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]", "-map", "[a]", full)
    video = rel / f"{stem}.mp4"
    total = SONG_SECONDS * len(SONGS)
    cmd = ["ffmpeg", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={total}",
           "-i", full]
    meta = tmp / "meta.txt"
    if with_chapters:
        lines = [";FFMETADATA1"]
        for i, name in enumerate(SONGS):
            lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                      f"START={i * SONG_SECONDS * 1000}",
                      f"END={(i + 1) * SONG_SECONDS * 1000}",
                      f"title={name}"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd += ["-i", meta, "-map_metadata", "2"]
    if dual_audio:
        cmd += ["-filter_complex", "[1:a]volume=0.4[quiet]",
                "-map", "0:v", "-map", "1:a", "-map", "[quiet]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "96k", "-shortest", video]
    run(*cmd)
    if with_music:
        music = rel / "music"
        music.mkdir(exist_ok=True)
        cover = tmp / "cover.jpg"
        run("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00e05f:s=120x120",
            "-frames:v", "1", cover)
        run("ffmpeg", "-y", "-i", songs[0], "-i", cover,
            "-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "mjpeg",
            "-disposition:v", "attached_pic", music / songs[0].name)
        for song in songs[1:]:
            shutil.copy2(song, music / song.name)
        make_choreography_notes(rel, title)
    shutil.rmtree(tmp)


def main(out: Path):
    if out.exists():
        shutil.rmtree(out)
    make_release(out, "COMBAT", "COMBAT 2", "COMBAT2", 100, True, True)
    make_release(out, "COMBAT", "COMBAT 10", "COMBAT10", 200, True, False)
    make_release(out, "PUMP", "PUMP 5", "PUMP5", 300, False, False, dual_audio=False)
    print(f"fixtures written to {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tests/make_fixtures.py <output_dir>")
    main(Path(sys.argv[1]).resolve())
