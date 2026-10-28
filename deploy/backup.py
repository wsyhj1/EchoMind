"""Cold backups of all three data stores; restore drills cannot overwrite live data."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
COMPONENTS=('postgres','redis','milvus')


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            result.update(chunk)
    return result.hexdigest()


def pack_components(runtime,destination):
    runtime,destination=Path(runtime).resolve(),Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Refusing to replace an existing backup')
    for name in COMPONENTS:
        source=runtime/name
        if not source.is_dir() or source.is_symlink() or source.resolve().parent!=runtime:
            raise ValueError('Missing or redirected component: '+name)
    destination.mkdir(parents=True)
    archive=destination/'databases.tar.gz'
    with tarfile.open(archive,'w:gz',dereference=False) as bundle:
        for name in COMPONENTS:
            for path in sorted((runtime/name).rglob('*')):
                if path.is_symlink():
                    raise ValueError('Symlink in data directory; backup requires explicit handling')
                if path.is_file():
                    bundle.add(path,arcname=str(path.relative_to(runtime)),recursive=False)
    manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'components':list(COMPONENTS),
              'consistency':'all_database_containers_stopped','archive':'databases.tar.gz','sha256':digest(archive),
              'environment_file_included':False,'contains_private_database_data':True}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def verify_backup(directory):
    directory=Path(directory).resolve()
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=1 or manifest.get('archive')!='databases.tar.gz' or manifest.get('components')!=list(COMPONENTS):
        raise ValueError('Unsupported backup manifest')
    archive=directory/'databases.tar.gz'
    if digest(archive)!=manifest['sha256']:
        raise ValueError('Backup digest mismatch')
    total=0
    with tarfile.open(archive,'r:gz') as bundle:
        for member in bundle:
            path=Path(member.name)
            if '\\' in member.name or path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in COMPONENTS:
                raise ValueError('Unsafe backup member')
            if not member.isfile() or member.size<0:
                raise ValueError('Only regular data files may be restored')
            total+=member.size
            if total>20*1024**3:
                raise ValueError('Restore size exceeds 20 GiB review limit')
    return {**manifest,'uncompressed_bytes':total}


def restore_drill(directory,target,allowed_root):
    manifest=verify_backup(directory)
    allowed_root=Path(allowed_root).resolve()
    target=Path(target).resolve()
    if not target.is_relative_to(allowed_root) or target==allowed_root:
        raise ValueError('Restore target must be a new directory under restore-drills')
    if target.exists():
        raise FileExistsError('Refusing to overwrite any existing restore directory')
    target.mkdir(parents=True)
    with tarfile.open(Path(directory)/manifest['archive'],'r:gz') as bundle:
        for member in bundle:
            destination=target/member.name
            destination.parent.mkdir(parents=True,exist_ok=True)
            with bundle.extractfile(member) as source,destination.open('xb') as output:
                shutil.copyfileobj(source,output)
    return target


def apps_stopped():
    for port in (8000,8501):
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError('Stop project backend/frontend before a consistent cold backup')


def compose(*args):
    subprocess.run(['docker','compose',*args],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','verify','restore-drill'])
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--target',type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--acknowledge-downtime',action='store_true')
    args=parser.parse_args()
    directory=args.directory or ROOT/'.runtime/backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if args.action=='backup':
        if not directory.resolve().is_relative_to((ROOT/'.runtime/backups').resolve()):
            parser.error('Backups must stay inside project .runtime/backups')
        if not args.execute:
            print('Dry run: stop apps; stop three DB containers; archive data; restart DB containers. No .env copied.')
            return
        if not args.acknowledge_downtime:
            parser.error('--acknowledge-downtime is required')
        apps_stopped()
        try:
            compose('stop',*COMPONENTS)
            pack_components(ROOT/'.runtime',directory)
        finally:
            compose('up','-d','--pull','never',*COMPONENTS)
        print('Backup verified:',verify_backup(directory)['sha256'])
    elif args.action=='verify':
        if not args.directory:
            parser.error('--directory is required')
        print(json.dumps(verify_backup(directory),indent=2))
    else:
        if not args.directory or not args.target:
            parser.error('--directory and --target are required')
        if not args.execute:
            print('Dry run: verify and extract only into a NEW .runtime/restore-drills directory; live stores remain untouched.')
            return
        print('Restored drill:',restore_drill(directory,args.target,ROOT/'.runtime/restore-drills'))


if __name__=='__main__':
    main()
