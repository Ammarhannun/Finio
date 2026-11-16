"""Encrypt and restore a synthetic Finio database in a disposable local container.

Never connects to Supabase. No host port or external container network is used.
The generated key is temporary; this is a drill, not a backup of customer data.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

from scripts.manual_backup import encrypt_stream, IMAGE

ROOT = Path(__file__).resolve().parents[1]
USER_A = '11111111-1111-1111-1111-111111111111'
USER_B = '22222222-2222-2222-2222-222222222222'


def run_drill(age_directory, output_directory, docker_config=None, docker_host=None):
    started = time.monotonic()
    output_directory = Path(output_directory).resolve()
    if output_directory == ROOT or ROOT in output_directory.parents:
        raise ValueError('Keep drill artifacts outside the source repository.')
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    age_directory = Path(age_directory).resolve()
    age, keygen = age_directory/'age', age_directory/'age-keygen'
    if not age.is_file() or not keygen.is_file():
        raise ValueError('Provide a directory containing the official age and age-keygen tools.')
    docker = ['docker']
    if docker_config:
        docker += ['--config', str(Path(docker_config).resolve())]
    if docker_host:
        docker += ['--host', docker_host]
    name = 'finio-recovery-' + uuid4().hex[:12]
    def command(args, data=None, timeout=60):
        process = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if process.returncode:
            # This drill contains synthetic data only. Keep diagnostics private;
            # never use this helper to run against a production connection.
            diagnostic = output_directory/'drill-diagnostic.log'
            diagnostic.write_bytes(process.stderr)
            diagnostic.chmod(0o600)
            raise RuntimeError('A recovery-drill operation failed. No live database was accessed.')
        return process.stdout
    def sql(database, query):
        return command(docker+['exec','-i',name,'psql','-X','-v','ON_ERROR_STOP=1','-U','finio','-d',database,'-At'], query.encode())
    try:
        command(docker+['run','-d','--rm','--name',name,'--network','none','--read-only','--memory=1g','--cpus=1',
            '--tmpfs','/var/lib/postgresql/data:rw,size=512m','--tmpfs','/var/run/postgresql:rw',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=128m','-e','POSTGRES_HOST_AUTH_METHOD=trust',
            '-e','POSTGRES_USER=finio','-e','POSTGRES_DB=source_synthetic',IMAGE])
        for attempt in range(30):
            # The image's temporary init server uses only a Unix socket. Check
            # TCP loopback so we wait for the final server after init/restart.
            ready = subprocess.run(docker+['exec',name,'pg_isready','-h','127.0.0.1','-U','finio','-d','source_synthetic'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError('The isolated database did not become ready.')
        setup = """
          create role anon; create role authenticated;
          create schema auth; create schema extensions;
          set search_path=public,extensions;
          create table auth.users(id uuid primary key,email text);
          create table auth.sessions(id uuid primary key,user_id uuid references auth.users(id),not_after timestamptz);
          create table auth.mfa_factors(id uuid primary key,user_id uuid references auth.users(id),status text,factor_type text);
          create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
          create function auth.jwt() returns jsonb language sql stable as $$select coalesce(nullif(current_setting('request.jwt.claims',true),'')::jsonb,'{}'::jsonb)$$;
          grant usage on schema auth,public,extensions to anon,authenticated;
          grant execute on function auth.uid(),auth.jwt() to anon,authenticated;
          alter default privileges in schema public grant all on tables to anon,authenticated;
          alter default privileges in schema public grant all on sequences to anon,authenticated;
        """
        files = [ROOT/'project-plan/supabase_schema.sql', *sorted((ROOT/'migrations').glob('*.sql'))]
        sql('source_synthetic', setup+'\n'+('\n'.join(path.read_text() for path in files)+'\n')*2)
        sql('source_synthetic', f"""
          insert into auth.users values('{USER_A}','a@example.invalid'),('{USER_B}','b@example.invalid');
          insert into public.users(id,email) select id,email from auth.users;
          insert into auth.sessions(id,user_id) select id,id from auth.users;
          insert into auth.mfa_factors values('{USER_A}','{USER_B}','verified','totp');
          insert into transactions(user_id,date,amount,merchant,category,flow,balance,position) values
          ('{USER_A}','2026-06-01',2000,'SYNTHETIC SALARY','Income','income',2000,0),
          ('{USER_A}','2026-06-02',-110,'SYNTHETIC GROCERIES','Groceries','expense',1890,1),
          ('{USER_A}','2026-06-03',10,'SYNTHETIC REFUND','Groceries','expense',1900,2),
          ('{USER_B}','2026-06-02',-20,'SYNTHETIC OTHER','Shopping','expense',500,0);
          insert into snapshots(user_id,month,summary_json) values('{USER_A}','2026-06-01','{{"metrics":{{"total_spent":100}}}}');
          insert into chat_history(user_id,role,message,chat_id) values('{USER_A}','user','Synthetic recovery fixture','drill');
        """)
        tables = ['public.users','public.transactions','public.snapshots','public.goals','public.budgets','public.streaks',
            'public.chat_history','public.spend_checks','public.merchant_embeddings','auth.users','auth.sessions','auth.mfa_factors']
        def snapshot(database):
            return {table:sql(database,f"select coalesce(jsonb_agg(row order by row::text),'[]'::jsonb) from (select to_jsonb(t) as row from {table} t) data;").strip()
                    for table in tables}
        expected = snapshot('source_synthetic')
        with tempfile.TemporaryDirectory(prefix='finio-drill-key-') as temporary:
            identity = Path(temporary)/'identity.txt'
            command([str(keygen),'-o',str(identity)])
            identity.chmod(0o600)
            recipient = command([str(keygen),'-y',str(identity)]).decode().strip()
            encrypted = output_directory/('synthetic-'+uuid4().hex[:8]+'.dump.age')
            encrypt_stream(docker+['exec',name,'pg_dump','-U','finio','-d','source_synthetic','-Fc','--no-owner'],
                age,recipient,encrypted,timeout=60)
            command(docker+['exec',name,'createdb','-U','finio','restored_synthetic'])
            with tempfile.TemporaryFile() as errors:
                decrypt = subprocess.Popen([str(age),'-d','-i',str(identity),str(encrypted)],stdout=subprocess.PIPE,stderr=errors)
                try:
                    restore = subprocess.run(docker+['exec','-i',name,'pg_restore','--exit-on-error','--no-owner','-U','finio','-d','restored_synthetic'],
                        stdin=decrypt.stdout,stdout=subprocess.DEVNULL,stderr=errors,timeout=60)
                    decrypt.stdout.close()
                    if restore.returncode or decrypt.wait(timeout=60):
                        raise RuntimeError('Isolated restore failed.')
                finally:
                    if decrypt.poll() is None: decrypt.kill(); decrypt.wait()
            if snapshot('restored_synthetic') != expected:
                raise RuntimeError('Restored rows differ from the synthetic source.')
            altered = Path(temporary)/'altered.age'
            payload=bytearray(encrypted.read_bytes());payload[-1]^=1;altered.write_bytes(payload)
            tamper=subprocess.run([str(age),'-d','-i',str(identity),str(altered)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            if tamper.returncode == 0:
                raise RuntimeError('Corrupted encrypted backup was accepted.')
            def owner_count(subject, aal):
                claims=json.dumps({'sub':subject,'session_id':subject,'aal':aal})
                result=sql('restored_synthetic',f"set role authenticated;set request.jwt.claim.sub='{subject}';set request.jwt.claims='{claims}';select count(*) from transactions;")
                return int(result.decode().strip().splitlines()[-1])
            if owner_count(USER_A,'aal1')!=3 or owner_count(USER_B,'aal1')!=0 or owner_count(USER_B,'aal2')!=1:
                raise RuntimeError('Restored ownership/MFA policies did not behave correctly.')
            sql('restored_synthetic',f"delete from auth.sessions where user_id='{USER_A}';")
            if owner_count(USER_A,'aal1')!=0:
                raise RuntimeError('Restored session-revocation policy failed.')
        result={'kind':'synthetic local PostgreSQL recovery drill','live_database_accessed':False,'tables_compared':len(tables),
            'exact_rows_restored':True,'ownership_and_mfa_restored':True,'revoked_session_denied':True,
            'tampered_ciphertext_rejected':True,'plaintext_dump_written':False,'temporary_identity_removed':True,
            'duration_seconds':round(time.monotonic()-started,2),'image':IMAGE,'encrypted_fixture':encrypted.name}
        (output_directory/'recovery-result.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    finally:
        subprocess.run(docker+['stop',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--age-directory',required=True)
    parser.add_argument('--output-directory',required=True)
    parser.add_argument('--docker-config')
    parser.add_argument('--docker-host')
    args=parser.parse_args()
    try:
        result=run_drill(args.age_directory,args.output_directory,args.docker_config,args.docker_host)
    except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as error:
        print(json.dumps({'passed':False,'error':type(error).__name__,'message':'The synthetic recovery drill failed. No live database was accessed.'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
