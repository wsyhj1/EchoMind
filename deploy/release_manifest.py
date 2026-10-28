"""Fingerprint deliverable source without staging or committing existing user work."""
import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def build():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    allowed={'.py','.sql','.ps1','.json','.yml','.yaml','.md','.txt','.toml','.css','.html','.svg','.js','.ts','.tsx','.jsx'}
    files={}
    for name in sorted(set(names)):
        path=ROOT/name
        if (path.is_file() and (path.suffix in allowed or path.name=='.env.example')
                and (not path.name.startswith('.env') or path.name=='.env.example') and not path.is_symlink()):
            files[name]=sha256(path.read_bytes()).hexdigest()
    return {'created_at':datetime.now(timezone.utc).isoformat(),
        'git_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'dirty_worktree':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
        'source_sha256':files,'credentials_and_runtime_data_included':False,'commits_created':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'.runtime/releases/source_manifest.json')
    args=parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new output; refusing to replace a release record')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(build(),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Source manifest written; no Git staging or commit performed.')

if __name__=='__main__':
    main()
