"""Normal creation receipts and preservation, in the existing CORE ledger only."""
from copy import deepcopy
from pathlib import Path

from coordination import SCHEMA, writer_lock
from evidence import artifact, fingerprint, io_path, load_bound, ref, report_for, require, write_atomic


# Only declared contract slots carry references; path/hash-shaped JSON elsewhere
# is data. Lists in these slots retain the same reference requirement.
REFERENCE_FIELDS = {'evidence', 'report', 'delivery', 'proof', 'authority', 'observation',
    'handoff', 'spec', 'approval', 'presentation', 'integration', 'independent_review',
    'author_report', 'merge_authority', 'merge_verification', 'remote_verification', 'artifacts',
    'resource_authority', 'independent_quality', 'technical_acceptance', 'authority_evidence',
    'discussion', 'check', 'start_reference', 'specs', 'probe_evidence', 'basis',
    'merge_observation', 'imported_author_report', 'original_report', 'artifact',
    'author_source_manifest', 'cleanup_proof', 'continuation', 'delivery_check', 'dispatch_result',
    'host_confirmation', 'inbound', 'launch', 'manifest', 'native_request', 'outbound',
    'reconcile_result', 'reference', 'release_receipt', 'request', 'request_evidence', 'result',
    'source', 'transport_evidence', 'unused_proof', 'backup_evidence', 'installation_evidence',
    'real_task_evidence', 'cancellation_verification',
    'actual_App_final', 'old_review', 'merged_source_archive', 'fresh_checks',
    'packaging14_and_negatives3_reused', 'probe', 'raw_journals', 'preservation'}


def managed_path(state, path):
    from cleanup import canonical_path
    root = canonical_path(state['runtime']['cleanup_root'])
    target = canonical_path(path)
    require(Path(path).is_absolute() and target != root and target.is_relative_to(root),
            'new resource must be inside the managed cleanup root')
    return target


def actor(state, task_id, actor_id):
    """Existing task approval is a bounded cleanup grant, never a global writer role."""
    expected = (state.get('delivery') or {}).get('agent_id',state['coordinator_id'])
    if actor_id == expected:
        return None
    task = state['tasks'][task_id]
    authority = task.get('resource_authority')
    if authority is None:
        observed = state['runtime'].get('observed_dispatches',{}).get(task_id)
        if observed:
            authority = load_bound(observed['proof'])['approval']
        elif task.get('cli_dispatch'):
            authority = task['cli_dispatch']['command']['approval']
    require(authority, 'frozen task-specific resource authority required')
    from real_use_closeout import approval
    approved = approval(state,task_id,authority,actor_id)
    require('bounded_new_task_resource_cleanup' in approved.get('authorized',[]) and
            task.get('task_kind','code') == 'code', 'explicit bounded new-task resource cleanup authority required')
    matches = [item for item in approved['tasks'] if item.get('id') == task_id]
    require(len(matches) == 1 and matches[0].get('branch') == task['branch'] and
            Path(matches[0].get('worktree','')).resolve() == Path(task['worktree']).resolve(),
            'exact approved new-task worktree/branch required')
    return authority


def agent(state, task_id, authority):
    """Called in the same transaction that learns the actual host identity."""
    task = state['tasks'][task_id]
    identity = task['owner_agent_id']
    executor = state['runtime']['executors'][identity]
    require(identity not in (state['coordinator_id'], (state.get('delivery') or {}).get('agent_id')),
            'main/integration actor is not a reclaimable developer')
    key = task_id + ':agent'
    resources = state['runtime'].setdefault('resources', {})
    value = dict(id=key, kind='agent', executor_id=identity, generation=executor['generation'],
        task_id=task_id, project_id=state['project']['id'], authority=authority, references=[task_id],
        attempt_id=task.get('worker_run', {}).get('attempt_id', 'resource-'+key), cleanup_status='retained')
    if key in resources:
        require(all(resources[key].get(k) == value[k] for k in ('task_id','executor_id','generation')),
                'registered author changed')
    else:
        resources[key] = value


