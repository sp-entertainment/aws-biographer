"""Build the Lambda deployment package.

Cross-compiles Linux wheels from Windows with pip's --platform flag, so no
Docker is needed to produce a working artifact. boto3 is excluded because the
Lambda runtime already ships it, and bundling a second copy only makes the
package larger and the runtime version ambiguous.

Run:  python scripts/build_lambda.py
"""

import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "lambda"
ARTIFACT = ROOT / "build" / "biographer.zip"

# Must match the Lambda runtime in infra/app.py or the wheels will not load.
PY_VERSION = "3.13"


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(BUILD),
         "--platform", "manylinux2014_x86_64", "--implementation", "cp",
         "--python-version", PY_VERSION, "--only-binary=:all:",
         "psycopg[binary]>=3.2", "psycopg_pool"],
        check=True,
    )

    shutil.copytree(ROOT / "src" / "biographer", BUILD / "biographer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "web", BUILD / "web")
    shutil.copytree(ROOT / "migrations", BUILD / "migrations")

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARTIFACT, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in BUILD.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(BUILD))

    size_mb = ARTIFACT.stat().st_size / 1_048_576
    print(f"{ARTIFACT}  {size_mb:.1f} MB")
    if size_mb > 50:
        print("WARNING: over the 50 MB direct-upload limit; deploy via S3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
