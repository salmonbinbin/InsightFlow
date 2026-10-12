"""Install pinned pgvector into pgserver's user-owned runtime (no database changes)."""

import hashlib
import platform
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pgserver

VERSION = "0.8.0"
SHA256 = "867a2c328d4928a5a9d6f052cd3bc78c7d60228a9b914ad32aa3db88e9de27b0"


def main():
    pg_config = Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_config"
    args = ["make", f"PG_CONFIG={pg_config}"]
    if platform.system() == "Darwin":
        sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
        args.append(f"PG_SYSROOT={sdk}")
    with tempfile.TemporaryDirectory(prefix="insightflow-pgvector-build-") as tmp:
        archive = Path(tmp) / "source.tar.gz"
        subprocess.run(
            [
                "curl",
                "--fail",
                "-L",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "-o",
                str(archive),
                f"https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v{VERSION}",
            ],
            check=True,
        )
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("pgvector archive checksum mismatch")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        source = Path(tmp) / f"pgvector-{VERSION}"
        subprocess.run(args, cwd=source, check=True)
        subprocess.run([*args, "install"], cwd=source, check=True)
    print("PGVECTOR_LOCAL_RUNTIME_INSTALLED (no database modified)")


if __name__ == "__main__":
    main()
