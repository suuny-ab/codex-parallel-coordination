"""Kind-specific canonical completion; never performs host or Git side effects."""
from copy import deepcopy

from evidence import artifact, load_bound, report_for, require, snapshot, verify_source


def definition(task):
    kind=task.get('task_kind','code')
    mode=task.get('acceptance_type','user_required')
    require(kind in ('code','read_only') and mode in ('delivery_verified','user_required'),'unknown task kind/acceptance type')
    require(kind!='code' or mode=='user_required','code retains its original acceptance gate')
    requirements=task.get('requirements')
    if requirements:
        require(requirements.get('task_kind','code')==kind and requirements.get('acceptance_type','user_required')==mode,
                'task kind/acceptance type differs from confirmed scope')
        if kind=='read_only':require(requirements.get('source_baseline'),'read-only confirmed source baseline required')
    return kind,mode


def confirmed(task, operation):
    kind,mode=definition(task)
    require(operation.get('task_kind',kind)==kind and operation.get('acceptance_type',mode)==mode,'confirmation cannot change task definition')
    value={k:operation[k] for k in ('scope','acceptance','user_reference')}
    value.update(task_kind=kind,acceptance_type=mode)
    if kind=='read_only':
        baseline=(task.get('requirements') or {}).get('source_baseline')
        if baseline:verify_source(baseline)
        value['source_baseline']=deepcopy(baseline) if baseline else snapshot(task['worktree'])
    return value


def acceptance(state, task):
    _,mode=definition(task)
    if mode=='user_required':
        record=task.get('acceptance_record') or {}
        require(task['user_acceptance']=='accepted' and record.get('accepted_by')==state['coordinator_id'] and
            record.get('acceptance_type')==mode and record.get('delivery_revision')==task['delivery_revision'] and
            record.get('report')==task['delivery'] and record.get('user_reference'), 'current actual user acceptance required')
        artifact(record['evidence'])


def review(state, key, proof, actor):
    from delivery import require_executor
    task=state['tasks'][key];kind,mode=definition(task)
    require(kind=='read_only','read-only completion cannot finish a code task')
    delegation=require_executor(state,actor,key)
    report=report_for(state,key)
    baseline=task['requirements']['source_baseline']
    require(report['source']==baseline,'read-only source changed from confirmed baseline')
    expected=dict(project_id=state['project']['id'],task_id=key,task_kind=kind,acceptance_type=mode,
        owner_agent_id=task['owner_agent_id'],reviewer_id=actor,delivery_revision=task['delivery_revision'],
        report=task['delivery'],source_digest=report['source']['digest'],source_baseline_digest=baseline['digest'],
        authority=delegation['authority_evidence'],requirements_reference=task['requirements']['user_reference'])
    require(all(proof.get(k)==value for k,value in expected.items()),'current task/revision/report/source/authority/acceptance binding mismatch')
    require(proof.get('status')=='passed' and proof.get('code_changes') is False and
        proof.get('scope') in ('fixture_only','host_verified') and proof.get('evidence'),'explicit independent verification evidence required')
    for item in proof['evidence']:artifact(item)
    require(task['implementation']=='completed' and not task.get('blocker') and
        task.get('worker_run',{}).get('status') not in ('running','queued'),'current work not fully delivered')
    acceptance(state,task)
    return expected


def verified(state, key):
    task=state['tasks'][key];completion=task.get('completion') or {}
    require(task['phase']=='completed' and completion.get('basis')=='read_only_delivery_review','canonical read-only completion missing')
    proof=load_bound(completion['proof'])
    expected=review(state,key,proof,completion['reviewer_id'])
    require(all(completion.get(k)==value for k,value in expected.items()) and completion.get('scope')==proof['scope'],
            'canonical completion binding changed')
    return completion


def finish(runtime, proof_ref, actor):
    from coordination import writer_lock
    proof=load_bound(proof_ref)
    with writer_lock(runtime.path):
        state=runtime._load();runtime._actor(state,actor)
        key=proof['task_id'];runtime._scope(state,key)
        task=state['tasks'][key]
        expected=review(state,key,proof,actor)
        if task['phase']=='completed':
            verified(state,key)
            require(task['completion']['proof']==proof_ref,'different duplicate completion proof')
            return deepcopy(task['completion']['result'])
        require(task['phase'] in ('awaiting_delivery_review','awaiting_integration') and
            proof.get('based_on_revision')==state['revision'],'current completion revision/phase required')
        require(state['items'].get(state['active_human_item'],{}).get('task_id')!=key,'active human item protects task')
        result=dict(task_id=key,task_completed=True,cleanup_completed=False,cleanup_authorized=False,revision=state['revision']+1)
        task.update(phase='completed',completion=dict(expected,basis='read_only_delivery_review',proof=proof_ref,scope=proof['scope'],result=result))
        runtime._save(state,dict(op='complete-read-only',task_id=key,proof=proof_ref))
        return deepcopy(result)


def completion_flags(state, key):
    task=state['tasks'][key];kind,_=definition(task)
    if task['phase']=='cancelled':
        from development_lifecycle import cancelled
        cancelled(state,key)
        return dict(task_kind=kind,task_completion_verified=False,task_completed_on_main=False,
                    read_only_completion_verified=False,task_cancelled_verified=True)
    require(task['phase']=='completed','task not completed')
    if kind=='read_only':verified(state,key)
    else:require(task.get('merge',{}).get('verified') is True,'main not verified')
    return dict(task_kind=kind,task_completion_verified=True,task_completed_on_main=kind=='code',read_only_completion_verified=kind=='read_only')


def preservation_completion(state,key,inventory,scope):
    """Read-only completion provenance; code keeps the existing exact main checks."""
    completion=verified(state,key)
    require(inventory.get('completion_verification')==completion['proof'] and completion['scope']==scope,
            'preservation is not bound to canonical completion/scope')
