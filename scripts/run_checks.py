"""Separate application, frontend, core-evaluation and optional RAGAS checks."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--integration',action='store_true')
    parser.add_argument('--ragas',action='store_true')
    args=parser.parse_args()
    environment=os.environ.copy()
    environment.update(PYTHONUTF8='1',RAGAS_DO_NOT_TRACK='true')
    environment.setdefault('DASHSCOPE_API_KEY','unit-test-no-provider-access')
    environment.setdefault('AGENT_BASE_MODEL','unit-test-model')
    environment.setdefault('BASE_URL','http://127.0.0.1:9/v1')
    temporary=ROOT/'.runtime/tmp'
    temporary.mkdir(parents=True,exist_ok=True)
    environment.update(TEMP=str(temporary),TMP=str(temporary))
    if args.integration:
        from dotenv import dotenv_values
        config=dotenv_values(ROOT/'.env')
        environment['ECHOMIND_TEST_DATABASE_URL']=config['DATABASE_URL']
        environment['ECHOMIND_TEST_MILVUS_URL']=config.get('Milvus_url','http://localhost:19530')
    commands=[['-m','unittest','discover','-s','backend/tests','-v'],
              ['-m','unittest','discover','-s','frontend/tests','-v'],
              ['-m','unittest','discover','-s','deploy/tests','-v']]
    modules=['evaluation.tests.'+path.stem for path in sorted((ROOT/'evaluation/tests').glob('test_*.py'))
             if path.stem!='test_ragas_adapter']
    commands.append(['-m','unittest',*modules,'-v'])
    if args.ragas:
        commands.append(['-m','unittest','evaluation.tests.test_ragas_adapter','-v'])
    failures=0
    for command in commands:
        failures+=subprocess.run([sys.executable,*command],cwd=ROOT,env=environment).returncode!=0
    raise SystemExit(bool(failures))

if __name__=='__main__':
    main()
