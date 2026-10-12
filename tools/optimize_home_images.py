#!/usr/bin/env python3
"""Rebuild committed image alternatives without modifying original photos.

Requires the official libwebp (cwebp/dwebp) and libavif (avifenc) tools.
The homepage keeps WebP fallbacks for browsers without AVIF support.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHOTOS = ("slide-2", "slide-3", "slide-4", "slide-5", "tuya-smaragd",
          "vishnya-morozovka", "yablonya-lobo", "klen-karnaval", "el-glauka",
          "tuya-krupnomer", "yablonya-kandil", "iva-pletenaya", "zhasmin-virginal")


def avif(source, scratch):
    png = scratch / (source.stem + ".png")
    subprocess.run(["dwebp", str(source), "-quiet", "-o", str(png)], check=True)
    subprocess.run(["avifenc", "-q", "60", "-y", "420", "-s", "6", "-j", "2",
                    str(png), str(source.with_suffix(".avif"))], check=True,
                   stdout=subprocess.DEVNULL)
    png.unlink()


def main():
    for executable in ("cwebp", "dwebp", "avifenc"):
        if not shutil.which(executable):
            raise SystemExit(f"Install libwebp/libavif: missing {executable}")
    output = ROOT / "img/performance"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="greengarden-images-") as temp:
        scratch = Path(temp)
        for name in PHOTOS:
            source = ROOT / "img" / (name + ".webp")
            for width in ((800, 1600) if name.startswith("slide-") else (480, 720, 900)):
                target = output / f"{name}-{width}.webp"
                subprocess.run(["cwebp", "-quiet", "-q", "80", "-m", "6",
                                "-resize", str(width), "0", str(source), "-o", str(target)], check=True)
                avif(target, scratch)
        subprocess.run(["cwebp", "-quiet", "-q", "90", "-resize", "84", "84",
                        str(ROOT / "img/logo.png"), "-o", str(output / "logo-84.webp")], check=True)
        full = ROOT / "img/optimized/abrikos-krasnoschekiy-1000.webp"
        mid = full.with_name("abrikos-krasnoschekiy-720.webp")
        subprocess.run(["cwebp", "-quiet", "-q", "80", "-m", "6", "-resize", "720", "0",
                        str(full), "-o", str(mid)], check=True)
        for source in (full, mid, ROOT / "img/optimized/golubika-blyukrop-1000.webp"):
            avif(source, scratch)


if __name__ == "__main__":
    main()
