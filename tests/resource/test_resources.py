"""Real small Windows jobs; low-memory is explicit injection, never actual OOM."""
import copy
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'component'
sys.path.insert(0, str(SCRIPTS))
from resources import Controller
from support import fingerprint, load, ref, write_atomic
from windows_resources import system_memory


class ResourceTests(unittest.TestCase):
    def setUp(self):
        evidence = Path(os.environ['COORDINATION_RESOURCE_TEST_EVIDENCE'])
        evidence.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=fingerprint(self._testMethodName)[:8] + '-', dir=evidence)).resolve()
        self.file('test-name.json', {'test': self._testMethodName})
        for project in ('alpha', 'beta'):
            (self.root / project).mkdir()
        self.authority = self.file('authority.json', {'scope': 'explicit_temporary_managed_resources', 'launch': True, 'reclaim_idle': True})
        self.policy = {'authority': ref(self.authority), 'max_parallel': 1, 'max_agent_slots': 1,
            'physical_reserve': 1024 * 1024, 'commit_reserve': 1024 * 1024,
            'projects': {x: str(self.root / x) for x in ('alpha', 'beta')},
            'executables': [sys.executable], 'exclusive_keys': ['heavy-comparison'],
            'phase_resources': {'self_test': ['heavy-comparison'], 'benchmark': ['heavy-comparison']},
            'allow_test_memory_injection': True}
        self.path = self.root / 'instance/resources.json'
        self.controller = Controller(self.path)
        self.controller.initialize(self.file('policy.json', self.policy))

    def file(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(p, value)
        return p

    def request(self, key='alpha:A', project='alpha', body="print('unchanged arguments')", **changes):
        value = {'request_id': key, 'project_id': project, 'task_id': key.split(':')[-1], 'attempt_id': 'attempt-1',
            'resource_kind': 'temporary_auxiliary', 'phase': 'self_test',
            'purpose': 'Disposable resource lifecycle verification', 'launch_identity': 'fixture-original-author',
            'cwd': str(self.root / project), 'argv': [sys.executable, '-B', '-c', body],
            'peak_bytes': 4 * 1024 * 1024, 'exclusive_keys': ['heavy-comparison'],
            'protection': 'running', 'reclaim_allowed': False}
        value.update(changes)
        return self.file(key.replace(':', '-') + '.json', value)

    def test_actual_memory_and_process_exit_preserve_argv_and_deduplicate(self):
        actual = system_memory()
        self.assertGreater(actual['physical_available'], 0)
        self.assertGreater(actual['commit_available'], 0)
        spec = self.request()
        result = self.controller.submit(spec)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['result']['tree_empty'])
        self.assertEqual(load(spec)['argv'], result['spec']['argv'])
        self.assertEqual((Path(result['logs']) / 'stdout.txt').read_text().strip(), 'unchanged arguments')
        before = self.path.read_bytes()
        duplicate = self.controller.submit(spec)
        self.assertEqual(duplicate['nonce'], result['nonce'])
        self.assertEqual(before, self.path.read_bytes())

    def test_injected_physical_or_commit_shortage_rejects_launch_retains_queue(self):
        for kind in ('physical', 'commit'):
            sample = {'source': 'explicit low-memory injection', 'injected': True,
                'physical_available': 10**12, 'commit_available': 10**12}
            sample[kind + '_available'] = 1
            controller = Controller(self.path, memory=lambda: sample)
            result = controller.submit(self.request('alpha:' + kind))
            self.assertEqual(result['status'], 'queued')
            self.assertIsNone(result['nonce'])
            head = min(load(self.path)['requests'].values(), key=lambda x: x['sequence'])
            self.assertIn(kind + '_headroom', head['reason'])
            # Keep independent branch FIFO from concealing the second branch.
            state = load(self.path)
            state['requests'] = {}
            write_atomic(self.path, state)

    def test_fifo_release_event_runs_next_project_and_error_exit_releases(self):
        a = self.request(body='raise SystemExit(7)')
        b = self.request('beta:B', 'beta', body="print('next-project')")
        self.controller.submit(a, run=False)
        self.controller.submit(b, run=False)
        self.assertEqual(self.controller.pump(), ['alpha:A', 'beta:B'])
        requests = load(self.path)['requests']
        self.assertEqual(requests['alpha:A']['status'], 'failed')
        self.assertEqual(requests['alpha:A']['result']['root_exit_code'], 7)
        self.assertEqual(requests['beta:B']['status'], 'completed')
        releases = [h['request_id'] for h in load(self.path)['history'] if h['operation'] == 'permit_released']
        self.assertEqual(releases, ['alpha:A', 'beta:B'])
        self.assertEqual(Controller(self.path).pump(), [])

    def test_cross_project_scope_and_changed_authority_refused(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.submit(self.request(project='beta', cwd=str(self.root / 'alpha')))
        self.assertEqual(before, self.path.read_bytes())
        self.authority.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.controller.submit(self.request())

    def test_phase_required_keys_rejected_before_canonical_and_same_id_can_be_corrected(self):
        before = self.path.read_bytes()
        spec = self.request(phase='benchmark', exclusive_keys=[])
        with self.assertRaises(ValueError):
            self.controller.submit(spec, run=False)
        self.assertEqual(self.path.read_bytes(), before)
        corrected = self.request(phase='benchmark', exclusive_keys=['heavy-comparison'])
        self.assertEqual(self.controller.submit(corrected)['status'], 'completed')
        self.assertEqual(len(load(self.path)['requests']), 1)

    def test_undeclared_phase_cannot_bypass_machine_resource_rules(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.submit(self.request(phase='invented-safe-phase', exclusive_keys=[]), run=False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_or_malformed_phase_policy_cannot_initialize_or_restore_as_trusted(self):
        for name, rules in (('missing', None), ('empty', {}), ('unknown-key', {'benchmark': ['not-authorized-key']})):
            policy = copy.deepcopy(self.policy)
            if rules is None:
                policy.pop('phase_resources')
            else:
                policy['phase_resources'] = rules
            controller = Controller(self.root / name / 'resources.json')
            controller.path.parent.mkdir()
            with self.assertRaises(ValueError):
                controller.initialize(self.file(name + '-policy.json', policy))
            self.assertFalse(controller.path.exists())
        state = load(self.path)
        legacy = copy.deepcopy(self.policy)
        legacy.pop('phase_resources')
        state['policy'] = ref(self.file('legacy-policy.json', legacy))
        write_atomic(self.path, state)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            Controller(self.path).pump()
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_external_worktree_root_launches_but_other_project_cannot_use_it(self):
        worktree = self.root / 'external-worktrees' / 'alpha-A'
        worktree.mkdir(parents=True)
        policy = copy.deepcopy(self.policy)
        policy['projects']['alpha'] = [str(self.root / 'alpha'), str(worktree)]
        controller = Controller(self.root / 'multi-root/resources.json')
        controller.path.parent.mkdir()
        controller.initialize(self.file('multi-root-policy.json', policy))
        request = self.request(cwd=str(worktree))
        self.assertEqual(controller.submit(request)['status'], 'completed')
        with self.assertRaises(ValueError):
            controller.submit(self.request('beta:foreign', 'beta', cwd=str(worktree)))

    def test_overlapping_cross_project_work_roots_cannot_initialize(self):
        policy = copy.deepcopy(self.policy)
        policy['projects']['beta'] = [str(self.root / 'alpha' / 'nested')]
        (self.root / 'alpha/nested').mkdir()
        controller = Controller(self.root / 'overlap/resources.json')
        controller.path.parent.mkdir()
        with self.assertRaises(ValueError):
            controller.initialize(self.file('overlap-policy.json', policy))
        self.assertFalse(controller.path.exists())

    def test_running_root_exited_child_alive_cannot_release_or_recover_as_idle(self):
        marker = self.root / 'child.json'
        child = "import pathlib,time; pathlib.Path(" + repr(str(marker)) + ").write_text('child-started'); time.sleep(1.5)"
        body = 'import subprocess,sys; subprocess.Popen([sys.executable,"-B","-c",' + repr(child) + '])'
        spec = self.request(body=body)
        proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(SCRIPTS / 'resources.py'),
            '--state', str(self.path), 'submit', str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Bounded test synchronization only. Product uses OS process events, no periodic poller.
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        state = load(self.path)
        self.assertEqual(state['requests']['alpha:A']['status'], 'running')
        result = Controller(self.path).recover('alpha:A')
        self.assertEqual(result['status'], 'still_running')
        self.assertTrue(result['permit_retained'])
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(json.loads(out)['status'], 'completed')
        self.assertGreaterEqual(len(load(self.path)['requests']['alpha:A']['result']['members']), 1)

    def test_model_capacity_is_separate_and_does_not_change_local_permits(self):
        self.controller.submit(self.request(), run=False)
        before = copy.deepcopy(load(self.path)['requests'])
        event = self.file('model.json', {'event_id': 'capacity:1', 'category': 'model_capacity', 'model': 'unchanged-model'})
        result = self.controller.model_capacity(ref(event))
        self.assertFalse(result['model_switched'])
        self.controller.model_capacity(ref(event))
        self.assertEqual(load(self.path)['requests'], before)
        self.assertEqual(len(load(self.path)['model_capacity_events']), 1)

    def start_waiting(self, protection='none', reclaim_allowed=True):
        marker = self.root / 'running.marker'
        body = "import pathlib,time; pathlib.Path(" + repr(str(marker)) + ").write_text('ready'); time.sleep(3)"
        spec = self.request(body=body, protection=protection, reclaim_allowed=reclaim_allowed)
        proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(SCRIPTS / 'resources.py'),
            '--state', str(self.path), 'submit', str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.communicate(timeout=10))
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        return proc, load(self.path)['requests']['alpha:A']

    def test_actual_idle_reclaim_only_after_owner_release_not_other_project_or_pid(self):
        proc, request = self.start_waiting()
        nonce = request['nonce']
        with self.assertRaises(ValueError):
            self.controller.reclaim('alpha:A', 'alpha', 'A', nonce)  # Still running.
        with self.assertRaises(ValueError):
            self.controller.reclaim('alpha:A', 'beta', 'A', nonce)
        with self.assertRaises(KeyError):
            self.controller.reclaim('unregistered', 'alpha', 'A', nonce)
        proof = self.file('owner-idle.json', {'project_id': 'alpha', 'task_id': 'A', 'nonce': nonce,
            'no_longer_needed': True, 'basis': 'Disposable owner test explicitly released its idle temporary environment'})
        self.controller.idle('alpha:A', 'alpha', 'A', nonce, ref(proof))
        original = load(self.path)
        changed = copy.deepcopy(original)
        changed['requests']['alpha:A']['process']['created'] += 1
        write_atomic(self.path, changed)
        with self.assertRaises(ValueError):
            self.controller.reclaim('alpha:A', 'alpha', 'A', nonce)
        write_atomic(self.path, original)
        result = self.controller.reclaim('alpha:A', 'alpha', 'A', nonce)
        self.assertTrue(result['termination_requested'])
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(json.loads(out)['result']['tree_empty'])

    def test_waiting_user_environment_cannot_be_declared_idle_or_reclaimed(self):
        proc, request = self.start_waiting(protection='waiting_user')
        proof = self.file('not-enough.json', {'project_id': 'alpha', 'task_id': 'A', 'nonce': request['nonce'],
            'no_longer_needed': True, 'basis': 'Cannot override protected human environment'})
        with self.assertRaises(ValueError):
            self.controller.idle('alpha:A', 'alpha', 'A', request['nonce'], ref(proof))
        with self.assertRaises(ValueError):
            self.controller.reclaim('alpha:A', 'alpha', 'A', request['nonce'])
        self.assertIsNone(proc.poll())
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)

    def test_two_controller_processes_exclusive_resource_queue_then_exit_handoff(self):
        # Allow two process slots, still prohibit overlap of the benchmark resource.
        policy = dict(self.policy, max_parallel=2)
        path = self.root / 'parallel-instance/resources.json'
        Controller(path).initialize(self.file('parallel-policy.json', policy))
        markers = {key: self.root / (key + '-timing.json') for key in ('A', 'B')}
        processes = []
        for key, project in (('A', 'alpha'), ('B', 'beta')):
            body = "import json,pathlib,time; t=time.time(); time.sleep(.4); pathlib.Path(" + repr(str(markers[key])) + ").write_text(json.dumps({'begin':t,'end':time.time()}))"
            spec = self.request(project + ':' + key, project, body=body)
            processes.append(subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(SCRIPTS / 'resources.py'),
                '--state', str(path), 'submit', str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for proc in processes:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, err)
        timings = [load(p) for p in markers.values()]
        self.assertTrue(timings[0]['end'] <= timings[1]['begin'] or timings[1]['end'] <= timings[0]['begin'])
        state = load(path)
        self.assertEqual({r['status'] for r in state['requests'].values()}, {'completed'})
        self.assertEqual(len([x for x in state['history'] if x['operation'] == 'permit_reserved']), 2)

    def test_crashed_monitor_never_inferrs_descendant_exit_from_missing_job_name(self):
        import ctypes as C
        from windows_resources import api, k32, identity, ok, close, open_process
        proc, request = self.start_waiting(reclaim_allowed=False)
        expected = request['launcher']
        handle = ok(open_process(0x1000 | 0x100000 | 1, False, expected['pid']))
        try:
            self.assertEqual(identity(handle, expected['pid'])['created'], expected['created'])
            ok(api(k32, 'TerminateProcess', C.c_int, C.c_void_p, C.c_uint)(handle, 91))
        finally:
            close(handle)
        self.assertEqual(Controller(self.path).recover('alpha:A')['status'], 'still_running')
        b = self.request('beta:B', 'beta')
        self.assertEqual(Controller(self.path).submit(b)['status'], 'queued')
        # Fixture sync only: the product never polls to decide a process is gone.
        from windows_resources import observe_job
        deadline = time.monotonic() + 8
        while observe_job(request['job_name'])['members'] and time.monotonic() < deadline:
            time.sleep(.01)
        proc.communicate(timeout=10)
        recovered = Controller(self.path).recover('alpha:A')
        self.assertEqual(recovered['status'], 'unknown_process_tree')
        self.assertTrue(recovered['permit_retained'])
        self.assertEqual(load(self.path)['requests']['beta:B']['status'], 'queued')
        before = self.path.read_bytes()
        self.assertEqual(Controller(self.path).recover('alpha:A')['status'], 'unknown_process_tree')
        self.assertEqual(before, self.path.read_bytes())

    def test_late_monitor_failure_after_actual_tree_exit_recovers_release_and_queue_once(self):
        first = self.request()
        self.controller.submit(first, run=False)
        second = self.request('beta:B', 'beta')
        self.controller.submit(second, run=False)
        program = 'import sys,os;sys.path.insert(0,' + repr(str(SCRIPTS)) + ');from resources import Controller;Controller.finish=lambda *a,**k:os._exit(91);Controller(' + repr(str(self.path)) + ').pump()'
        failed = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', program], capture_output=True, timeout=15)
        self.assertNotEqual(failed.returncode, 0)
        a = load(self.path)['requests']['alpha:A']
        self.assertTrue(load(Path(a['logs']) / 'tree-exit.json')['tree_empty'])
        result = Controller(self.path).recover('alpha:A')
        self.assertEqual(result['next_started_from_release_event'], ['beta:B'])
        before = self.path.read_bytes()
        self.assertTrue(Controller(self.path).recover('alpha:A')['duplicate'])
        self.assertEqual(before, self.path.read_bytes())

    def test_corrupt_and_abandoned_file_lock_never_reinitialize_or_steal(self):
        lock = self.path.with_suffix('.json.lock')
        lock.write_text('preserved unavailable owner', encoding='utf-8')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'never auto-steal'):
            self.controller.submit(self.request())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(lock.read_text(), 'preserved unavailable owner')
        lock.unlink()  # Only this test-created exact file after its assertions.
        self.path.write_bytes(b'broken JSON')
        with self.assertRaises(ValueError):
            Controller(self.path).read()
        self.assertEqual(self.path.read_bytes(), b'broken JSON')

    def test_fake_release_while_running_or_wrong_execution_cannot_free_permit(self):
        proc, request = self.start_waiting(reclaim_allowed=False)
        fake = self.file('not-an-exit.json', {'tree_empty': True})
        for nonce in (request['nonce'], 'other-execution'):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                self.controller.finish('alpha:A', nonce, {'tree_empty': True, 'root_exit_code': 0}, ref(fake))
        self.assertEqual(load(self.path)['requests']['alpha:A']['status'], 'running')
        self.assertIsNone(proc.poll())
        proc.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
