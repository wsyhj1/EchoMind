"""Local administrator creates accounts; passwords never appear in argv/logs."""
import argparse
import asyncio
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))

async def run(args, password):
    import asyncpg
    from dotenv import load_dotenv
    from auth import AuthStore
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'],min_size=1,max_size=2)
    try:
        store = AuthStore(pool)
        await store.initialize()
        user_id = await store.create_account(args.username,password,args.existing_user_id)
        print('Account created for user',user_id)
    finally:
        await pool.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create-user'])
    parser.add_argument('--username',required=True)
    parser.add_argument('--existing-user-id',type=int)
    parser.add_argument('--password-stdin',action='store_true')
    args=parser.parse_args()
    if args.existing_user_id is not None and args.existing_user_id<1:
        parser.error('existing-user-id must be positive')
    password=sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('New password (12+ characters): ')
    asyncio.run(run(args,password))

if __name__=='__main__':
    main()
