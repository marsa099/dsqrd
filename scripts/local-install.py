#!/usr/bin/env python3
"""Per-user Linux installer/updater. No sudo, Nix, automatic push, or force reset."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def git(repo, *args):
    return run("git", "-C", str(repo), *args)


def merge_checkout(repo, fork_remote="origin", upstream_remote="upstream"):
    """Merge fetched tips off-tree; leave the real checkout untouched on conflict."""
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Checkout has uncommitted/untracked files; commit or stash them first")
    if git(repo, "symbolic-ref", "--short", "HEAD") != "main":
        raise RuntimeError("Automatic updates require the main branch")
    start = git(repo, "rev-parse", "HEAD")
    for remote in (fork_remote, upstream_remote):
        git(repo, "fetch", "--no-tags", remote,
            f"+refs/heads/main:refs/remotes/{remote}/main")
    fork = git(repo, "rev-parse", f"{fork_remote}/main")
    upstream = git(repo, "rev-parse", f"{upstream_remote}/main")
    with tempfile.TemporaryDirectory(prefix="dsqrd-merge-") as temporary:
        stage = Path(temporary) / "tree"
        git(repo, "worktree", "add", "--detach", str(stage), start)
        try:
            # Both merges happen in the disposable worktree. A conflicting
            # second merge must not leave a half-updated main branch behind.
            git(stage, "merge", "--no-edit", fork)
            git(stage, "merge", "--no-edit", upstream)
            revision = git(stage, "rev-parse", "HEAD")
            if git(repo, "rev-parse", "HEAD") != start or git(repo, "status", "--porcelain"):
                raise RuntimeError("Checkout changed during update; refusing to overwrite it")
            git(repo, "merge", "--ff-only", revision)
        finally:
            git(repo, "worktree", "remove", "--force", str(stage))
    return fork, upstream


def repo_slug(repo, remote):
    url = git(repo, "remote", "get-url", remote).removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):]
            if len(slug.split("/")) == 2:
                return slug
    raise RuntimeError(f"Expected a GitHub remote for {remote}, got {url}")


def stop_processes(app):
    """Match exact argv paths, never a broad pgrep substring."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            daemon = str(app / "dsqrd.py") in args
            ui = any(args[i:i + 2] == ["-p", str(app / "ui")] for i in range(len(args)))
            if daemon or ui:
                os.kill(int(entry.name), signal.SIGTERM)
                pids.append(int(entry.name))
        except (OSError, UnicodeError):
            continue
    for _ in range(50):
        alive = []
        for pid in pids:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if stat != "Z":
                    alive.append(pid)
            except OSError:
                pass
        if not alive:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Processes did not stop: {alive}; installation not replaced")


def atomic_text(path, content, executable=False):
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(content)
    temporary.chmod(0o755 if executable else 0o600)
    os.replace(temporary, path)


def install(repo, prefix, baselines=None, restart=True):
    repo = repo.resolve()
    share, bin_dir = prefix / "share", prefix / "bin"
    share.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    app = share / "dsqrd"
    python = share / "dsqrd-venv/bin/python3"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(repo / "requirements.txt")], check=True)
    run(str(python), "-c", "import socks, websocket, filetype, google.protobuf, jeepney")
    revision = git(repo, "rev-parse", "HEAD")
    fork_repo, upstream_repo = repo_slug(repo, "origin"), repo_slug(repo, "upstream")
    # A baseline is valid only if this installation actually contains it.
    # This also supports installing an unpublished local merge commit.
    if baselines is None:
        baselines = tuple(git(repo, "merge-base", revision, f"{remote}/main")
                          for remote in ("origin", "upstream"))
    for baseline in baselines:
        git(repo, "merge-base", "--is-ancestor", baseline, revision)
    config = {"checkout": str(repo), "fork_rev": baselines[0], "upstream_rev": baselines[1]}
    quote = shlex.quote
    env = {"DSQRD_REV": revision, "DSQRD_UPDATE_REPO": fork_repo,
           "DSQRD_UPSTREAM_REPO": upstream_repo, "DSQRD_FORK_REV": baselines[0],
           "DSQRD_UPSTREAM_REV": baselines[1]}
    daemon = "#!/bin/sh\n" + "".join(f"export {key}={quote(value)}\n" for key, value in env.items())
    daemon += f'exec {quote(str(python))} {quote(str(app / "dsqrd.py"))} "$@"\n'
    client = (repo / "scripts/dsqrd-client.sh").read_text()
    client = client.replace("@PREFIX@", quote(str(prefix)))
    # Prepare and validate before stopping the running application.
    with tempfile.TemporaryDirectory(prefix=".dsqrd-stage-", dir=share) as temporary:
        stage = Path(temporary) / "app"
        stage.mkdir()
        for name in ("dsqrd.py", "codemap.json", "media-viewer.sh", "ui", "dchat"):
            source = repo / name
            if source.is_dir():
                shutil.copytree(source, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, stage / name)
        run(str(python), "-m", "compileall", "-q", str(stage))
        subprocess.run(["bash", "-n"], input=client, text=True, check=True)
        stop_processes(app)
        # Runtime preferences/emoji data share this directory on default XDG
        # installs. Carry all non-payload files forward rather than deleting them.
        if app.exists():
            for source in app.iterdir():
                if not (stage / source.name).exists():
                    if source.is_dir():
                        shutil.copytree(source, stage / source.name)
                    else:
                        shutil.copy2(source, stage / source.name)
        backup = share / ".dsqrd-previous"
        if backup.exists():
            shutil.rmtree(backup)
        if app.exists():
            app.rename(backup)
        try:
            stage.rename(app)
        except OSError:
            if backup.exists():
                backup.rename(app)
            raise
    atomic_text(bin_dir / "dsqrd", daemon, True)
    atomic_text(bin_dir / "dsqrd-client", client, True)
    atomic_text(bin_dir / "dsqrd-manage", (repo / "scripts/local-install.py").read_text(), True)
    update = (repo / "scripts/dsqrd-update.sh").read_text().replace("@PREFIX@", quote(str(prefix)))
    atomic_text(bin_dir / "dsqrd-update", update, True)
    atomic_text(share / "dsqrd-install.json", json.dumps(config, indent=2) + "\n")
    print(f"Installed {revision[:12]} from {repo}", flush=True)
    if restart:
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dsqrd"
        state.mkdir(parents=True, exist_ok=True)
        with (state / "ui.log").open("a") as log:
            subprocess.Popen([str(bin_dir / "dsqrd-client")], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "update"))
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    prefix = args.prefix.expanduser().resolve()
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dsqrd"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another dsqrd installation/update is running")
        repo, baselines = args.source, None
        if args.action == "update":
            config = json.loads((prefix / "share/dsqrd-install.json").read_text())
            repo = Path(config["checkout"])
            baselines = merge_checkout(repo)
        install(repo, prefix, baselines, not args.no_restart)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"dsqrd: {error}", file=sys.stderr)
        sys.exit(1)
