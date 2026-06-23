"""Deploy docs/ to the user GitHub Pages repo (kevinqy001.github.io).

The site is served at https://kevinqy001.github.io/ when content lives in that
repository's default branch root.

Usage:
    python scripts/build_github_pages.py
    python scripts/deploy_user_github_pages.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
USER_PAGES_REPO = "https://github.com/kevinqy001/kevinqy001.github.io.git"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    if not DOCS_DIR.exists() or not (DOCS_DIR / "index.html").exists():
        print("docs/ is missing. Run: python scripts/build_github_pages.py", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="gh-pages-deploy-"))
    try:
        clone_dir = work / "site"
        run(["git", "clone", USER_PAGES_REPO, str(clone_dir)])

        for child in clone_dir.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

        for child in DOCS_DIR.iterdir():
            dest = clone_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(child, dest)

        run(["git", "add", "-A"], cwd=clone_dir)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=clone_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        if not status.stdout.strip():
            print("No changes to deploy.")
            return 0

        run(
            [
                "git",
                "-c",
                "user.name=kevinqy001",
                "-c",
                "user.email=kevinqy001@users.noreply.github.com",
                "commit",
                "-m",
                "Deploy Qualitrol Quoting Automation webapp demo",
            ],
            cwd=clone_dir,
        )
        run(["git", "push", "origin", "HEAD"], cwd=clone_dir)
        print("Deployed to https://kevinqy001.github.io/")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Deploy failed: {exc}", file=sys.stderr)
        return exc.returncode or 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