def inventory(state, task_id, *, author=True, require_paths=True):
    """Strict only for new managed tasks; never retrofits historical resources."""
    task = state['tasks'][task_id]
    if not task.get('resource_contract'):
        return
    resources = state['runtime'].get('resources', {})
    tree = resources.get(task_id+':worktree', {})
    require(tree.get('task_id') == task_id and tree.get('kind') == 'worktree' and
            tree.get('path_identity') and Path(tree['path']).resolve() == Path(task['worktree']).resolve(),
            'actual worktree must be registered before managed execution/cleanup')
    if require_paths and tree.get('cleanup_status') != 'cleaned':
        require(io_path(tree['path']).exists(), 'registered worktree disappeared before confirmed cleanup')
        info = io_path(tree['path']).stat()
        require([info.st_dev,info.st_ino] == tree['path_identity'], 'registered worktree now identifies another object')
    if author:
        item = resources.get(task_id+':agent', {})
        require(item.get('task_id') == task_id and item.get('executor_id') == task['owner_agent_id'] and
                item.get('generation') == state['runtime']['executors'][task['owner_agent_id']]['generation'],
                'actual author must be registered before managed cleanup')
    pending = [o for o in state['runtime']['operations'].values() if o.get('operation') == 'create-resource'
               and o.get('task_id') == task_id and o.get('status') != 'registered']
    require(not pending, 'resource creation result needs observation; no complete managed inventory')


def create(runtime, task_id, operation_id, resource, authority, actor, invoke, *, authorized=False):
    require(authorized is True and operation_id, 'exact resource creation authorization required')
    artifact(authority)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._resource_actor(state, actor, task_id); runtime._scope(state, task_id)
        task = state['tasks'][task_id]
        require(task.get('resource_contract') == 'new-task/1' and task['phase'] not in ('completed','cancelled'),
                'unfinished managed new task required')
        target = managed_path(state, resource['path'])
        require(resource['kind'] in ('worktree','database','environment','artifact'), 'supported finite creation kind required')
        key = resource['id']
        old = state['runtime']['operations'].get(operation_id)
        identity = dict(task_id=task_id, resource=resource, authority=authority)
        if old:
            require(all(old.get(k) == v for k,v in identity.items()), 'creation operation identity differs')
            return deepcopy(old)
        require(key not in state['runtime'].get('resources', {}) and not io_path(target).exists(),
                'new resource only; no adoption or overwrite')
        if resource['kind'] == 'worktree':
            require(key == task_id+':worktree' and target == Path(task['worktree']).resolve(), 'planned worktree differs')
        for other in state['runtime'].get('resources', {}).values():
            if other.get('path') and other['cleanup_status'] != 'cleaned':
                path = Path(other['path']).resolve()
                require(not target.is_relative_to(path) and not path.is_relative_to(target), 'overlapping resource')
        record = dict(identity, operation='create-resource', operation_id=operation_id, status='intent', owner=actor)
        state['runtime']['operations'][operation_id] = record
        runtime._save(state, dict(op='create-resource-intent', task_id=task_id, operation_id=operation_id))
        try:
            # Host callback must register actual RESOURCE identity before returning.
            actual = invoke(deepcopy(resource))
            require(io_path(target).exists(), 'creation has no actual object')
            info = io_path(target).stat()
            value = dict(resource, **actual, path=str(target), path_identity=[info.st_dev,info.st_ino],
                task_id=task_id, project_id=runtime.project_id, authority=authority, references=[task_id],
                cleanup_status='retained')
            state['runtime'].setdefault('resources', {})[key] = value
            record.update(status='registered', actual=deepcopy(value))
        except Exception as error:
            record.update(status='unknown', reason=type(error).__name__ + ': ' + str(error),
                          next_action='inspect_exact_creation_effect_without_repeating')
        runtime._save(state, dict(op='create-resource-outcome', task_id=task_id, operation_id=operation_id))
        return deepcopy(record)


