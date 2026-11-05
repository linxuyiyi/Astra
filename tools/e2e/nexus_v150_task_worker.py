"""在显式指定的隔离 Nexus/Worker 环境中验证真实原生执行、续答和人工确认。

示例：python tools/e2e/nexus_v150_task_worker.py --base-url http://127.0.0.1:8100 --worker-id e2e-worker
需要已部署且空闲的 Worker 及可用网关模型；会留下本次验收 Task 和原生 Session。
等待期间只读 Task，让后台 SSE 自行推进状态。
"""
import argparse
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=90) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        def waiting(task_id):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                task = request('GET',f'/api/tasks/{task_id}')
                if task['status'] == 'WAITING' and not task['pending_message_id']:
                    assert task['waiting_reason'] == 'confirmation', task['current_progress']
                    return task
                time.sleep(1)
            raise TimeoutError(f'{task_id} 未进入人工处理状态')
        worker = request('GET',f'/api/workers/{args.worker_id}')
        assert worker['status'] == 'IDLE' and worker['enabled'], '需要已启用且空闲的 Worker'
        task = request('POST','/api/tasks',json={
            'title':'v1.5.0 原生执行链路验收','description':'仅输出“执行链路已验证”；不要修改文件或调用工具。',
            'task_type':worker['task_types'][0], 'category_id':worker['category_ids'][0],
            'agent_worker_id':worker['id']})
        task_id = task['id']
        request('POST',f'/api/tasks/{task_id}/start')
        first = waiting(task_id)
        sid = first['current_session_id']
        assert request('GET',f'/api/workers/{worker["id"]}')['status'] == 'WORKING'
        request('POST',f'/api/tasks/{task_id}/opencode/{sid}/session/{sid}/prompt_async',json={
            'parts':[{'type':'text','text':'继续在本会话仅输出“续答已验证”。'}]})
        second = waiting(task_id)
        assert second['current_session_id'] == sid and second['last_message_id'] != first['last_message_id']
        confirmed = request('POST',f'/api/tasks/{task_id}/confirm')
        assert confirmed['status'] == 'SUCCEEDED'
        assert request('GET',f'/api/workers/{worker["id"]}')['status'] == 'IDLE'
        print({'task_id':task_id,'session_id':sid,'status':'SUCCEEDED'})


if __name__ == '__main__':
    main()
