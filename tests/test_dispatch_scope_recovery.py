"""Scoped saves and pre-callback recovery; native actions are fixtures only."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

import test_dispatch
from evidence import fingerprint, write_atomic


class ScopeRecovery(unittest.TestCase):
    def setUp(self):
        self.f = test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def interrupt_intent(self):
        f = self.f
        save = f.runtime._save
        def interrupted(state, operation):
            save(state, operation)
            if operation['op'] == 'launch-intent':
                raise KeyboardInterrupt('fixture interruption after durable intent, before callback')
        with patch.object(f.runtime, '_save', interrupted), self.assertRaises(KeyboardInterrupt):
            f.run_dispatch()
        self.assertEqual(f.calls, [])
        binding = dict(project_id=f.runtime.project_id, ledger=str(f.runtime.path), task_id='A',
            request_id=f.request['request_id'], request_digest=fingerprint(f.request), actor_id=f.actor,
            request_ref=f.json('original-request.json', f.request))
        owner = f.json('external/host-owner-fixture.json', dict(schema='dispatch-writer-owner/1',
            binding=binding, process=dict(pid=os.getpid(), creation='fixture-creation', status='alive')))
        return f.json('external/prehost-fixture.json', dict(schema='dispatch-prehost-terminal/1',
            binding=binding, owner=owner, child_pid=os.getpid(), closed=True, exit_code=71, signal=None,
            host_calls=0, pending=None, result_refs=[], scope='fixture_only'))

    def resume(self, proof):
        # Baseline exercises its real re-entry instead of failing on a missing API.
        method = getattr(self.f.dispatch, 'resume_prehost', None)
        if method is None:
            return self.f.run_dispatch()
        return method(self.f.request, proof, authorized=True)

    def test_all_single_task_dispatch_saves_capture_only_current_task(self):
        import stage_feedback
        scopes = []
        capture = stage_feedback.capture
        def observed(state, task_ids=None):
            scopes.append(deepcopy(task_ids))
            return capture(state, task_ids)
        with patch.object(stage_feedback, 'capture', observed):
            self.assertEqual(self.f.run_dispatch()['status'], 'dispatched')
        self.assertTrue(scopes)
        self.assertTrue(all(scope is not None and set(scope) <= {'A'} for scope in scopes), scopes)

    def test_terminal_zero_host_recovers_original_intent_once(self):
        f = self.f
        proof = self.interrupt_intent()
        before = f.runtime._load()
        permit = before['runtime']['operations']['dispatch-A:launch']['permit_id']
        with patch('dispatch_lock.process_identity', return_value=dict(pid=os.getpid(), creation='fixture-creation', status='dead')):
            self.assertEqual(self.resume(proof)['status'], 'dispatched')
            self.assertEqual(self.resume(proof)['status'], 'dispatched')
        state = f.runtime._load()
        self.assertEqual([x[0] for x in f.calls], ['spawn_agent', 'wait_agent', 'send_input'])
        self.assertEqual(state['runtime']['operations']['dispatch-A:launch']['permit_id'], permit)
        self.assertEqual(len(state['runtime']['executors']), 1)
        self.assertEqual(len(f.controller.read()[0]['capacity_requests']), 1)
        self.assertEqual(len(f.git._entries()), 2)
        self.assertEqual(state['runtime']['executors']['native_agent:native-fixture']['generation'], 1)

    def test_recovery_rejects_missing_forged_or_nonterminal_evidence(self):
        from evidence import load_bound
        f = self.f
        proof = self.interrupt_intent()
        original = load_bound(proof)
        for field, value in [('closed', False), ('host_calls', 1), ('pending', {'tool': 'spawn'}),
            ('result_refs', [proof]), ('exit_code', None), ('child_pid', 999), ('schema', 'self-asserted')]:
            with self.subTest(field=field):
                changed = dict(original, **{field: value})
                candidate = f.json('external/forged-'+field+'.json', changed)
                before = f.runtime.path.read_bytes(), f.controller.path.read_bytes()
                with self.assertRaises((ValueError, KeyError)):
                    f.dispatch.resume_prehost(f.request, candidate, authorized=True)
                self.assertEqual((f.runtime.path.read_bytes(), f.controller.path.read_bytes()), before)
        with self.assertRaisesRegex(ValueError, 'sideband'):
            f.dispatch.resume_prehost(f.request, proof)
        with self.assertRaises((ValueError, OSError)):
            f.dispatch.resume_prehost(f.request, dict(proof, sha256='0'*64), authorized=True)
        self.assertEqual(f.calls, [])

    def test_recovery_refuses_active_unknown_pid_reuse_and_wrong_binding(self):
        from evidence import load_bound
        f = self.f
        proof = self.interrupt_intent()
        for status, creation in [('alive', 'fixture-creation'), ('unknown', None), ('dead', 'reused')]:
            with patch('dispatch_lock.process_identity', return_value=dict(pid=os.getpid(), creation=creation, status=status)):
                with self.assertRaisesRegex(ValueError, 'active|unknown|reused'):
                    self.resume(proof)
        original = load_bound(proof)
        for field in ('project_id', 'task_id', 'request_id', 'request_digest', 'actor_id'):
            changed = deepcopy(original)
            changed['binding'][field] = 'wrong'
            candidate = f.json('external/wrong-'+field+'.json', changed)
            with self.assertRaises((ValueError, KeyError)):
                self.resume(candidate)
        self.assertEqual(f.calls, [])

    def test_callback_unknown_actual_owner_and_late_return_never_respawn(self):
        f = self.f
        proof = self.interrupt_intent()
        original = f.runtime.path.read_bytes()
        for field, value in [('callback_captures', {'spawn': {}}), ('host_results', [{'operation': 'spawn'}]),
            ('status', 'unknown'), ('status', 'accepted_unconfirmed'), ('executor_id', 'actual-author'), ('generation', 2)]:
            state = f.runtime._load()
            state['runtime']['operations']['dispatch-A:launch'][field] = value
            write_atomic(f.runtime.path, state)
            with patch('dispatch_lock.process_identity', return_value=dict(pid=os.getpid(), creation='fixture-creation', status='dead')):
                with self.assertRaises((ValueError, KeyError)):
                    self.resume(proof)
            self.assertEqual(f.calls, [])
            f.runtime.path.write_bytes(original)
        f.json('external/host-result-late.json', dict(request_id='dispatch-A', actual_agent_id='actual-author'))
        with patch('dispatch_lock.process_identity', return_value=dict(pid=os.getpid(), creation='fixture-creation', status='dead')):
            with self.assertRaisesRegex(ValueError, 'retained'):
                self.resume(proof)
        self.assertEqual(f.calls, [])

    def test_task_notifications_survive_scoped_save_and_global_still_captures_all(self):
        import stage_feedback
        f = self.f
        f.run_dispatch()
        f.prepare('B')
        state = f.runtime._load()
        for key in ('A', 'B'):
            state['tasks'][key]['blocker'] = dict(category='technical', owner='integration-fixture', reason='original '+key)
        with patch.object(stage_feedback, 'owners', return_value={'A': 'integration-fixture', 'B': 'integration-fixture'}):
            stage_feedback.capture(state)
            write_atomic(f.runtime.path, state)
            events = deepcopy(state['runtime']['stage_feedback'])
            state['tasks']['A']['blocker']['reason'] = 'changed A'
            state['tasks']['B']['blocker']['reason'] = 'unobserved B'
            write_atomic(f.runtime.path, state)
            f.dispatch._record('dispatch-A', reason='metadata only')
            after = f.runtime._load()
            self.assertEqual(after['tasks']['B'], state['tasks']['B'])
            old_b = {k:v for k,v in events['events'].items() if v['task_id'] == 'B'}
            self.assertEqual({k:v for k,v in after['runtime']['stage_feedback']['events'].items() if v['task_id'] == 'B'}, old_b)
            self.assertTrue(any(e['task_id'] == 'A' and e['status'] == 'pending' and
                e['source']['reason'] == 'changed A' for e in after['runtime']['stage_feedback']['events'].values()))
            f.runtime._save(after, dict(op='global-metadata'))
            self.assertTrue(any(e['task_id'] == 'B' and e['status'] == 'pending' and
                e['source']['reason'] == 'unobserved B' for e in f.runtime._load()['runtime']['stage_feedback']['events'].values()))

    def test_routing_scope_for_batch_dependency_and_rejected_command(self):
        from demand_routing import feedback_tasks
        f = self.f
        f.run_dispatch()
        f.prepare('B')
        state = f.runtime._load()
        data = state['runtime']['intake']
        data['plans']['batch'] = dict(items=[dict(task_id='A'), dict(task_id='B')])
        data['dependencies']['dependency'] = dict(source_task='A', target_task='B')
        self.assertEqual(feedback_tasks(state, dict(op='present', plan_id='batch')), ['A', 'B'])
        self.assertEqual(feedback_tasks(state, dict(op='verify-dependency', dependency_id='dependency')), ['A', 'B'])
        with patch('stage_feedback.capture') as capture:
            result = f.runtime.routing(dict(op='adopt', task_id='missing', event_id='rejected'), f.actor)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(capture.call_args.args[1], [])

    def test_changed_permit_route_rights_and_actual_author_block_recovery(self):
        f = self.f
        proof = self.interrupt_intent()
        original = f.runtime.path.read_bytes()
        for kind in ('permit', 'route', 'rights', 'author', 'host-intent'):
            state = f.runtime._load()
            if kind == 'permit': state['runtime']['operations']['dispatch-A:launch']['permit_id'] = 'other'
            elif kind == 'route': state['runtime']['intake']['routes']['dispatch-A:route']['attempt_id'] = 'other'
            elif kind == 'rights': state['tasks']['A']['authorization']['commit']['allowed'] = False
            elif kind == 'author':
                state['runtime']['resources']['A:agent'] = dict(id='A:agent', kind='agent', task_id='A')
            else:
                f.json('external/host-'+fingerprint(['dispatch-A', 'multi_agent_v1__spawn_agent', 'intent'])+'.json',
                       dict(tool='multi_agent_v1__spawn_agent'))
            write_atomic(f.runtime.path, state)
            with patch('dispatch_lock.process_identity', return_value=dict(pid=os.getpid(), creation='fixture-creation', status='dead')):
                with self.assertRaises((ValueError, KeyError)):
                    self.resume(proof)
            self.assertEqual(f.calls, [])
            f.runtime.path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