def preserved_binding(task):
    return {key: deepcopy(task.get(key)) for key in ('owner_agent_id','worktree','branch','delivery_revision',
        'delivery','merge','review','independent_quality','technical_acceptance','integration','mr','development_binding',
        'requirements','resource_authority')}


def remote_binding(state, task_id, source, reference, scope):
    from urllib.parse import urlsplit
    task=state['tasks'][task_id];remote=load_bound(reference)
    require(remote.get('schema')=='remote-main-completion/1' and remote.get('scope')==scope and
        remote.get('project_id')==state['project']['id'] and remote.get('task_id')==task_id and
        remote.get('delivery_revision')==task['delivery_revision'] and remote.get('report')==task['delivery'] and
        remote.get('source_digest')==source['digest'] and remote.get('source_head')==task['merge']['head_sha'] and
        remote.get('main_ref')=='refs/heads/'+state['project']['main_branch'] and remote.get('main_sha') and
        remote.get('pr_url')==task['mr']['url'] and remote.get('contains_source') is True and remote.get('author_clean') is True and
        remote.get('remote_identity'), 'current cloud PR/main/source binding required')
    commands=remote.get('commands')
    require(isinstance(commands,list) and len(commands)==2 and all(row.get('exit_code')==0 for row in commands) and
        commands[0].get('arguments')==['ls-remote','--exit-code','origin',remote['main_ref']] and
        commands[0].get('stdout','').split()==[remote['main_sha'],remote['main_ref']] and
        commands[1].get('arguments')==['merge-base',remote['source_head'],remote['main_sha']] and
        commands[1].get('stdout')==remote['source_head'],'actual remote ref and ancestry observations required')
    require(scope=='fixture_only' or remote['remote_identity']!='explicit-local-remote-fixture','fixture is not cloud verification')
    parsed=urlsplit(task['mr']['url'].split('/pull/')[0].split('/-/merge_requests/')[0])
    expected=(parsed.hostname+'/'+parsed.path.strip('/').removesuffix('.git')).casefold()
    require(remote['remote_identity']==expected or
        scope=='fixture_only' and remote['remote_identity']=='explicit-local-remote-fixture','remote repository differs from approved PR')


