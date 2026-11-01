"""Bounded, restartable delivery using the canonical ledger and existing gates.

JSON supplies identities and evidence, never actor identity, tools or authority.
The host supplies capabilities and per-action authorization on every invocation.
"""
from copy import deepcopy
import os
from pathlib import Path

from coordination import writer_lock
from evidence import ERRORS, artifact, fingerprint, load_bound, preserved_read, require


def source(task):
    return {key: deepcopy(task.get(key)) for key in
            ('owner_agent_id', 'worktree', 'branch', 'requirements', 'delivery_revision', 'delivery')}


class Continuation:
    def __init__(self, runtime, reference, actor, *, bridge=None, dispatch=None, available_tools=(),
                 reclaim_authorized=False, install_authorized=False, install=None,
                 successor_authorized=False, successor_origin=None, scope='host_verified'):
        self.runtime, self.reference, self.actor = runtime, reference, actor
        self.request = load_bound(reference)
        require(self.request.get('schema') == 'delivery-continuation/1' and
                self.request.get('project_id') == runtime.project_id and self.request.get('request_id') and
                not set(self.request) - {'schema','project_id','task_id','request_id','approval',
                    'preservation_destination','handoff','installation_required','successor'}, 'bounded continuation request required')
        require(scope in ('fixture_only','host_verified'), 'explicit continuation proof scope required')
        self.key, self.identity = self.request['task_id'], self.request['request_id']
        self.bridge, self.dispatch, self.tools, self.scope = bridge, dispatch, tuple(available_tools), scope
        self.reclaim, self.install_allowed, self.install = reclaim_authorized is True, install_authorized is True, install
        self.successor_allowed, self.origin = successor_authorized is True, successor_origin

    def authorize(self, state):
        require(self.key in state['tasks'], 'existing scoped task required')
        task = state['tasks'][self.key]
        approved = load_bound(self.request['approval'])
        if self.actor == state['coordinator_id']:
            require(task.get('requirements', {}).get('user_reference') == self.request['approval']['path'],
                    'retain original task approval')
            self.runtime._scope(state, self.key)
        else:
            from real_use_closeout import approval
            approval(state, self.key, self.request['approval'], self.actor)
        require(self.actor != task['owner_agent_id'], 'author cannot close its own delivery')
        return approved

    def bind(self):
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); self.authorize(state)
            operations = state['runtime']['operations']
            old = operations.get(self.identity)
            if old:
                require(old.get('operation') == 'delivery-continuation' and old['request'] == self.reference and
                        old['scope'] == self.scope, 'conflicting continuation; retain original operation/request')
                return
            require(not any(o.get('operation') == 'delivery-continuation' and o.get('task_id') == self.key
                            for o in operations.values()), 'task already has a continuation; use original request')
            operations[self.identity] = dict(operation='delivery-continuation', task_id=self.key,
                request=self.reference, scope=self.scope, confirmed=[])
            self.runtime._save(state, dict(op='continuation-bound', task_id=self.key, request=self.reference))

    def pin(self):
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); self.authorize(state)
            task = state['tasks'][self.key]; record = state['runtime']['operations'][self.identity]
            if task.get('delivery'):
                current = source(task)
                require(not record.get('source') or record['source'] == current,
                        'continuation source/report changed; reconcile original operation')
                with preserved_read(state,self.key): artifact(task['delivery'])
                if not record.get('source'):
                    record['source'] = current
                    self.runtime._save(state, dict(op='continuation-source', task_id=self.key))
        return state, task

    def mark(self, step, result):
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); self.authorize(state)
            record = state['runtime']['operations'][self.identity]
            require(not record.get('source') or record['source'] == source(state['tasks'][self.key]),
                    'task changed during step; preserve original result and reconcile')
            if step not in record['confirmed']:
                record['confirmed'].append(step)
                record.setdefault('results', {})[step] = deepcopy(result)
                self.runtime._save(state, dict(op='continuation-step', task_id=self.key, step=step))

    def result(self, step, status, reason, *, owner=None, package=None):
        state = self.runtime._load(); record = state['runtime']['operations'][self.identity]
        task = state['tasks'][self.key]
        installation=deepcopy(record.get('results',{}).get('installation'))
        if installation is None:
            update=state['runtime']['operations'].get(self.identity+':installation')
            installation=dict(status='unknown' if update and update['status'] in ('intent','unknown') else
                'unverified' if update else 'pending' if self.request.get('installation_required') else 'not_requested')
            if update:installation.update(operation_id=self.identity+':installation',reason=update.get('reason'))
        technical_owner=load_bound(self.request['approval']).get('integration_agent') or self.actor
        value = dict(request=self.reference, task_id=self.key, operation_id=self.identity, step=step, status=status,
            confirmed=deepcopy(record['confirmed']), next_owner=owner or technical_owner, next_action=reason, reason=reason,
            task_completed=task['phase'] == 'completed', merge=deepcopy(task.get('merge')),
            cleanup=deepcopy(task.get('cleanup')), scope=self.scope, idle_wakeup=False, automatic_retry=False,
            installation=installation,
            session_loaded='unknown', ledger=str(self.runtime.path), revision=state['revision'])
        if package: value['host_package'] = dict(package, request=self.reference, task_id=self.key)
        with writer_lock(self.runtime.path):
            current = self.runtime._load(); self.authorize(current)
            op = current['runtime']['operations'][self.identity]
            latest = {key: value[key] for key in ('step','status','next_owner','next_action','reason')}
            if op.get('latest') != latest:
                op['latest'] = latest
                self.runtime._save(current, dict(op='continuation-progress',task_id=self.key, **latest))
            value['revision'] = current['revision']
        return value

    def effect(self, step, callback, *, observed=None):
        """Persist before callback; after a crash only an actual observed outcome can finish it."""
        identity = self.identity+':'+step
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); self.authorize(state)
            operations = state['runtime']['operations']; old = operations.get(identity)
            if old:
                require(old.get('operation') == 'continuation-effect' and old['task_id'] == self.key and
                        old['request'] == self.reference, 'effect identity differs')
                if old['status'] == 'confirmed':
                    require(observed is None or step not in ('preservation','installation') or old['result']==observed,
                            'conflicting confirmed evidence; retain original effect')
                    return deepcopy(old['result'])
                if observed is None: return None
                old.update(status='confirmed', result=deepcopy(observed))
                self.runtime._save(state, dict(op='continuation-observed-effect',task_id=self.key,step=step))
                return observed
            if observed is not None:
                operations[identity]=dict(operation='continuation-effect',task_id=self.key,request=self.reference,
                    status='confirmed',source=source(state['tasks'][self.key]),result=deepcopy(observed))
                self.runtime._save(state,dict(op='continuation-observed-effect',task_id=self.key,step=step))
                return observed
            operations[identity] = dict(operation='continuation-effect',task_id=self.key,request=self.reference,
                                       status='intent', owner_pid=os.getpid(), source=source(state['tasks'][self.key]))
            self.runtime._save(state, dict(op='continuation-effect-intent',task_id=self.key,step=step))
        try:
            result = callback()
        except Exception as error:
            with writer_lock(self.runtime.path):
                state = self.runtime._load(); record = state['runtime']['operations'][identity]
                record.update(status='unknown', reason=str(error), error_type=type(error).__name__)
                self.runtime._save(state,dict(op='continuation-effect-unknown',task_id=self.key,step=step))
            raise
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); record = state['runtime']['operations'][identity]
            record['result'] = deepcopy(result)
            record['status'] = 'confirmed' if record['source'] == source(state['tasks'][self.key]) else 'unknown'
            self.runtime._save(state,dict(op='continuation-effect-result',task_id=self.key,step=step))
            require(record['status'] == 'confirmed', 'task changed during effect; original result retained')
        return result

    def claim_install(self):
        with writer_lock(self.runtime.path):
            state=self.runtime._load();approved=self.authorize(state);task=state['tasks'][self.key]
            require(self.install_allowed and 'update_local_plugin_after_review' in approved.get('authorized',[]) and
                    task['phase']=='completed' and task.get('merge',{}).get('verified'), 'approved merged local update required')
            record=state['runtime']['operations'][self.identity]
            require(record.get('source')==source(task),'update source changed')
            key=self.identity+':installation'
            if key in state['runtime']['operations']:
                return dict(dispatch_allowed=False,operation_id=key,status=state['runtime']['operations'][key]['status'])
            state['runtime']['operations'][key]=dict(operation='continuation-effect',task_id=self.key,request=self.reference,
                status='intent',source=source(task))
            self.runtime._save(state,dict(op='continuation-install-intent',task_id=self.key))
            return dict(dispatch_allowed=True,plan=dict(task_id=self.key,operation_id=key,merge=task['merge'],approval=self.request['approval']))

    def advance(self, inputs):
        state, task = self.pin()
        if task['phase'] != 'completed':
            if task.get('worker_run', {}).get('status') != 'reported':
                result = self.runtime.handoff(self.key,self.actor,available_tools=self.tools,
                    **{key: inputs[key] for key in ('observation_ref','report_ref') if key in inputs})
                if result['status'] != 'received':
                    return self.result('receipt',result['status'],result.get('next_action') or result.get('reason','Author receipt required'),
                        owner=result.get('next_owner'),package=dict(action='observe',plan=result) if result.get('host_call') else None)
                self.mark('receipt',result); return
            self.mark('receipt',task['delivery'])
            if not task.get('independent_quality'):
                if not inputs.get('review'):
                    return self.result('review','waiting_review','Independent source-bound review required')
                result = self.runtime.real_use(dict(action='quality',task_id=self.key,approval=self.request['approval'],review=inputs['review']),self.actor)
                self.mark('review',result); return
            from real_use_closeout import quality, code_acceptance
            quality(state,self.key); self.mark('review',task['independent_quality'])
            if not task.get('technical_acceptance') and task.get('user_acceptance') != 'accepted':
                if not inputs.get('acceptance_authority'):
                    human = task.get('acceptance_type') == 'user_required' and task.get('acceptance_policy_explicit')
                    return self.result('acceptance','waiting_acceptance','Existing acceptance gate requires evidence',
                        owner=state['coordinator_id'] if human else self.actor)
                result = self.runtime.real_use(dict(action='technical-acceptance',task_id=self.key,
                    approval=self.request['approval'],authority=inputs['acceptance_authority']),self.actor)
                self.mark('acceptance',result); return
            code_acceptance(state,self.key); self.mark('acceptance',dict(status='accepted'))
            if not inputs.get('merge'):
                return self.result('merge','waiting_merge_fact','Integrator must supply verified actual PR/head/base/main inclusion; no merge is issued here')
            command = load_bound(inputs['merge'])
            require(command.get('action') == 'record-merge' and command.get('task_id') == self.key and
                    command.get('approval') == self.request['approval'] and command.get('scope') == self.scope,
                    'actual merge command must bind original task, approval and scope')
            result = self.runtime.real_use(command,self.actor)
            self.mark('merge',result); return
        require(task.get('merge',{}).get('verified'), 'completed code task needs verified merge')
        self.mark('merge',task['merge'])
        if self.request.get('installation_required'):
            approved = self.authorize(state)
            if not self.install_allowed or 'update_local_plugin_after_review' not in approved.get('authorized',[]):
                return self.result('installation','authorization_required','Approved task-specific local update authority required')
            proof = inputs.get('installation_proof')
            if proof:
                self.runtime.feedback(dict(op='record-installation',task_id=self.key,proof=proof),self.actor)
                result = self.effect('installation',lambda: dict(status='verified',proof=proof),observed=dict(status='verified',proof=proof))
                # A supplied real observation is also sufficient before an update intent was needed.
                if result is None: result = dict(status='verified',proof=proof)
            else:
                prior = state['runtime']['operations'].get(self.identity+':installation')
                if prior and prior['status'] == 'confirmed': result = prior['result']
                elif not callable(self.install):
                    return self.result('installation','host_required','Use the existing trusted updater, then return its installation proof',
                        package=dict(action='installation',operation_id=self.identity+':installation'))
                else:
                    def update():
                        actual = self.install(dict(task_id=self.key,merge=task['merge'],operation_id=self.identity+':installation'))
                        artifact(actual)
                        self.runtime.feedback(dict(op='record-installation',task_id=self.key,proof=actual),self.actor)
                        return dict(status='verified',proof=actual)
                    result = self.effect('installation',update)
            if result is None: return self.result('installation','needs_reconcile','Inspect the original update intent; never reinstall after unknown')
            artifact(result['proof'])
            self.mark('installation',result)
        if not self.reclaim:
            return self.result('preservation','authorization_required','Current task reclamation authorization required')
        try:
            self.runtime._resource_actor(state,self.actor,self.key)
        except ValueError as error:
            return self.result('preservation','host_required',str(error),owner=state['coordinator_id'],
                package=dict(action='continue',required_actor=state['coordinator_id']))
        preservation = task.get('preservation') or inputs.get('preservation')
        if preservation:
            from cleanup import eligibility
            executor = state['runtime']['executors'][task['owner_agent_id']]
            pending = next((key for key,o in state['runtime']['operations'].items() if o.get('operation') == 'close' and
                            o.get('task_id') == self.key and o.get('status') in ('intent','unknown')),None)
            eligibility(self.runtime,state,self.key,preservation,self.scope,closing_operation=pending,resource_paths=False)
            self.effect('preservation',lambda: preservation,observed=preservation)
        else:
            if not self.bridge or not self.request.get('handoff') or not self.request.get('preservation_destination'):
                return self.result('preservation','evidence_required','External destination, handoff and RESOURCE bridge required')
            def preserve():
                remote = inputs.get('remote_verification')
                from evidence import io_path
                marker=io_path(Path(self.request['preservation_destination'])/'preservation-intent.json')
                if self.scope == 'host_verified' and not remote and not marker.exists():
                    remote = self.bridge.observe_remote_completion(self.key,self.actor,scope=self.scope)
                return self.runtime.preserve_task(self.key,self.request['preservation_destination'],self.request['handoff'],
                    self.actor,scope=self.scope,remote_verification=remote,operation_id=self.identity+':preservation')
            previous=state['runtime']['operations'].get(self.identity+':preservation')
            if previous and previous['status']=='intent':
                from call_recovery import dead
                if not dead(previous.get('owner_pid')):
                    return self.result('preservation','needs_reconcile','Original preservation caller is alive/unknown; no concurrent copy')
            # The existing copy operation verifies every partial byte before continuing its own directory.
            observed=preserve() if previous and previous['status'] in ('intent','unknown') else None
            preservation = self.effect('preservation',preserve,observed=observed)
            if not preservation:
                return self.result('preservation','needs_reconcile','Inspect original preservation intent and directory; no new copy attempt')
        self.mark('preservation',preservation)
        state = self.runtime._load(); executor = state['runtime']['executors'][task['owner_agent_id']]
        known=[(key,op) for key,op in state['runtime']['operations'].items() if op.get('task_id')==self.key and
            op.get('operation') in ('spawn','send','resume') and op.get('request_id') and op.get('permit_id')]
        capacity=self.bridge.controller.read()[0].get('capacity_requests',{}) if self.bridge and known else {}
        for attempt,launch in known:
            actual=capacity.get(launch['request_id'],{})
            if not actual or actual.get('permit_id')!=launch['permit_id'] or not all(actual.get('spec',{}).get(k)==launch.get(k)
                    for k in ('task_id','attempt_id','launch_identity')):
                return self.result('close','needs_reconcile','Known local permit RESOURCE identity is missing or changed; retain original operation')
            if actual.get('status')=='released':continue
            if (state['tasks'][self.key]['worker_run'].get('attempt_id')!=attempt or launch.get('executor_id')!=executor['id'] or
                launch.get('generation',executor['generation'])!=executor['generation'] or
                launch.get('status') in ('not_started','capacity','unsupported','queued')):
                return self.result('close','needs_reconcile','Known local permit has no verified current actor/attempt mapping; retain it, do not close/release or report not_reserved')
        closes = [(key,o) for key,o in state['runtime']['operations'].items() if o.get('operation') == 'close' and
            o.get('task_id') == self.key and o.get('executor_id') == executor['id'] and o.get('generation') == executor['generation']]
        require(len(closes) <= 1, 'multiple close intents require reconciliation')
        close_id, closed = closes[0] if closes else (self.identity+':close', None)
        if closed is None:
            closed = self.runtime.prepare_native_close(self.key,close_id,preservation,self.actor,authorized=True,scope=self.scope)
        if closed['status'] != 'closed':
            if inputs.get('native_observation'):
                closed = self.runtime.reconcile_lifecycle(close_id,inputs['native_observation'],self.actor)
                if closed['status'] == 'closed': return
            if not closed.get('native_request'):
                return self.result('close','needs_reconcile','Observe original lifecycle operation; never issue another close')
            return self.result('close','host_required','Claim original native close once, retain raw close/wait results and return confirmation',
                owner=state['coordinator_id'],package=dict(action='native_close',operation_id=close_id,
                    native_request=closed['native_request'],required_actor=state['coordinator_id']))
        self.mark('close',dict(operation_id=close_id,status='closed'))
        if closed.get('request_id') and closed.get('permit_id'):
            if not self.bridge: return self.result('release','capability_unavailable','RESOURCE bridge required to release the exact original permit')
            capacity = self.bridge.controller.read()[0]['capacity_requests'][closed['request_id']]
            require(capacity['permit_id']==closed['permit_id'] and all(capacity['spec'].get(k)==closed.get(k)
                    for k in ('task_id','attempt_id','launch_identity')), 'original permit context changed')
            observed=None
            if capacity['status']=='released':
                require(capacity.get('release_receipt')==closed.get('release_receipt') and
                    self.runtime.verify_release_receipt(dict(capacity['spec'],permit_id=capacity['permit_id']),capacity['release_receipt'])['verified'],
                    'actual original release receipt required')
                observed=dict(status='released',request_id=closed['request_id'],permit_id=closed['permit_id'],receipt=capacity['release_receipt'])
            released = self.effect('release',lambda: self.bridge.release_agent_quota(close_id,self.actor),observed=observed)
            if released is None: return self.result('release','needs_reconcile','Observe original permit release before continuing')
        else:
            require(closed.get('close_confirmation') and self.runtime.verify_close_confirmation(closed['close_confirmation'])['verified'],
                    'actual native closure confirmation required')
            released = dict(status='not_reserved',platform_capacity_count_known=False)
        self.mark('release',released)
        # Continue the one explicitly approved original request even when file cleanup is still pending.
        successor_pending=None
        if self.request.get('successor'):
            successor = load_bound(self.request['successor'])
            from codex_parallel_coordination.dispatch import approved_request
            if 'spec_check' in successor: successor = approved_request(successor)
            require(successor['task_id'] != self.key and successor['project_id'] == self.runtime.project_id,
                    'same-project original successor required')
            operation=self.runtime._load()['runtime']['operations'].get(successor['request_id'],{})
            already_dispatched=(operation.get('operation')=='standard-dispatch' and
                operation.get('request_digest')==fingerprint(successor) and
                operation.get('handoff',{}).get('status')=='accepted')
            if already_dispatched:
                self.mark('successor',dict(request=self.request['successor'],status='dispatched'))
            elif not self.successor_allowed or not self.origin or self.origin.get('role') != 'user' or \
                    self.origin.get('conversation_id') != state['coordinator_id'] or self.origin.get('reference') != successor['approval']:
                successor_pending=self.result('successor','authorization_required','Explicit original-main approval to continue this exact request required')
            elif not self.dispatch or self.actor != state['coordinator_id']:
                successor_pending=self.result('successor','host_required','Continue original StandardDispatch request with unchanged approval and identity',
                    owner=state['coordinator_id'],package=dict(action='dispatch',successor=self.request['successor'],required_actor=state['coordinator_id']))
            else:
                dispatched = self.dispatch.run(successor)
                if dispatched['status'] != 'dispatched':
                    successor_pending=self.result('successor',dispatched['status'],dispatched.get('record',{}).get('next_action') or 'Original successor admission/baseline/host gates still apply',
                        package=dict(action='dispatch',successor=self.request['successor'],required_actor=state['coordinator_id']))
                else:self.mark('successor',dict(request=self.request['successor'],result=dispatched))
        if not self.bridge: return self.result('cleanup','capability_unavailable','RESOURCE cleanup capability required')
        resources = [r for r in self.runtime._load()['runtime'].get('resources',{}).values() if r['task_id'] == self.key]
        for resource in resources:
            if resource.get('cleanup_status') in ('intent','unknown'):
                self.bridge.reconcile_cleanup(self.key,resource['id'],self.actor)
        result = self.runtime.cleanup_task(self.key,self.identity+':files',preservation,self.bridge.cleanup,
                                          self.actor,authorized=True,scope=self.scope)
        if result['status'] != 'cleanup_completed':
            return dict(successor_pending,cleanup=self.runtime._load()['tasks'][self.key]['cleanup']) if successor_pending else \
                self.result('cleanup','pending_cleanup','Native close and permit release confirmed; reconcile original file cleanup separately')
        self.mark('cleanup',result)
        if successor_pending:
            return self.result('successor',successor_pending['status'],successor_pending['reason'],
                owner=successor_pending['next_owner'],package=successor_pending.get('host_package'))
        return self.result('complete','completed','Delivery and explicitly approved continuation confirmed')


def run(runtime, request_ref, actor_id, *, inputs=None, max_steps=8, **options):
    require(type(max_steps) is int and 1 <= max_steps <= 16, 'bounded step limit (1..16) required')
    flow = Continuation(runtime,request_ref,actor_id,**options)
    flow.bind()
    for _ in range(max_steps):
        try:
            result = flow.advance(inputs or {})
            if result is not None: return result
        except ERRORS as error:
            return flow.result('reconcile','blocked',str(error))
    return flow.result('continue','bounded','Continue the same request from current canonical facts')
