"""Restore the latest weekly DB artifact; fail closed if history has expired."""
import argparse
import io
import json
import subprocess
import zipfile
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_monitor.publication import STATE_NAME, load_state


def restore_publication_state(archive, out: Path) -> None:
    """Restore only the manifest and explicitly referenced automatic snapshots."""
    name = f'{STATE_NAME}/findings.json'
    if name not in archive.namelist():
        return  # Backward-compatible with state saved before automatic review.
    info = archive.getinfo(name)
    if info.file_size > 10_000_000 or archive.namelist().count(name) != 1:
        raise ValueError('invalid_publication_manifest_archive')
    raw = archive.read(name)
    state = json.loads(raw)
    if not isinstance(state.get('findings'), list) or len(state['findings']) > 200:
        raise ValueError('invalid_publication_manifest')
    root = out / STATE_NAME
    root.mkdir(exist_ok=True)
    for finding in state['findings']:
        source = finding.get('source', '')
        if not re.fullmatch(r'auto-[a-f0-9]{64}\.json', source):
            raise ValueError('unsafe_publication_source')
        member = f'{STATE_NAME}/{source}'
        if archive.namelist().count(member) != 1 or archive.getinfo(member).file_size > 200_000:
            raise ValueError('invalid_publication_source_archive')
        (root / source).write_bytes(archive.read(member))
    (root / 'findings.json').write_bytes(raw)
    load_state(root)  # Validate evidence hashes and approvals before any paid work.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    artifacts = []
    for name in ('proofrun-monitor-state', 'proofrun-monitor-state-checkpoint'):
        raw = subprocess.check_output(["gh", "api", f"repos/{args.repo}/actions/artifacts?name={name}&per_page=100"])
        artifacts.extend(a for a in json.loads(raw)["artifacts"] if not a["expired"])
    out = Path("weekly-state")
    out.mkdir(exist_ok=True)
    if not artifacts:
        if args.bootstrap:
            return
        raise SystemExit("No durable monitoring state found. Review history, then explicitly bootstrap a manual run. No paid call was made.")
    latest = max(artifacts, key=lambda a: (a["created_at"], a["id"]))
    data = subprocess.check_output(["gh", "api", f"repos/{args.repo}/actions/artifacts/{latest['id']}/zip"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        # Fixed core files and allowlisted publication snapshots only; never
        # extract arbitrary paths, private diagnostics or one-use review tickets.
        for name in ("monitor.db", "last-run.json"):
            if name in archive.namelist():
                (out / name).write_bytes(archive.read(name))
        restore_publication_state(archive, out)
    if not (out / "monitor.db").exists():
        raise SystemExit("Saved state did not contain monitor.db; refusing a paid restart.")


if __name__ == "__main__":
    main()
