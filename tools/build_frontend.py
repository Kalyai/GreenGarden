#!/usr/bin/env python3
"""Build committed CSS/JS bundles; Node/npm required only for development.

Run after editing css/style.css, css/fonts.css, css/seo.css or public scripts.
Docker serves the committed outputs and has no Node dependency.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESBUILD_VERSION = "0.28.2"


def main():
    for source in ("css/style.css", "css/seo.css", "js/main.js", "js/product.js"):
        path = Path(source)
        output = path.with_name(path.stem + ".min" + path.suffix)
        command = ["npx", "--yes", f"esbuild@{ESBUILD_VERSION}", source,
                   "--minify", "--target=es2020", f"--outfile={output}"]
        if path.suffix == ".css":
            command += ["--bundle", "--external:/fonts/*"]
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