def preserve(runtime, task_id, destination, handoff, actor, *, merge_verification=None, scope='host_verified', extra=(),
             remote_verification=None):
    """Copy the exact artifact graph; do not rewrite reports or manufacture review."""
    from cleanup import canonical_path, eligibility
    artifact(handoff)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._resource_actor(state, actor, task_id); runtime._scope(state, task_id)
        task = state['tasks'][task_id]
        require(task['phase'] == 'completed' and task.get('merge', {}).get('verified'), 'verified main completion required')
        inventory(state, task_id)
        report = report_for(state, task_id)
        require(scope=='fixture_only' or remote_verification, 'actual cloud main verification required before reclamation preservation')
        if remote_verification:remote_binding(state,task_id,report['source'],remote_verification,scope)
        require(Path(destination).is_absolute(), 'absolute preservation directory required')
        destination = canonical_path(destination)
        require(not io_path(destination).exists(), 'new absolute preservation directory required')
        for root in [runtime.project_root, Path(task['worktree']), Path(state['runtime']['cleanup_root'])]:
            require(not destination.is_relative_to(canonical_path(root)), 'preservation must outlive resources')
        for resource in state['runtime'].get('resources', {}).values():
            require(not resource.get('path') or not destination.is_relative_to(canonical_path(resource['path'])),
                    'preservation inside resource')
        files, seen = [], set()
        def source_inventory(value):
            return isinstance(value, dict) and value.get('kind') in ('git', 'directory') and \
                {'root','files','extra','digest'}.issubset(value)
        def source_identity(value):
            return (canonical_path(value['path']), value['sha256'])
        def copy(value, required=False, declared=False):
            # Reference shape and an inline evidence declaration are separate;
            # a referenced file starts its own JSON content contract below.
            if isinstance(value, dict):
                reference = required and 'path' in value and 'sha256' in value
                if reference:
                    original = dict(path=value['path'], sha256=value['sha256'])
                    token = fingerprint(original)
                    if token not in seen:
                        seen.add(token)
                        source = artifact(original)
                        data = source.read_bytes()
                        target = destination/(token+source.suffix)
                        io_path(target).parent.mkdir(parents=True, exist_ok=True)
                        with io_path(target).open('xb') as stream: stream.write(data)
                        files.append(dict(ref(target), original=original))
                        # JSON references retain their original identities; copies resolve only after confirmed cleanup.
                        import json
                        try: nested = json.loads(data.decode('utf-8-sig'))
                        except (ValueError, UnicodeError): nested = None
                        # A JSON root list of absolute artifact refs is a manifest.
                        manifest = isinstance(nested, list) and bool(nested) and all(
                            isinstance(row, dict) and 'path' in row and 'sha256' in row and
                            Path(row['path']).is_absolute() for row in nested)
                        copy(nested, manifest, manifest)
                # Serialized state, host packets and dispatch requests are
                # captured values, not another live graph. References above
                # are still checked even when they carry packet metadata.
                elif not declared and (value.get('schema') in (SCHEMA, 'dispatch-host-result/1') or \
                        value.get('kind') in ('result', 'host_call', 'host_result') or \
                        {'request_id','project_id','task_id','requirement','approval','worktree','branch'}.issubset(value)): return
                elif source_inventory(value):
                    # Supplements already in the tracked inventory are code identities;
                    # necessary non-Git extras still require exact external copies.
                    source_files = {(canonical_path(Path(value['root'])/row['relative']), row['sha256'])
                        for row in value['files'] if value['kind'] == 'git' and row.get('relative') and row.get('sha256')}
                    for nested in value['extra']:
                        if not Path(nested['path']).is_absolute() or source_identity(nested) not in source_files:
                            copy(nested, True, True)
                    if not declared: return
                if {'executor_id','generation','files','handoff','unregistered_evidence_preserved'}.issubset(value):
                    copy(value['files'], True, True)
                # Inventory rows and reference metadata can both contain real evidence.
                for key, nested in value.items():
                    # A preserved copy's original is an identity, not a live dependency.
                    if not (reference and key == 'original'):
                        copy(nested, key in REFERENCE_FIELDS, declared or key in
                             ('evidence','artifacts','backup_evidence','installation_evidence','real_task_evidence','raw_journals','specs'))
            elif isinstance(value, list):
                for nested in value: copy(nested, required, declared)
        copy(preserved_binding(task)); copy(handoff, True); copy(list(extra), True, True);copy(remote_verification, True)
        copy(state['runtime']['executors'][task['owner_agent_id']])
        for resource in state['runtime'].get('resources', {}).values():
            if resource.get('task_id') == task_id: copy(resource)
        for receipt in state['receipts'].values():
            if receipt.get('payload', {}).get('task_id') == task_id: copy(receipt)
        proof = merge_verification or task['merge']['proof']; copy(proof, True)
        manifest = dict(executor_id=task['owner_agent_id'], generation=state['runtime']['executors'][task['owner_agent_id']]['generation'],
            files=files, merge_verification=proof, handoff=handoff, unregistered_evidence_preserved=True,
            project_id=runtime.project_id, task_id=task_id, binding=preserved_binding(task), source=report['source'], scope=scope)
        if remote_verification:manifest['remote_verification']=remote_verification
        path = destination/'preservation.json'; write_atomic(path, manifest); reference = ref(path)
        task['preservation'] = reference
        eligibility(runtime, state, task_id, reference, scope)
        runtime._save(state, dict(op='preserve-task', task_id=task_id, preservation=reference))
        return reference
