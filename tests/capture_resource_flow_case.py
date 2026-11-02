"""Freeze one explicitly selected failed-native/managed-CLI case; never write live inputs."""
import argparse
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),('\\\\?\\' if sys.platform=='win32' else '')+str(ROOT/'skills/isolated-parallel-development-coordination/scripts')]
from evidence import io_path,ref,load_bound,write_atomic


def capture(ledger,controller,launch,process_created,output):
    if not output.is_absolute() or output.exists():raise ValueError('new explicit owned capture directory required')
    state=json.loads(io_path(ledger).read_text(encoding='utf-8-sig'));actual=load_bound(ref(launch));created=load_bound(ref(process_created))
    key=actual['task_id'];task=state['tasks'][key]
    assert task['owner_agent_id'] is None and task['worktree']==actual['worktree']
    native=[k for k,v in state['runtime']['operations'].items() if v.get('task_id')==key and v.get('operation')=='spawn']
    assert len(native)==1 and state['runtime']['operations'][native[0]]['status']=='capacity'
    from codex_parallel_coordination.resources.windows_resources import observe_job,pid_identity
    observed=observe_job(actual['job_name']);identity=pid_identity(created['identity']['pid'])
    assert identity['created']==created['identity']['created'] and identity['running'] and any(
        m['pid']==identity['pid'] and m['created']==identity['created'] for m in observed['members'])
    output.mkdir(parents=True)
    for name,path in [('ledger.json',ledger),('controller.json',controller),('actual-launch.json',launch),('process-created.json',process_created)]:
        (output/name).write_bytes(io_path(path).read_bytes())
    data=io_path(actual['stdout_path']).read_bytes();data=data.rsplit(b'\n',1)[0]+b'\n'
    (output/'cli.jsonl').write_bytes(data)
    mapped=dict(actual,stdout_path=str(output/'cli.jsonl'))
    write_atomic(output/'isolated-launch.json',mapped)
    from demand_routing import current_requirement
    spec=current_requirement(state,task['routing']['requirement_id'])['spec']
    value=dict(task_id=key,project_root=state['project']['root'],actor_fixture='designated integrator in isolated ledger only',
        author_thread=actual['thread_id'],prior_operation_id=native[0],approval=task['resource_authority'],spec=spec,
        backend_authority=actual['authority'],native_original=state['runtime']['operations'][native[0]],
        inputs={name:ref(output/name) for name in ['ledger.json','controller.json','actual-launch.json','process-created.json','cli.jsonl','isolated-launch.json']},
        live_inputs=[ref(ledger),ref(controller),ref(launch),ref(process_created)],
        process_observation=dict(identity=identity,job_name=actual['job_name'],member=True),
        mapping=dict(stdout_only=[actual['stdout_path'],str(output/'cli.jsonl')]),
        boundary='Original task and failed operation copied unchanged. Only isolated ledger is mutated; role/approval sideband is a declared fixture. OS Job/PID and CLI thread were observed at capture; no native actions.')
    write_atomic(output/'case.json',value)
    return output/'case.json'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['ledger','controller','launch','process-created','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();print(capture(args.ledger,args.controller,args.launch,args.process_created,args.output))
