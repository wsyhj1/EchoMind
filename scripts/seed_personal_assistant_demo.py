"""Create a clearly labelled, idempotent local demo. No model or private-document access."""
import argparse
import json
from uuid import NAMESPACE_URL,uuid5
from urllib.parse import urlsplit

import requests


def key(name):
    return str(uuid5(NAMESPACE_URL,'echomind-personal-assistant-demo-v1:'+name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',default='http://127.0.0.1:8000')
    args = parser.parse_args()
    origin = urlsplit(args.api)
    if origin.scheme!='http' or origin.hostname not in ('127.0.0.1','localhost','::1') or origin.username or origin.password:
        parser.error('The demo script only supports a loopback local API.')
    api = args.api.rstrip('/')
    session = requests.Session()
    session.trust_env = False
    def get(path):
        response = session.get(api+path,timeout=10,allow_redirects=False)
        response.raise_for_status()
        return response.json()
    def post(path,body):
        response = session.post(api+path,json=body,timeout=15,allow_redirects=False)
        if not response.ok:
            raise RuntimeError(f'Demo operation failed ({response.status_code}): '+response.text[:500])
        return response.json()
    identity = get('/auth/me')
    if identity['mode']!='local':
        raise SystemExit('This helper is for the existing local workspace only. Create your project from the authenticated UI.')
    project = post('/assistant/projects',{'client_create_id':key('create-project'),
        'title':'示例 · EchoMind 工作与学习','goal':'演示数据：体验任务、项目记忆、工作恢复和复盘；不代表你的实际进展。','scope_id':'默认知识库'})
    pid = project['project_id']
    revision = 1
    def action(name,payload):
        nonlocal revision
        result = post(f'/assistant/projects/{pid}/actions',{'operation_id':key(name),
            'expected_revision':revision,'action':payload})
        revision = result['project']['revision']
        return result
    plan = action('seed-plan',{'kind':'add_tasks','tasks':[
        {'title':'演示：整理个人助手的产品目标','estimated_minutes':20,'priority':'high'},
        {'title':'演示：学习项目状态与聊天历史的区别','category':'study','estimated_minutes':25},
        {'title':'演示：整理工作台交互反馈','estimated_minutes':30,'priority':'high'},
        {'title':'演示：选择未来提醒的通知渠道','estimated_minutes':15}]})
    tasks = plan['entity']['tasks']
    action('complete-product-goal',{'kind':'update_task','task_id':tasks[0]['task_id'],'status':'done',
        'completion_note':'演示完成记录，用于展示复盘来源；不是实际开发成绩。'})
    action('start-learning',{'kind':'update_task','task_id':tasks[1]['task_id'],'status':'in_progress'})
    action('block-notification',{'kind':'update_task','task_id':tasks[3]['task_id'],'status':'blocked',
        'blocker':'演示：尚未选择通知渠道，先不承诺后台提醒。'})
    action('decision',{'kind':'add_note','note_kind':'decision','content':'演示决定：先打通项目状态与跨会话恢复，再接提醒与日历。'})
    action('learning',{'kind':'add_note','note_kind':'learning','content':'演示学习记录：会话历史用于理解讨论，结构化项目状态用于确认已保存任务；两者由项目ID关联。'})
    action('resource',{'kind':'add_resource','resource_type':'link','label':'PostgreSQL 事务官方教程',
        'reference':'https://www.postgresql.org/docs/current/tutorial-transactions.html'})
    conversation = post('/conversations',{'client_create_id':key('project-conversation'),'project_id':pid,'scope_id':'默认知识库'})
    workspace = get('/assistant/projects/'+pid)
    report = {'demo':True,'project_id':pid,'revision':workspace['project']['revision'],
        'tasks':len(workspace['tasks']),'notes':len(workspace['notes']),
        'workspace_url':'http://127.0.0.1:8501/?project='+pid,
        'conversation_id':conversation['conversation_id'],'model_calls':0}
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
