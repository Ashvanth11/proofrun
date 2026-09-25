"""Restore the latest weekly DB artifact; fail closed if history has expired."""
import argparse
import io
import json
import subprocess
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    raw = subprocess.check_output(["gh", "api", f"repos/{args.repo}/actions/artifacts?name=proofrun-monitor-state&per_page=100"])
    artifacts = [a for a in json.loads(raw)["artifacts"] if not a["expired"]]
    out = Path("weekly-state")
    out.mkdir(exist_ok=True)
    if not artifacts:
        if args.bootstrap:
            return
        raise SystemExit("No durable monitoring state found. Review history, then explicitly bootstrap a manual run. No paid call was made.")
    latest = max(artifacts, key=lambda a: a["created_at"])
    data = subprocess.check_output(["gh", "api", f"repos/{args.repo}/actions/artifacts/{latest['id']}/zip"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        # Only the two expected files can be restored; never extract arbitrary paths.
        for name in ("monitor.db", "last-run.json"):
            if name in archive.namelist():
                (out / name).write_bytes(archive.read(name))
    if not (out / "monitor.db").exists():
        raise SystemExit("Saved state did not contain monitor.db; refusing a paid restart.")


if __name__ == "__main__":
    main()
