"""Project-keyed public entrypoint over the frozen single-ledger kernel (stdlib)."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

from coordination import (new_state, validate, transition, writer_lock, summary,
                          checkpoint, next_item, presentation, receipt_context, receipt_report)
from evidence import load, write_atomic, ref, artifact, load_bound, require, fingerprint, io_path
from progress import round_handoff
from reconcile import closeout


class Runtime:
    def __init__(self, state_root, project_root):
        self.project_root = Path(project_root).resolve()
        require(self.project_root.is_dir(), 'existing project root required')
        identity = os.path.normcase(str(self.project_root))
        self.project_id = 'project-'+hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.directory = Path(state_root).resolve()/'projects'/self.project_id
        require(not self.directory.is_relative_to(self.project_root), 'ledger must be outside project worktree')
        self.path = self.directory/'ledger.json'

    def initialize(self, coordinator_id, *, worker_capacity=4, provider='github'):
        with writer_lock(self.path):
            require(not self.path.exists(), 'project already initialized; use recover')
            state = new_state(coordinator_id, self.project_id)
            require(provider in ('github','gitlab'),'supported project provider required')
            state['project'].update(root=str(self.project_root), worker_capacity=worker_capacity,provider=provider)
            state['runtime'] = dict(version=1, idle_wakeup=False, executors={}, operations={},
                cleanup_root=str(self.directory/'resources'))
            validate(state)
            write_atomic(self.path, state)
        return self.recover()

    def _load(self):
        state = load(self.path)
        validate(state)
        require(state['project']['id'] == self.project_id and
                Path(state['project']['root']).resolve() == self.project_root, 'project identity mismatch')
        require(state.get('runtime', {}).get('idle_wakeup') is False, 'unsupported runtime configuration')
        return state

    def recover(self):
        state = self._load()  # Never rebuild, reset, migrate or silently steal a lock.
        return dict(ledger=str(self.path), project_id=self.project_id, revision=state['revision'],
                    summary=summary(state), handoff=round_handoff(state, checkpoint(state)), idle_wakeup=False)

    def _actor(self, state, actor, *, control=False):
        expected = state['coordinator_id'] if control else state.get('delivery', {}).get('agent_id', state['coordinator_id'])
        require(actor == expected, 'current main/delivery role required; developers do not write the task ledger')

    def _scope(self, state, task_id):
        require(not state.get('delivery') or task_id in state['delivery']['task_ids'], 'outside delegated task scope')

    def _resource_actor(self,state,actor_id,task_id):
        from resource_lifecycle import actor
        return actor(state,task_id,actor_id)

    def _save(self, state, operation):
        state['revision'] += 1
        state['last_reconcile'] = None
        state['history'].append(dict(revision=state['revision'], operation=operation))
        validate(state)
        write_atomic(self.path, state)

    def apply(self, operation, actor_id):
        require(operation['op'] not in {'dispatch','start','redispatch','rework','integration-start'},
                'execution must pass the admitted host launch entrypoint')
        with writer_lock(self.path):
            state = self._load()
            from delivery import CONTROL
            self._actor(state, actor_id, control=operation['op'] in CONTROL)
            state, result = transition(state, dict(operation, actor_id=actor_id))
            write_atomic(self.path, state)
            return result

    def prepare(self, spec, actor_id):
        """Queue a real scope before any host side effect; unknown executor is null."""
        with writer_lock(self.path):
            state = self._load(); self._actor(state, actor_id, control=True)
            require(spec.get('task_kind', 'code') == 'code', 'new queries and read-only analysis stay with main; historical tasks remain recoverable')
            from demand_routing import prepare_binding
            binding = prepare_binding(state, spec)
            op = dict(spec, op='register', pending_executor=not spec.get('owner_agent_id'), actor_id=actor_id)
            op.setdefault('owner_agent_id',None)
            state, _ = transition(state, op)
            state, _ = transition(state, dict(op='confirm', task_id=spec['task_id'], actor_id=actor_id,
                **{k:spec[k] for k in ('scope','acceptance','user_reference')}))
            state, result = transition(state, dict(op='queue', task_id=spec['task_id'], actor_id=actor_id,
                reason='Awaiting admitted host execution', user_reference=spec['user_reference']))
            if binding:
                state['tasks'][spec['task_id']]['routing'] = binding
            if spec.get('resource_contract'):
                from resource_lifecycle import managed_path
                require(spec['resource_contract']=='new-task/1','unknown new-task resource contract')
                managed_path(state,spec['worktree'])
                state['tasks'][spec['task_id']]['resource_contract']=spec['resource_contract']
            else:
                # Low-level queueing remains available, but cannot silently claim a managed closed loop.
                state['tasks'][spec['task_id']]['resource_contract']='registration-required/1'
            if spec.get('resource_authority'):
                artifact(spec['resource_authority'])
                state['tasks'][spec['task_id']]['resource_authority']=deepcopy(spec['resource_authority'])
            result=dict(result,resource_registration='required',managed_closed_loop=False)
            write_atomic(self.path, state)
            return result

    def routing(self, command, actor_id, *, user_origin=None):
        """Public intake mutations share the task ledger, lock and audit history."""
        from demand_routing import apply as route, intake
        with writer_lock(self.path):
            original = self._load()
            # Reject role spoofing before dedup or recording a command.
            if command['op'] == 'dependency':
                require(actor_id == original['tasks'][command['source_task']]['owner_agent_id'], 'source developer required')
            else:
                self._actor(original, actor_id, control=command['op'] != 'verify-dependency')
            require(command.get('event_id'), 'stable intake event id required')
            digest = fingerprint(dict(command=command, actor=actor_id, user_origin=user_origin))
            prior = intake(original)['events'].get(command['event_id'])
            if prior:
                require(prior['fingerprint'] == digest, 'conflicting duplicate intake event')
                return deepcopy(prior['result'])
            state = deepcopy(original)
            try:
                result = dict(status='recorded', result=route(state, command, actor_id, user_origin))
            except (ValueError, KeyError, TypeError, OSError) as error:
                state = original
                result = dict(status='rejected', reason=str(error))
            intake(state)['events'][command['event_id']] = dict(fingerprint=digest, command=deepcopy(command),
                actor_id=actor_id, result=deepcopy(result))
            self._save(state, dict(op='routing', event_id=command['event_id'], status=result['status']))
            return result

    def routing_status(self):
        """Read current routing evidence and canonical task facts without another queue."""
        from demand_routing import task_version
        state = self._load()
        return dict(revision=state['revision'], intake=deepcopy(state['runtime'].get('intake', {})),
            observed_dispatches=deepcopy(state['runtime'].get('observed_dispatches', {})),
            tasks={key: {field: deepcopy(task.get(field)) for field in
                ('phase', 'owner_agent_id', 'worktree', 'branch', 'requirements', 'authorization', 'mr', 'worker_run', 'routing')}
                for key, task in state['tasks'].items()},
            target_versions={key: task_version(state, key) for key in state['tasks']})

    def bind_observed_dispatch(self, task_id, proof_ref, actor_id):
        """Register a witnessed external spawn without calling any host tool."""
        from demand_routing import bind_observed
        with writer_lock(self.path):
            state = self._load()
            result = bind_observed(state, task_id, proof_ref, actor_id)
            if not result.get('duplicate'):
                if state['tasks'][task_id].get('resource_contract')=='new-task/1':
                    from resource_lifecycle import inventory, agent
                    inventory(state,task_id,author=False)
                    agent(state,task_id,proof_ref)
                self._save(state, dict(op='bind-observed-dispatch', task_id=task_id, proof=proof_ref))
            return dict(result, revision=state['revision'])

    def receive_observed_delivery(self, payload, observation_ref, actor_id):
        """Use approved task-only receipt authority without changing global delivery roles."""
        from demand_routing import receive_observed
        with writer_lock(self.path):
            state = self._load()
            result = receive_observed(state, payload, observation_ref, actor_id)
            if not result['duplicate']:
                self._save(state, dict(op='receive-observed-delivery', task_id=payload['task_id'],
                    event_id=payload['event_id'], actor_id=actor_id, observation=observation_ref))
            return dict(result, revision=state['revision'])

    def bind_executor(self, executor_id, routes, actor_id, authority_ref, *, task_id=None):
        artifact(authority_ref)
        require(executor_id and routes and set(routes) <= {'native_agent','app_thread'}, 'actual executor routes required')
        for route in routes.values():
            require(isinstance(route,dict) and all(isinstance(route.get(k),str) and route[k].strip()
                for k in ('target_id','host_id')), 'actual host/target identity required')
        with writer_lock(self.path):
            state = self._load(); self._actor(state, actor_id, control=True)
            if task_id is not None:
                from demand_routing import bind_guard
                bind_guard(state, task_id, executor_id)
            for executor in state['runtime']['executors'].values():
                for channel, route in routes.items():
                    require(executor['id'] == executor_id or not any(old_channel==channel and
                        old.get('host_id')==route['host_id'] and old.get('target_id')==route['target_id']
                        for old_channel,old in executor['routes'].items()), 'route already owned')
            old = state['runtime']['executors'].get(executor_id)
            require(not old or old['routes'] == routes, 'do not silently rebind an existing executor')
            if not old:
                state['runtime']['executors'][executor_id] = dict(id=executor_id, routes=deepcopy(routes),
                    generation=1, lease='open', execution='unknown', active_children=None, pending_work=None,
                    authority=authority_ref, observation=None)
            if task_id is not None:
                state['tasks'][task_id]['owner_agent_id'] = executor_id
                state['tasks'][task_id]['worker_run']['owner_agent_id'] = executor_id
                if state['tasks'][task_id].get('resource_contract')=='new-task/1':
                    from resource_lifecycle import agent
                    agent(state,task_id,authority_ref)
            if not old or task_id is not None:
                self._save(state, dict(op='bind-executor', executor_id=executor_id))

    def launch(self, task_id, adapter, *, attempt_id, admission, authorized, message, spawn=False, executor_id=None, route_id=None):
        """One bounded host attempt, no auto close and no automatic side-effect retry."""
        with writer_lock(self.path):
            state = self._load(); self._actor(state, adapter.actor_id)
            self._scope(state,task_id)
            task = state['tasks'][task_id]
            require(authorized is True and attempt_id, 'scoped launch authorization required')
            if task.get('resource_contract')=='new-task/1':
                from resource_lifecycle import inventory
                inventory(state,task_id,author=False)
            operations = state['runtime']['operations']
            if attempt_id in operations:
                require(operations[attempt_id]['task_id'] == task_id, 'attempt belongs to another task')
                require(operations[attempt_id].get('route_id') == route_id, 'attempt belongs to another routing decision')
                return self._launch_result(state,operations[attempt_id])
            require(task['requirements'] and task['worker_run']['status'] == 'queued', 'task must be queued')
            require(not any(o.get('task_id')==task_id and o.get('operation') in ('spawn','send') and
                    o['status'] in ('intent','unknown','accepted_unconfirmed') for o in operations.values()),
                    'previous host effect uncertain; reconcile before another attempt')
            # RESOURCE owns permit truth; caller passes its granted receipt, not a task stage.
            permit = load_bound(admission)
            require(permit.get('project_id') == self.project_id and permit.get('task_id') == task_id and
                    permit.get('attempt_id') == attempt_id and permit.get('status') == 'granted' and
                    permit.get('request_id') and permit.get('permit_id'), 'fresh resource admission required')
            require(permit.get('launch_identity')==(('spawn:'+attempt_id) if spawn else executor_id),
                    'resource admission belongs to another launch identity')
            running = [t for t in state['tasks'].values() if t.get('worker_run',{}).get('status') == 'running']
            executor = None
            if not spawn:
                executor = state['runtime']['executors'][executor_id]
                artifact(executor['authority'])
                observed = load_bound(executor['observation'])
                require(observed.get('executor_id')==executor_id and observed.get('generation')==executor['generation'] and
                    all(observed.get(k)==executor[k] for k in ('execution','active_children','pending_work')),
                    'bound executor observation differs from current registered facts')
                require(task['owner_agent_id'] in (None,executor_id), 'preserve the original responsible executor')
                require(not any(key!=task_id and other['owner_agent_id']==executor_id and other['phase']!='completed'
                    for key,other in state['tasks'].items()), 'executor still owns another unfinished task')
                require(executor['lease'] == 'open' and executor['execution'] in ('completed','idle'),
                        'reuse requires an observed non-running open executor')
                require(executor['active_children'] == [] and executor['pending_work'] is False,
                        'executor has uncompleted actions or children')
                require(not any(t['owner_agent_id'] == executor_id for t in running), 'executor already executing')
            from demand_routing import launch_guard
            route = launch_guard(state, task_id, route_id, spawn=spawn, executor_id=executor_id, message=message)
            record = dict(operation='spawn' if spawn else 'send', task_id=task_id, attempt_id=attempt_id,
                request_id=permit['request_id'], permit_id=permit['permit_id'], admission=admission,
                status='intent', executor_id=executor_id,launch_identity=permit['launch_identity'],route_id=route_id)
            operations[attempt_id] = record
            if not adapter.capabilities()['spawn' if spawn else 'existing_message']:
                record.update(status='unsupported',reason='required authorized host capability unavailable')
                self._save(state,dict(op='launch-not-started',task_id=task_id))
                return self._launch_result(state,record)
            if len(running) >= state['project']['worker_capacity']:
                record.update(status='queued', reason='project_worker_capacity')
                self._save(state, dict(op='launch-queued', task_id=task_id))
                return self._launch_result(state, record)
            target = None if spawn else executor['routes'][adapter.channel]['target_id']
            if spawn:
                require(task['owner_agent_id'] is None,'existing task owner cannot be replaced by a new spawn')
            if route is not None:
                route['attempt_id'] = attempt_id
            # Persist before invoking a tool. A crash leaves intent/unknown, never a second send.
            self._save(state, dict(op='launch-intent', task_id=task_id, attempt_id=attempt_id))
            try:
                outcome = adapter.call('spawn' if spawn else 'send', target=target, message=message, authorized=authorized)
            except Exception as error:
                outcome = dict(status='unknown', error_type=type(error).__name__)
            record.update(outcome)
            if outcome['status'] == 'accepted':
                if spawn:
                    executor_id = adapter.channel+':'+outcome['target_id']
                    require(executor_id not in state['runtime']['executors'], 'spawn reused an already registered host ID')
                    executor = dict(id=executor_id, routes={adapter.channel:dict(target_id=outcome['target_id'],host_id='current')},
                        generation=1, lease='open', execution='unknown', active_children=None, pending_work=None,
                        authority=admission, observation=None)
                    state['runtime']['executors'][executor_id] = executor
                task['owner_agent_id'] = executor_id
                task['worker_run']['owner_agent_id'] = executor_id
                task['worker_run']['attempt_id'] = attempt_id
                record['executor_id'] = executor_id
                if task.get('resource_contract')=='new-task/1':
                    from resource_lifecycle import agent
                    agent(state,task_id,admission)
                executor.update(active_children=None,pending_work=None,observation=None)
                try:
                    observed = adapter.call('observe', target=executor['routes'][adapter.channel]['target_id'], authorized=True)
                    executor['execution'] = observed.get('execution','unknown')
                    record['observation'] = observed
                    if executor['execution'] == 'running':
                        # Native authorization was already checked; preserve task rights and scope.
                        task.update(phase='developing', implementation='running')
                        task['worker_run'].update(status='running', attempt_id=attempt_id)
                        record['status'] = 'running'
                    else:
                        record.update(status='accepted_unconfirmed', reason='message accepted; execution not observed running')
                except Exception as error:
                    record.update(status='accepted_unconfirmed', error_type=type(error).__name__)
            if record['status'] != 'running':
                task['worker_run']['reason'] = record.get('reason', record['status'])
            self._save(state, dict(op='launch-outcome', task_id=task_id, attempt_id=attempt_id))
            return self._launch_result(state, record)

    def _launch_result(self, state, record):
        return dict(deepcopy(record), revision=state['revision'],
            public_progress='已实际开始执行。' if record['status']=='running' else
                '任务仍排队，尚未确认开始执行；'+record.get('reason',record['status']),
            slot_released=False)

    def reconcile_launch(self, operation_id, observation_ref, actor_id):
        facts=load_bound(observation_ref)
        with writer_lock(self.path):
            state=self._load();self._actor(state,actor_id)
            record=state['runtime']['operations'][operation_id];self._scope(state,record['task_id'])
            require(record['operation'] in ('spawn','send') and record['status'] in ('intent','unknown','accepted_unconfirmed'),
                    'launch does not need reconciliation')
            require(facts.get('project_id')==self.project_id and facts.get('task_id')==record['task_id'] and
                    facts.get('operation_id')==operation_id and facts.get('status')=='not_started' and facts.get('evidence'),
                    'explicit no-execution host observation required')
            for item in facts['evidence']:artifact(item)
            record.setdefault('observations',[]).append(observation_ref)
            record.update(status='not_started',reason='host observation confirms no execution')
            self._save(state,dict(op='reconcile-launch',operation_id=operation_id))
            return self._launch_result(state,record)

    def observe_executor(self, executor_id, facts, actor_id, evidence_ref, *, task_id=None):
        raw = load_bound(evidence_ref)
        require(raw == facts, 'observation differs from bound host facts')
        require(facts.get('execution') in ('running','completed','idle','unknown') and
                isinstance(facts.get('active_children'),list) and type(facts.get('pending_work')) is bool,
                'explicit execution/children/pending-work observation required')
        with writer_lock(self.path):
            state=self._load()
            if task_id is None:self._actor(state,actor_id)
            else:
                self._resource_actor(state,actor_id,task_id);self._scope(state,task_id)
                require(state['tasks'][task_id]['owner_agent_id']==executor_id,'task executor differs')
            executor=state['runtime']['executors'][executor_id]
            require(facts.get('executor_id')==executor_id and facts.get('generation')==executor['generation'], 'stale executor observation')
            require(facts.get('based_on_revision')==state['revision'],'executor observation predates ledger changes')
            executor.update({k:deepcopy(facts[k]) for k in ('execution','active_children','pending_work')})
            executor['observation']=evidence_ref
            self._save(state,dict(op='observe-executor',executor_id=executor_id))

    def cycle(self, observation_ref, actor_id):
        with writer_lock(self.path):
            state=self._load(); self._actor(state,actor_id)
            state,result=closeout(state,load_bound(observation_ref),observation_ref,actor_id=actor_id)
            write_atomic(self.path,state)
            return dict(result,summary=summary(state),ledger=str(self.path),idle_wakeup=False)

    def receive(self, payload, actor_id):
        """Cross-channel duplicate facts retain the original canonical provenance."""
        with writer_lock(self.path):
            state=self._load();self._actor(state,actor_id)
            old=state['receipts'].get(payload['event_id'])
            if old and old['payload'].get('channel')!=payload['channel']:
                receipt_context(state,payload);receipt_report(state,payload)
                require(all(old['payload'].get(k)==payload.get(k) for k in
                    ('project_id','task_id','owner_agent_id','recipient_agent_id','delivery_revision','kind','report')),
                    'conflicting duplicate final report')
                arrivals=state['runtime'].setdefault('alternate_receipts',{})
                key=fingerprint(payload)
                if key not in arrivals:
                    arrivals[key]=deepcopy(payload)
                    self._save(state,dict(op='cross-channel-duplicate',event_id=payload['event_id']))
                return dict(receipt='duplicate',revision=state['revision'])
            state,result=transition(state,dict(op='receive',receipt=payload,actor_id=actor_id))
            write_atomic(self.path,state);return result

    def resolve_and_next(self, resolution, actor_id):
        with writer_lock(self.path):
            state=self._load();self._actor(state,actor_id,control=True)
            state,_=transition(state,dict(resolution,op='close',actor_id=actor_id))
            candidate=next_item(state)
            if candidate:
                state,_=transition(state,dict(op='next',actor_id=actor_id))
            write_atomic(self.path,state)
            return dict(revision=state['revision'],summary=summary(state),
                next= presentation(state,state['items'].get(state['active_human_item'])),
                delivered_to_user=False, progress_fresh=False)

    def record_main_inclusion(self, proof_ref, actor_id):
        """Record independently observed actual merge; never execute a remote merge."""
        proof=load_bound(proof_ref)
        with writer_lock(self.path):
            state=self._load();self._actor(state,actor_id)
            key=proof['task_id'];self._scope(state,key)
            task=state['tasks'][key]
            from task_completion import definition
            require(definition(task)[0]=='code','read-only tasks use the independent completion entry')
            from delivery import merge_authority
            merge_authority(state,key,actor_id,for_merge=True)
            from evidence import report_for
            report=report_for(state,key)
            from real_use_closeout import code_acceptance
            code_acceptance(state,key)
            require(task['phase']=='awaiting_merge' and
                    task['integration']['status']=='passed' and task.get('review'),'accepted, integrated, independently reviewed task required')
            require(all(task['authorization'][r]['allowed'] for r in ('commit','push','mr','merge')),'actual merge authorization required')
            require(proof.get('project_id')==self.project_id and proof.get('based_on_revision')==state['revision'] and
                    proof.get('source_head')==task['mr']['head_sha'] and proof.get('previous_main')==task['mr']['target_sha'] and
                    proof.get('main_ref')==state['project']['main_branch'] and proof.get('main_head') and
                    proof.get('contains_source') is True and proof.get('provider')==state['project']['provider'] and
                    proof.get('scope') in ('host_verified','fixture_only') and proof.get('evidence'), 'actual target main inclusion proof mismatch')
            require(task['review']['reviewer_id']==actor_id,'delivery independent review required')
            require(all(task['review'].get(k)==value for k,value in dict(source_digest=report['source']['digest'],
                    head_sha=task['mr']['head_sha'],target_sha=task['mr']['target_sha']).items()),'independent review source binding stale')
            integrated=load_bound(task['integration']['report'])
            require(integrated.get('head_sha')==task['mr']['head_sha'] and integrated.get('target_sha')==task['mr']['target_sha']
                    and integrated.get('source_digest')==report['source']['digest'],'integration binding stale')
            for item in integrated['evidence']:artifact(item)
            for item in proof['evidence']:artifact(item)
            task.update(phase='completed',merge=dict(verified=True,head_sha=proof['source_head'],target_sha=proof['previous_main'],
                main_sha=proof['main_head'],scope=proof['scope'],proof=proof_ref))
            task['cleanup']=dict(status='pending_cleanup',owner=actor_id,next_action='preserve_and_reclaim')
            self._save(state,dict(op='record-main-inclusion',task_id=key,proof=proof_ref))
            return dict(revision=state['revision'],task_id=key,status='completed',cleanup_authorized=False,
                        cleanup_required=True,next_action='preserve_and_reclaim')

    def complete_read_only(self,proof_ref,actor_id):
        from task_completion import finish
        return finish(self,proof_ref,actor_id)

    def real_use(self,command,actor_id):
        from real_use_closeout import apply
        return apply(self,command,actor_id)

    def bind_development(self,task_id,spec_ref,actor_id,**options):
        from development_lifecycle import bind
        return bind(self,task_id,spec_ref,actor_id,**options)

    def cancel_task(self,proof_ref,actor_id):
        from development_lifecycle import cancel
        return cancel(self,proof_ref,actor_id)

    def close_cancelled_pr(self,task_id,operation_id,preservation_ref,invoke,actor_id,**options):
        from development_lifecycle import close_pr
        return close_pr(self,task_id,operation_id,preservation_ref,invoke,actor_id,**options)

    def reconcile_cancelled_pr(self,task_id,observation_ref,actor_id):
        from development_lifecycle import reconcile_pr
        return reconcile_pr(self,task_id,observation_ref,actor_id)

    def reclaim_development(self,task_id,operation_id,preservation_ref,adapter,invoke,*,
                            close_pr=None,authorized=False):
        """Finite post-end continuation; host supplies preservation and current authority."""
        require(authorized is True and operation_id,'current exact reclamation authorization required')
        state=self._load();self._resource_actor(state,adapter.actor_id,task_id);self._scope(state,task_id)
        task=state['tasks'][task_id]
        if task['phase']=='cancelled':
            pr=self.close_cancelled_pr(task_id,operation_id+':pr',preservation_ref,
                close_pr or (lambda _:dict(status='failed',reason='PR close capability unavailable')),
                adapter.actor_id,authorized=True,scope=adapter.scope)
            if pr.get('status',pr.get('pr_status')) not in ('closed','not_required'):
                return dict(status='pending_cleanup',owner=adapter.actor_id,next_action='reconcile_or_close_pr')
        from cleanup import eligibility
        eligibility(self,self._load(),task_id,preservation_ref,adapter.scope)
        executor=self._load()['runtime']['executors'][task['owner_agent_id']]
        if executor['lease']!='closed':
            closed=self.lifecycle('close',task['owner_agent_id'],task_id,operation_id+':agent',executor['generation'],
                                  adapter,preservation_ref=preservation_ref,authorized=True)
            if closed['status']!='closed':
                return dict(status='pending_cleanup',owner=adapter.actor_id,next_action='reconcile_or_close_agent',outcome=closed)
        else:
            closed=next(o for o in self._load()['runtime']['operations'].values() if o.get('operation')=='close' and
                o.get('task_id')==task_id and o.get('executor_id')==task['owner_agent_id'] and
                o.get('generation')==executor['generation'] and o.get('status')=='closed')
        receipt=self.release_receipt(closed['operation_id'],adapter.actor_id)
        result=self.cleanup_task(task_id,operation_id+':files',preservation_ref,invoke,adapter.actor_id,
                                 authorized=True,scope=adapter.scope)
        return dict(result,release_receipt=receipt)

    def register_resource(self,task_id,resource,authority_ref,actor_id):
        from cleanup import register
        return register(self,task_id,resource,authority_ref,actor_id)

    def preserve_task(self,task_id,destination,handoff_ref,actor_id,**options):
        from resource_lifecycle import preserve
        return preserve(self,task_id,destination,handoff_ref,actor_id,**options)

    def cleanup_task(self,task_id,operation_id,preservation_ref,invoke,actor_id,**options):
        from cleanup import run
        return run(self,task_id,operation_id,preservation_ref,invoke,actor_id,**options)

    def reconcile_cleanup(self,task_id,resource_id,observation_ref,actor_id):
        from cleanup import reconcile
        return reconcile(self,task_id,resource_id,observation_ref,actor_id)

    def verify_cleanup_receipt(self,context,proof):
        from cleanup import verify
        return verify(self,context,proof)

    @contextmanager
    def lifecycle_guard(self, executor_id, task_id, attempt_id, generation, actor_id, preservation_ref, *, scope='fixture_only'):
        """Hold the canonical lock through the finite close/observe callback."""
        with writer_lock(self.path):
            state=self._load();self._resource_actor(state,actor_id,task_id)
            self._scope(state,task_id)
            executor=state['runtime']['executors'][executor_id]
            task=state['tasks'][task_id]
            from cleanup import eligibility
            eligibility(self,state,task_id,preservation_ref,scope)
            require(task['owner_agent_id']==executor_id and executor['generation']==generation,'stale lifecycle target')
            require(executor['lease']=='open','lease needs reconciliation or is already closed')
            ticket=dict(project_id=self.project_id,task_id=task_id,attempt_id=attempt_id,executor_id=executor_id,
                generation=generation,revision=state['revision'],delivery_preserved=True,delivered=True,
                no_running_children=True,no_pending_work=True,protected=False,evidence=[preservation_ref,executor['observation']],outcome=None)
            yield ticket

    def lifecycle(self, operation, executor_id, task_id, attempt_id, generation, adapter, *,
                  preservation_ref=None, admission=None, authorized=False):
        require(operation in ('close','resume') and authorized is True,'explicit lifecycle authorization required')
        # One persisted operation ID prevents uncertain retries after a crash.
        with writer_lock(self.path):
            state=self._load()
            resource_authority=self._resource_actor(state,adapter.actor_id,task_id) if operation=='close' else None
            if operation!='close':self._actor(state,adapter.actor_id)
            self._scope(state,task_id)
            existing=state['runtime']['operations'].get(attempt_id)
            if existing:
                require(existing.get('executor_id')==executor_id and existing.get('operation')==operation and
                        existing.get('task_id')==task_id and existing.get('generation')==generation,'operation ID reused')
                actual=state['runtime']['executors'][executor_id]['generation']
                require(actual==generation+(1 if existing['status']=='resumed' else 0),'stale lifecycle generation')
                return deepcopy(existing)
        if operation=='close':
            guard=self.lifecycle_guard(executor_id,task_id,attempt_id,generation,adapter.actor_id,preservation_ref,scope=adapter.scope)
        else:
            guard=self._resume_guard(executor_id,task_id,attempt_id,generation,adapter.actor_id,admission)
        with guard as ticket:
            state=self._load();executor=state['runtime']['executors'][executor_id]
            record=dict(operation=operation,operation_id=attempt_id,executor_id=executor_id,task_id=task_id,attempt_id=attempt_id,
                generation=generation,status='intent',project_id=self.project_id,preservation=preservation_ref,proof_scope=adapter.scope,
                cleanup_authorized=authorized,executor_routes=deepcopy(executor['routes']),executor_authority=executor['authority'])
            if resource_authority:record.update(resource_authority=resource_authority,actor_id=adapter.actor_id)
            if admission:
                permit=load_bound(admission);record.update(request_id=permit['request_id'],permit_id=permit['permit_id'],launch_identity=permit.get('launch_identity'))
            elif task_id in state['tasks']:
                launch=state['runtime']['operations'].get(state['tasks'][task_id].get('worker_run',{}).get('attempt_id'),{})
                record.update({k:launch.get(k) for k in ('request_id','permit_id','attempt_id')})
                if 'launch_identity' in launch:record['launch_identity']=launch['launch_identity']
            state['runtime']['operations'][attempt_id]=record
            executor['lease']='closing' if operation=='close' else 'resuming'
            self._save(state,dict(op=operation+'-intent',executor_id=executor_id))
            try:
                result=(adapter.call(operation,target=executor['routes'][adapter.channel]['target_id'],authorized=True)
                        if adapter.capabilities()[operation] else dict(status='unsupported'))
            except Exception as error: result=dict(status='unknown',error_type=type(error).__name__)
            record.update(result)
            if result['status']=='closed':
                executor.update(lease='closed',execution='unknown')
                if state['tasks'][task_id]['phase']=='cancelled' and state['tasks'][task_id].get('worker_run'):
                    state['tasks'][task_id]['worker_run']['status']='cancelled'
            elif result['status']=='resumed': executor.update(lease='open',generation=generation+1,execution='unknown',observation=None)
            elif result['status']=='unsupported': executor['lease']='open' if operation=='close' else 'closed'
            else: executor['lease']='unknown'
            self._save(state,dict(op=operation+'-outcome',executor_id=executor_id))
            return deepcopy(record)

    def release_receipt(self, operation_id, actor_id):
        """Mint a bounded resource-release fact only from this canonical operation."""
        with writer_lock(self.path):
            state=self._load()
            operation=state['runtime']['operations'][operation_id]
            self._resource_actor(state,actor_id,operation['task_id'])
            self._scope(state,operation['task_id'])
            closed=operation['operation']=='close' and operation['status']=='closed'
            not_started=operation['operation'] in ('spawn','send') and operation['status'] in ('capacity','unsupported','queued','not_started')
            require(closed or not_started,'unknown/failed/accepted/running is not confirmed resource release')
            if closed:
                executor=state['runtime']['executors'][operation['executor_id']]
                require(executor['lease']=='closed' and executor['generation']==operation['generation'],'close receipt is stale after resume')
                require(operation.get('executor_routes')==executor['routes'] and operation.get('executor_authority')==executor['authority'],
                        'close receipt executor identity changed')
            require(operation.get('request_id') and operation.get('permit_id'),'exact resource permit must be retained')
            value={k:operation[k] for k in ('task_id','attempt_id','request_id','permit_id')}
            value.update(project_id=self.project_id,operation_id=operation_id,
                outcome='confirmed_closed' if closed else 'confirmed_not_started')
            if 'launch_identity' in operation:value['launch_identity']=operation['launch_identity']
            path=io_path(self.directory/'receipts'/(fingerprint(value)+'.json'))
            if path.exists(): require(load(path)==value,'immutable release receipt changed')
            else: path.parent.mkdir(exist_ok=True);write_atomic(path,value)
            result=ref(path)
            if operation.get('release_receipt')!=result:
                operation['release_receipt']=result
                self._save(state,dict(op='release-receipt',operation_id=operation_id))
            return result

    def verify_release_receipt(self, context, receipt):
        """Read-only callback under RESOURCE lock; no host action or CORE write."""
        try:
            state=self._load();value=load_bound(receipt) if 'path' in receipt else receipt
            operation=state['runtime']['operations'][value['operation_id']]
            if operation.get('resource_authority'):
                require(self._resource_actor(state,operation['actor_id'],operation['task_id'])==operation['resource_authority'],
                        'task resource authority changed')
            require(operation.get('release_receipt') and load_bound(operation['release_receipt'])==value,'not a canonical release receipt')
            require(all(value.get(k)==context.get(k) for k in ('project_id','task_id','attempt_id','request_id','permit_id')),'resource context differs')
            require(value.get('launch_identity')==context.get('launch_identity'),'launch identity differs')
            if value['outcome']=='confirmed_closed':
                executor=state['runtime']['executors'][operation['executor_id']]
                task=state['tasks'][operation['task_id']]
                require(executor['lease']=='closed' and executor['generation']==operation['generation'] and operation['status']=='closed','not a current confirmed close')
                require(operation.get('executor_routes')==executor['routes'] and
                        operation.get('executor_authority')==executor['authority'],'executor identity changed')
                artifact(executor['authority'])
                from cleanup import eligibility
                from task_completion import completion_flags
                eligibility(self,state,operation['task_id'],operation['preservation'],operation['proof_scope'])
                return dict(completion_flags(state,operation['task_id']),verified=True,preservation_valid=True,no_active_work=True,
                    no_active_descendants=True,no_release_dependencies=True,exclusive_task_resource=True,
                    cleanup_authorized=operation.get('cleanup_authorized') is True,protected=False)
            require(value['outcome']=='confirmed_not_started' and operation['status'] in ('capacity','unsupported','queued','not_started'),'not a confirmed unused admission')
            return dict(verified=True,not_started=True)
        except (ValueError,KeyError,TypeError,OSError):return dict(verified=False)

    def reconcile_lifecycle(self, operation_id, observation_ref, actor_id):
        facts=load_bound(observation_ref)
        with writer_lock(self.path):
            state=self._load()
            record=state['runtime']['operations'][operation_id]
            self._resource_actor(state,actor_id,record['task_id']);self._scope(state,record['task_id'])
            executor=state['runtime']['executors'][record['executor_id']]
            require(record['operation']=='close' and record['status'] in ('intent','unknown') and
                    executor['generation']==record['generation'],'not a current uncertain close')
            require(facts.get('operation_id')==operation_id and facts.get('executor_id')==executor['id'] and
                    facts.get('generation')==executor['generation'] and facts.get('closed') is True and facts.get('evidence'),
                    'actual close confirmation required; idle/completed is insufficient')
            for item in facts['evidence']:artifact(item)
            record.setdefault('observations',[]).append(observation_ref);record['status']='closed'
            executor.update(lease='closed',execution='unknown')
            task=state['tasks'][record['task_id']]
            if task['phase']=='cancelled' and task.get('worker_run'):task['worker_run']['status']='cancelled'
            self._save(state,dict(op='reconcile-close',operation_id=operation_id));return deepcopy(record)

    @contextmanager
    def _resume_guard(self, executor_id, task_id, attempt_id, generation, actor_id, admission):
        with writer_lock(self.path):
            state=self._load();self._actor(state,actor_id)
            executor=state['runtime']['executors'][executor_id]
            require(state['tasks'][task_id]['phase'] not in ('completed','cancelled'),
                    'completed tasks are not resumed; create a new task from current main and link its predecessor')
            require(state['tasks'][task_id]['owner_agent_id']==executor_id and executor['generation']==generation and
                    executor['lease']=='closed','only explicitly closed current executor can resume')
            permit=load_bound(admission)
            require(permit.get('status')=='granted' and permit.get('project_id')==self.project_id and
                    permit.get('task_id')==task_id and permit.get('attempt_id')==attempt_id and permit.get('permit_id') and
                    permit.get('launch_identity')==executor_id,
                    'resume needs fresh resource admission')
            yield dict(project_id=self.project_id,task_id=task_id,attempt_id=attempt_id,executor_id=executor_id)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',required=True);p.add_argument('--project-root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--coordinator',required=True);init.add_argument('--worker-capacity',type=int,default=4)
    sub.add_parser('recover');sub.add_parser('capabilities');sub.add_parser('routing-status')
    for name in ('apply','prepare','cycle','resolve-next','record-main','complete-read-only','routing','real-use'):
        cmd=sub.add_parser(name);cmd.add_argument('input');cmd.add_argument('--actor',required=True)
    args=p.parse_args();runtime=Runtime(args.state_root,args.project_root)
    if args.command=='init': result=runtime.initialize(args.coordinator,worker_capacity=args.worker_capacity)
    elif args.command=='recover':result=runtime.recover()
    elif args.command=='routing-status':result=runtime.routing_status()
    elif args.command=='capabilities':result=dict(idle_wakeup=False,callbacks_required_for_host_side_effects=True,global_install=False)
    elif args.command=='cycle':result=runtime.cycle(ref(args.input),args.actor)
    elif args.command=='record-main':result=runtime.record_main_inclusion(ref(args.input),args.actor)
    elif args.command=='complete-read-only':result=runtime.complete_read_only(ref(args.input),args.actor)
    else:
        method={'apply':runtime.apply,'prepare':runtime.prepare,'resolve-next':runtime.resolve_and_next,'routing':runtime.routing,
                'real-use':runtime.real_use}[args.command]
        result=method(load(args.input),args.actor)
    print(json.dumps(result,ensure_ascii=False))
    if result.get('status') == 'rejected':
        sys.exit(1)


if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(json.dumps(dict(status='rejected',error=str(error)),ensure_ascii=False));sys.exit(1)
