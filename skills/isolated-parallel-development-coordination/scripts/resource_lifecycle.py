"""Normal creation receipts and preservation, in the existing CORE ledger only."""
from copy import deepcopy
from pathlib import Path

from coordination import writer_lock
from evidence import artifact, fingerprint, historical_git_artifact, io_path, load_bound, ref, report_for, require, write_atomic


# Only declared contract slots carry references; path/hash-shaped JSON elsewhere
# is data. Lists in these slots retain the same reference requirement.
REFERENCE_FIELDS = {'evidence', 'report', 'review', 'delivery', 'proof', 'authority', 'observation',
    'handoff', 'spec', 'approval', 'presentation', 'integration', 'independent_review',
    'author_report', 'merge_authority', 'merge_verification', 'remote_verification', 'artifacts',
    'resource_authority', 'independent_quality', 'technical_acceptance', 'authority_evidence',
    'discussion', 'check', 'start_reference', 'specs', 'probe_evidence', 'basis',
    'merge_observation', 'imported_author_report', 'original_report', 'artifact',
    'author_source_manifest', 'cleanup_proof', 'continuation', 'delivery_check', 'dispatch_result',
    'host_confirmation', 'inbound', 'launch', 'manifest', 'native_request', 'outbound', 'declaration',
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
    from task_state import receiver
    if task.get('state_contract') and actor_id == receiver(state, task_id):
        return None
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


def temporary_actor(state, task_id, actor_id, kind):
    """Developer capability is limited to approved temporary kinds on this task."""
    task = state['tasks'][task_id]
    if actor_id != task['owner_agent_id']:
        return actor(state, task_id, actor_id)
    from task_state import receiver
    authority = task.get('resource_authority')
    require(authority, 'original temporary resource approval required')
    from real_use_closeout import approval
    approved = approval(state, task_id, authority, receiver(state, task_id) or load_bound(authority).get("integration_agent"))
    item = next(x for x in approved['tasks'] if x['id'] == task_id)
    require(kind in item.get('temporary_resources', []) and kind in ('environment','database','artifact','process'),
            'temporary kind outside original task authorization')
    executor = state['runtime']['executors'][actor_id]
    require(executor['lease'] == 'open' and task['phase'] not in ('completed','cancelled') and
            not task.get('cancellation_request'), 'stopped task cannot create temporary resources')
    return authority


def ended(state, task_id, scope):
    """Consume canonical STATE completion, without opening reports or file graphs."""
    task = state['tasks'][task_id]
    require(scope in ('fixture_only','host_verified'), 'explicit observation scope required')
    if task['phase'] == 'cancelled':
        from development_lifecycle import cancelled
        cancelled(state, task_id, scope)
    else:
        require(task['phase'] == 'completed' and task.get('merge', {}).get('verified') is True,
                'task must be merged and verified or explicitly cancelled')
        merge = task['merge']
        observed_scope = merge.get('scope', merge.get('observation', {}).get('scope'))
        require(observed_scope == scope, 'completion observation scope differs')
    require(not task.get('publication_dependencies') and not task.get('acceptance_environment_requires_agent'),
            'live task dependency protects resources')
    require(state['items'].get(state['active_human_item'], {}).get('task_id') != task_id,
            'active user item protects task')
    return task


def agent_eligibility(runtime, state, task_id, scope, *, closing_operation=None):
    task = ended(state, task_id, scope)
    identity = task['owner_agent_id']
    require(identity and identity not in (state['coordinator_id'], (state.get('delivery') or {}).get('agent_id')),
            'actual exclusive developer required')
    executor = state['runtime']['executors'][identity]
    require(not executor.get('shared') and not executor.get('protected'), 'shared/protected executor')
    require(all(t['phase'] in ('completed','cancelled') for t in state['tasks'].values()
                if t['owner_agent_id'] == identity), 'executor still serves active work')
    require(executor.get('active_children') == [] and executor.get('pending_work') is False and
            (executor['execution'] in ('completed','idle') or executor['lease']=='closed'),
            'active or unverified executor/descendants')
    observation = load_bound(executor['observation'])
    require(observation.get('executor_id') == identity and observation.get('generation') == executor['generation'] and
            observation.get('execution') in ('completed','idle') and observation.get('active_children') == [] and
            observation.get('pending_work') is False, 'stale or active executor observation')
    artifact(executor['authority'])
    item = state['runtime'].get('resources', {}).get(task_id+':agent', {})
    require(item.get('executor_id') == identity and item.get('generation') == executor['generation'] and
            item.get('task_id') == task_id and item.get('references') == [task_id] and
            not item.get('protected') and not item.get('shared'), 'actual registered exclusive author required')
    pending = state['runtime']['operations'].get(closing_operation, {})
    closing = executor['lease'] == 'closing' and pending.get('operation') == 'close' and pending.get('task_id') == task_id and \
        pending.get('executor_id') == identity and pending.get('generation') == executor['generation']
    require(executor['lease'] in ('open','closed') or closing, 'uncertain close must be reconciled')
    return executor


def create(runtime, task_id, operation_id, resource, authority, actor, invoke, *, authorized=False):
    from contextlib import ExitStack
    from evidence import callback_result
    require(authorized is True and operation_id, 'exact resource creation authorization required')
    artifact(authority)
    with ExitStack() as locks:
        locks.enter_context(writer_lock(runtime.path))
        state = runtime._load(); runtime._scope(state, task_id)
        check = runtime._resource_actor if resource['kind']=='worktree' else temporary_actor
        if resource['kind']=='worktree': check(state, actor, task_id)
        else: check(state, task_id, actor, resource['kind'])
        task = state['tasks'][task_id]
        require(task.get('resource_contract') == 'new-task/1' and task['phase'] not in ('completed','cancelled') and
                not task.get('cancellation_request'), 'unfinished managed new task required')
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
        for other_id,other in state['runtime']['operations'].items():
            pending=other.get('resource',{})
            if other_id!=operation_id and other.get('operation')=='create-resource' and other.get('status')!='registered' and pending:
                path=Path(pending['path']).resolve()
                require(key!=pending['id'] and not target.is_relative_to(path) and not path.is_relative_to(target),
                        'original uncertain creation owns resource identity/path')
        for other in state['runtime'].get('resources', {}).values():
            if other.get('path') and other['cleanup_status'] != 'cleaned':
                path = Path(other['path']).resolve()
                require(not target.is_relative_to(path) and not path.is_relative_to(target), 'overlapping resource')
        for other_key, other in state['tasks'].items():
            if other_key != task_id and other.get('worktree'):
                path = Path(other['worktree']).resolve()
                require(not target.is_relative_to(path) and not path.is_relative_to(target), 'another task worktree protected')
        record = dict(identity, operation='create-resource', operation_id=operation_id, status='intent', owner=actor)
        binding = dict(project_id=runtime.project_id, operation_id=operation_id, task_id=task_id,
                       actor_id=actor, resource=deepcopy(resource), authority=authority, task=deepcopy(task))
        record['callback_binding'] = binding
        state['runtime']['operations'][operation_id] = record
        runtime._save(state, dict(op='create-resource-intent', task_id=task_id, operation_id=operation_id))
        locks.close()
        try:
            actual = invoke(deepcopy(resource))
            require(io_path(target).exists(), 'creation has no actual object')
            info = io_path(target).stat()
            result = dict(status='registered', actual=dict(resource, **actual, path=str(target),
                path_identity=[info.st_dev,info.st_ino], task_id=task_id, project_id=runtime.project_id,
                authority=authority, references=[task_id], cleanup_status='retained'))
        except Exception as error:
            result = dict(status='unknown', reason=type(error).__name__ + ': ' + str(error),
                          next_action='inspect_exact_creation_effect_without_repeating')
        captured = callback_result(runtime.directory, binding, result=result, capture=True)
        try: locks.enter_context(writer_lock(runtime.path))
        except ValueError as error:
            if not str(error).startswith('writer lock exists;'):raise
            return dict(status='registration_pending',operation_id=operation_id,actual_result=captured,
                        next_action='observe_original_creation_without_repeating',reason=str(error))
        state = runtime._load(); record = state['runtime']['operations'][operation_id]
        record['actual_result'] = captured
        if state['tasks'][task_id] != binding['task']:
            record.update(status='unknown', next_action='reconcile_retained_creation_result', reason='task changed during creation')
        else:
            record.update(result)
            if result['status']=='registered': state['runtime'].setdefault('resources', {})[key] = result['actual']
        record['result_revision']=state['revision']+1
        try:runtime._save(state, dict(op='create-resource-outcome', task_id=task_id, operation_id=operation_id))
        except OSError as error:
            return dict(status='registration_pending',operation_id=operation_id,actual_result=captured,
                        reason=str(error),next_action='observe_original_creation_without_repeating')
        return deepcopy(record)


def observe_creation(runtime, task_id, operation_id, actor_id):
    """Adopt a retained actual create return after registration failure; never invoke it again."""
    from evidence import callback_result
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        operation=state['runtime']['operations'][operation_id]
        require(operation.get('operation')=='create-resource' and operation['task_id']==task_id and operation.get('callback_binding'),
                'original finite creation intent required')
        if operation['status']=='registered':return deepcopy(operation)
        captured=callback_result(runtime.directory,operation['callback_binding'])
        if not captured:return dict(status='unknown',next_action='inspect_original_creation_without_repeating')
        result=load_bound(captured)['result']
        if result['status']!='registered':return dict(result,actual_result=captured)
        resource=result['actual'];target=managed_path(state,resource['path']);info=io_path(target).stat()
        require(resource['id']==operation['resource']['id'] and resource['task_id']==task_id and
                [info.st_dev,info.st_ino]==resource['path_identity'], 'actual created object changed')
        artifact(operation['authority'])
        old=state['runtime'].setdefault('resources',{}).get(resource['id'])
        require(not old or old==resource, 'another resource owns creation result')
        state['runtime']['resources'][resource['id']]=deepcopy(resource)
        operation.update(result,actual_result=captured)
        runtime._save(state,dict(op='create-resource-outcome',task_id=task_id,operation_id=operation_id))
        return deepcopy(operation)


def preserved_binding(task):
    result = {key: deepcopy(task.get(key)) for key in ('owner_agent_id','worktree','branch','delivery_revision',
        'delivery','merge','review','independent_quality','technical_acceptance','integration','mr','development_binding',
        'requirements','resource_authority')}
    if task.get('preservation_classification'):
        result['preservation_classification'] = deepcopy(task['preservation_classification'])
    return result


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
             remote_verification=None, operation_id=None):
    """Copy the exact artifact graph; do not rewrite reports or manufacture review."""
    from cleanup import canonical_path, eligibility
    artifact(handoff)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._resource_actor(state, actor, task_id); runtime._scope(state, task_id)
        task = state['tasks'][task_id]
        require(task['phase'] == 'completed' and task.get('merge', {}).get('verified'), 'verified main completion required')
        from preservation_contract import captures, CAPTURE_SLOTS, extends
        classified = captures(state, task_id)
        inventory(state, task_id)
        report = report_for(state, task_id)
        require(Path(destination).is_absolute(), 'absolute preservation directory required')
        destination = canonical_path(destination)
        marker=io_path(destination/'preservation-intent.json')
        context=dict(project_id=runtime.project_id,task_id=task_id,operation_id=operation_id,
            binding=preserved_binding(task),destination=str(destination),handoff=handoff,scope=scope,
            merge_verification=merge_verification or task['merge']['proof'],extra=list(extra))
        addition = None
        amendment = io_path(destination/'preservation-classification-intent.json')
        if operation_id:
            operation=state['runtime']['operations'][operation_id]
            require(operation.get('operation')=='continuation-effect' and operation['task_id']==task_id and
                    operation['status'] in ('intent','unknown'), 'original preservation operation required')
        if io_path(destination).exists():
            require(operation_id and marker.is_file(),'new absolute preservation directory or original marked copy required')
            prior=load_bound(ref(marker))
            prior_context = dict(context)
            current_classification = task.get('preservation_classification')
            old_classification = prior.get('binding', {}).get('preservation_classification')
            if current_classification!=old_classification and extends(current_classification, old_classification):
                prior_context['binding'] = dict(context['binding'])
                if old_classification is None: prior_context['binding'].pop('preservation_classification')
                else: prior_context['binding']['preservation_classification'] = old_classification
                require(all(prior.get(key)==value for key,value in prior_context.items()),'preservation resume binding changed')
                # Append the exact classification to the same operation; keep the old
                # marker and failed partial bytes intact for review.
                addition = dict(context, original_intent=ref(marker))
                if amendment.exists() and load_bound(ref(amendment))!=addition:
                    amendment = io_path(destination/('preservation-classification-intent-'+fingerprint(current_classification)+'.json'))
                if amendment.exists():
                    require(load_bound(ref(amendment))==addition,'preservation classification intent changed')
            else:
                require(all(prior.get(key)==value for key,value in context.items()),'preservation resume binding changed')
            require(not remote_verification or remote_verification==prior.get('remote_verification'),'original remote observation required')
            remote_verification=prior.get('remote_verification')
        require(scope=='fixture_only' or remote_verification, 'actual cloud main verification required before reclamation preservation')
        if remote_verification:remote_binding(state,task_id,report['source'],remote_verification,scope)
        for root in [runtime.project_root, Path(task['worktree']), Path(state['runtime']['cleanup_root'])]:
            require(not destination.is_relative_to(canonical_path(root)), 'preservation must outlive resources')
        for resource in state['runtime'].get('resources', {}).values():
            require(not resource.get('path') or not destination.is_relative_to(canonical_path(resource['path'])),
                    'preservation inside resource')
        if addition and not amendment.exists(): write_atomic(amendment, addition)
        if operation_id and not marker.exists():
            marker.parent.mkdir(parents=True,exist_ok=False)
            write_atomic(marker,dict(context,remote_verification=remote_verification))
        files, seen, git_sources = [], set(), []
        def source_inventory(value):
            return isinstance(value, dict) and value.get('kind') in ('git', 'directory') and \
                {'root','files','extra','digest'}.issubset(value)
        def source_identity(value):
            return (canonical_path(value['path']), value['sha256'])
        reports = [row['payload']['report'] for row in state['receipts'].values() if
            row.get('status') == 'processed' and row.get('payload', {}).get('task_id') == task_id and
            row['payload'].get('kind') == 'completed']
        def copy(value, required=False, declared=False, report_ref=None, slot=None):
            # Reference shape and an inline evidence declaration are separate;
            # a referenced file starts its own JSON content contract below.
            if isinstance(value, dict):
                reference = required and 'path' in value and 'sha256' in value
                if reference:
                    original = dict(path=value['path'], sha256=value['sha256'])
                    capture = next((row for row in classified if row['original']==original), None)
                    require(not capture or slot in CAPTURE_SLOTS[capture['contract']],
                            'required delivery/authority/proof cannot be classified as capture')
                    token = fingerprint(original)
                    if token not in seen:
                        seen.add(token)
                        provenance = None
                        try: data = artifact(original).read_bytes()
                        except ValueError:
                            if report_ref is None or not canonical_path(original['path']).is_relative_to(
                                    canonical_path(task['worktree'])): raise
                            data, provenance = historical_git_artifact(state, task_id, report_ref, original)
                        target = destination/(token+Path(original['path']).suffix)
                        io_path(target).parent.mkdir(parents=True, exist_ok=True)
                        if io_path(target).exists():
                            require(operation_id and io_path(target).read_bytes()==data,'partial preserved bytes changed')
                        else:
                            with io_path(target).open('xb') as stream: stream.write(data)
                        row = dict(ref(target), original=original)
                        if capture: row['capture'] = {k:v for k,v in capture.items() if k!='original'}
                        if provenance: row['git_source'] = provenance
                        files.append(row)
                        # Keep JSON identities; historical reads revalidate their Git provenance.
                        import json
                        try: nested = json.loads(data.decode('utf-8-sig'))
                        except (ValueError, UnicodeError): nested = None
                        # A JSON root list of absolute artifact refs is a manifest.
                        manifest = isinstance(nested, list) and bool(nested) and all(
                            isinstance(row, dict) and 'path' in row and 'sha256' in row and
                            Path(row['path']).is_absolute() for row in nested)
                        if not capture:
                            copy(nested, manifest, manifest, original if original in reports else report_ref)
                elif source_inventory(value):
                    # Supplements already in the tracked inventory are code identities;
                    # necessary non-Git extras still require exact external copies.
                    source_files = {(canonical_path(Path(value['root'])/row['relative']), row['sha256'])
                        for row in value['files'] if value['kind'] == 'git' and row.get('relative') and row.get('sha256')}
                    for nested in value['extra']:
                        if not Path(nested['path']).is_absolute() or source_identity(nested) not in source_files:
                            if report_ref and value['kind'] == 'git':
                                try:
                                    _, provenance = historical_git_artifact(state, task_id, report_ref, nested)
                                except ValueError: pass
                                else:
                                    identity = dict(original=nested, git_source=provenance)
                                    if identity not in git_sources: git_sources.append(identity)
                                    continue
                            copy(nested, True, True, report_ref, 'extra')
                    if not declared:
                        for key, nested in value.items():
                            if key not in ('root','kind','files','extra','digest'):
                                copy(nested, key in REFERENCE_FIELDS, key in REFERENCE_FIELDS, report_ref, key)
                        return
                if {'executor_id','generation','files','handoff','unregistered_evidence_preserved'}.issubset(value):
                    copy(value['files'], True, True, report_ref)
                # Inventory rows and reference metadata can both contain real evidence.
                for key, nested in value.items():
                    # A preserved copy's original is an identity, not a live dependency.
                    if not (reference and key == 'original'):
                        copy(nested, key in REFERENCE_FIELDS, declared or key in REFERENCE_FIELDS, report_ref, key)
            elif isinstance(value, list):
                for nested in value: copy(nested, required, declared, report_ref, slot)
        copy(preserved_binding(task)); copy(handoff, True); copy(list(extra), True, True, slot='extra');copy(remote_verification, True)
        copy(state['runtime']['executors'][task['owner_agent_id']])
        for resource in state['runtime'].get('resources', {}).values():
            if resource.get('task_id') == task_id: copy(resource)
        for receipt in state['receipts'].values():
            if receipt.get('payload', {}).get('task_id') == task_id: copy(receipt)
        proof = merge_verification or task['merge']['proof']; copy(proof, True)
        manifest = dict(executor_id=task['owner_agent_id'], generation=state['runtime']['executors'][task['owner_agent_id']]['generation'],
            files=files, merge_verification=proof, handoff=handoff, unregistered_evidence_preserved=True,
            project_id=runtime.project_id, task_id=task_id, binding=preserved_binding(task), source=report['source'], scope=scope)
        if git_sources: manifest['git_sources'] = git_sources
        if task.get('preservation_classification'): manifest['classification'] = task['preservation_classification']
        if amendment.exists(): manifest['classification_intent'] = ref(amendment)
        if remote_verification:manifest['remote_verification']=remote_verification
        path = destination/'preservation.json'; write_atomic(path, manifest); reference = ref(path)
        task['preservation'] = reference
        eligibility(runtime, state, task_id, reference, scope)
        runtime._save(state, dict(op='preserve-task', task_id=task_id, preservation=reference))
        return reference
