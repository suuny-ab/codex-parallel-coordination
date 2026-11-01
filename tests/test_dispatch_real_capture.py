"""Relocate the existing captured task; preserve every historical task and event."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import unittest
import time
from unittest.mock import patch

import test_dispatch_prehost_transport
import test_dispatch_host
from demand_routing import task_version
from evidence import artifact, fingerprint, load_bound, ref, write_atomic
from codex_parallel_coordination.dispatch import approved_request
from codex_parallel_coordination.resources.support import fingerprint as resource_fingerprint


@unittest.skipUnless(os.environ.get('COORDINATION_REAL_CAPTURE') and os.environ.get('COORDINATION_REAL_DISPATCH_INPUT'),
                     'explicit frozen real inputs required; no live discovery')
class RealCaptureTransport(unittest.TestCase):
    has_capture = True

    def setUp(self):
        self.host = test_dispatch_host.HostTransport('test_real_thread_id_wins_over_parent_session_id')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        f = self.host.fixture
        captured = Path(os.environ['COORDINATION_REAL_CAPTURE'])
        inputs = Path(os.environ['COORDINATION_REAL_DISPATCH_INPUT'])
        raw = (captured/'INPUT_CAPTURE.json').read_bytes()
        original = json.loads(raw)
        self.original = original
        rows = json.loads((inputs/'MANIFEST.json').read_text(encoding='utf-8'))['entries']
        rows += json.loads((captured/'MANIFEST.json').read_text(encoding='utf-8'))['entries']
        frozen = {(os.path.normcase(str(Path(row['original']['path']).resolve())), row['original']['sha256']): row
                  for row in rows}
        def input_named(name):
            return next(row['preserved'] for row in rows if Path(row['original']['path']).name == name)
        original_request = load_bound(input_named('DISPATCH_REQUEST_R2.json'))
        original_profile = load_bound(input_named('HOST_PROFILE_SIDEBAND_R1.json'))
        target = 'CLEANUP-RESUME'
        old_project = original['project']['id']
        old_digest = original['runtime']['operations'][original_request['request_id']]['request_digest']
        replacements = {original['project']['root']: str(f.runtime.project_root),
            original['runtime']['cleanup_root']: str(f.managed), old_project: f.runtime.project_id,
            original_profile['evidence_root']: str(f.bridge.evidence),
            original_request['report_path']: str(f.root/'reports/author.json')}
        originals = Path(os.environ['COORDINATION_TIMEOUT_EVIDENCE'])/'frozen-target-objects'
        originals.mkdir(parents=True)
        material = f.root/'relocated'
        material.mkdir()
        cache, sources = {}, []
        def text(value):
            for old, new in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
                value = value.replace(old, new).replace(old.replace('\\', '/'), new.replace('\\', '/'))
            return value
        def relocate(value):
            if isinstance(value, str): return text(value)
            if isinstance(value, list): return [relocate(v) for v in value]
            if not isinstance(value, dict): return value
            if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
                key = (os.path.normcase(str(Path(value['path']).resolve())), value['sha256'])
                if key in cache: return cache[key]
                row = frozen.get(key)
                source = artifact(row['preserved']) if row and row['status'] == 'captured' else artifact(value)
                data = source.read_bytes()
                kept = originals/value['sha256']
                if not kept.exists(): kept.write_bytes(data)
                sources.append(dict(original=deepcopy(value), preserved=ref(kept)))
                try:
                    decoded = json.loads(data)
                    data = (json.dumps(relocate(decoded), ensure_ascii=False, indent=2)+'\n').encode()
                except (ValueError, UnicodeError): pass
                name = material/(hashlib.sha256(str(key).encode()).hexdigest()+'.json')
                name.write_bytes(data)
                result = ref(name)
                cache[key] = result
                return result
            return {text(k): relocate(v) for k,v in value.items()}
        # Only the current task's evidence closure is materialized. Other tasks'
        # immutable facts/notifications stay byte-equivalent in the ledger copy.
        request = relocate(original_request)
        request['project_id'] = f.runtime.project_id
        expanded = approved_request(request)
        replacements[old_digest] = fingerprint(expanded)
        cache.clear()
        state = deepcopy(original)
        state['project'].update(id=f.runtime.project_id, root=str(f.runtime.project_root))
        state['runtime']['cleanup_root'] = str(f.managed)
        state['tasks'][target] = relocate(original['tasks'][target])
        for key, operation in original['runtime']['operations'].items():
            if operation.get('task_id') == target:
                state['runtime']['operations'][key] = relocate(operation)
        intake = state['runtime']['intake']
        for collection in ('requirements', 'plans', 'approvals', 'instructions', 'routes'):
            # Routing records reference shared approved material; relocate refs
            # for this task only, leave unrelated historical records untouched.
            for key, value in list(intake.get(collection, {}).items()):
                if target in json.dumps(value, ensure_ascii=False) or key == state['tasks'][target]['routing']['requirement_id']:
                    intake[collection][key] = relocate(value)
        route_id = expanded['request_id']+':route'
        intake['routes'][route_id]['version'] = task_version(state, target)
        for key, value in list(state['runtime'].get('resources', {}).items()):
            if value.get('task_id') == target: state['runtime']['resources'][key] = relocate(value)
        resources = load_bound(input_named('state.json'))
        resources['policy'] = relocate(resources['policy'])
        for key, row in list(resources['capacity_requests'].items()):
            if row['spec']['task_id'] == target:
                resources['capacity_requests'][key] = relocate(row)
                resources['capacity_requests'][key]['spec_digest'] = resource_fingerprint(resources['capacity_requests'][key]['spec'])
        for key, row in list(resources.get('cleanup_resources', {}).items()):
            if row.get('task_id') == target: resources['cleanup_resources'][key] = relocate(row)
        write_atomic(f.controller.path, resources)
        write_atomic(f.runtime.path, state)
        f.bridge.evidence.mkdir(parents=True, exist_ok=True)
        source = load_bound(state['runtime']['operations'][expanded['request_id']+':launch']['admission'])
        write_atomic(f.bridge.evidence/('dispatch-'+fingerprint(expanded['request_id'])+'-capacity.json'),
            load_bound(source['resource_source']))
        f.actor = state['coordinator_id']
        f.dispatch.actor = f.actor
        f.request = expanded
        f.origin = dict(role='user', conversation_id=f.actor, reference=expanded['approval'])
        base = f.dispatch._base(expanded, state['runtime']['operations'][expanded['request_id']])
        f.git._git('worktree', 'add', '-b', expanded['branch'], expanded['worktree'], base)
        # The actual isolated tree has a new filesystem identity. Preserve the
        # original in the raw capture; bind only this relocated fixture object.
        tree_info = Path(expanded['worktree']).stat()
        state['runtime']['resources'][target+':worktree']['path_identity'] = [tree_info.st_dev, tree_info.st_ino]
        write_atomic(f.runtime.path, state)
        self.host.config.update(request=f.json('real-relocated-request.json', expanded),
            requestMeta=dict(threadId=f.actor), approvalOrigin=f.origin)
        self.host.config['profile'].update(integration_channel='app_thread', integration_target=expanded['integration_agent'])
        self.host.config['availableTools'][-1] = 'mcp__codex_app__send_message_to_thread'
        self.original_others = {k:deepcopy(v) for k,v in state['tasks'].items() if k != target}
        write_atomic(originals/'MANIFEST.json', dict(input_sha256=hashlib.sha256(raw).hexdigest(),
            entries=sources, relocation=dict(replacements=replacements, request=expanded,
                boundary='Paths/project identity/request hashes and dead fixture child identity only; all tasks/history retained.')))

    def test_captured_existing_intent_reaches_host_in_original_45_seconds(self):
        import evidence
        f = self.host.fixture
        before = f.runtime.path.read_bytes()
        captured = Path(os.environ['COORDINATION_REAL_CAPTURE'])
        baseline = (captured/'BASELINE_DISPATCH.py').read_bytes()
        namespace = dict(__name__='original_real_dispatch')
        exec(compile(baseline, 'original_real_dispatch.py', 'exec'), namespace)
        rows = json.loads((captured/'MANIFEST.json').read_text(encoding='utf-8'))['entries']
        mapping = {(row['original']['path'], row['original']['sha256']): row for row in rows}
        actual = evidence.original_artifact
        def frozen(reference):
            row = mapping.get((reference['path'], reference['sha256']))
            if row:
                if row['status'] != 'captured': raise ValueError('frozen source unavailable')
                return actual(row['preserved'])
            return actual(reference)  # Relocated current-task fixture evidence.
        f.developer.actor_id = f.integration.actor_id = f.actor
        old = namespace['StandardDispatch'](f.runtime, f.bridge, f.git, f.developer, f.integration,
            f.origin, f.request['integration_agent'])
        started = time.perf_counter()
        with patch.object(evidence, 'original_artifact', frozen):
            result = old.run(f.request)
        self.assertEqual(result['status'], 'intent')
        self.assertEqual(f.calls, [])
        write_atomic(Path(os.environ['COORDINATION_TIMEOUT_EVIDENCE'])/'ORIGINAL_REENTRY.json',
            dict(result=result, elapsed_seconds=time.perf_counter()-started, source_sha256=hashlib.sha256(baseline).hexdigest(),
                boundary='Original Python dispatcher on relocated same captured state, fixture adapter; no native calls.'))
        f.runtime.path.write_bytes(before)
        # Reuse the exact host-module/real-child fault harness, with the captured
        # original request/task/permit instead of registering a new demand.
        test_dispatch_prehost_transport.PrehostTransport.probe(self)
        state = self.host.fixture.runtime._load()
        self.assertEqual(len(state['tasks']), len(self.original['tasks']))
        self.assertEqual({k:v for k,v in state['tasks'].items() if k != 'CLEANUP-RESUME'}, self.original_others)
