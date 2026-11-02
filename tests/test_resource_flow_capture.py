"""Isolated replay of the original RESOURCE-FLOW record, using explicit frozen inputs."""
from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import unittest

from core_runtime import Runtime
from evidence import ref,load_bound,write_atomic
from codex_parallel_coordination.bridge import ResourceBridge
from codex_parallel_coordination.resources import Controller,ArtifactCleanup


class OriginalResourceFlowCapture(unittest.TestCase):
    def test_original_failed_native_to_actual_managed_CLI_single_binding(self):
        source=os.environ.get('RESOURCE_FLOW_CAPTURE')
        if not source:self.skipTest('Explicit original failed native/managed CLI capture not supplied')
        case=load_bound(ref(source));original=load_bound(case['inputs']['ledger.json']);key=case['task_id']
        with tempfile.TemporaryDirectory(prefix='rf-original-') as temporary:
            root=Path(temporary);runtime=Runtime(root/'authority',Path(case['project_root']))
            runtime.directory.mkdir(parents=True);runtime.path.write_bytes(Path(case['inputs']['ledger.json']['path']).read_bytes())
            controller_path=root/'controller.json';controller_path.write_bytes(Path(case['inputs']['controller.json']['path']).read_bytes())
            controller=Controller(controller_path);bridge=ResourceBridge(runtime,controller,ArtifactCleanup(controller),root/'receipts')
            launch=load_bound(case['inputs']['isolated-launch.json']);log=case['inputs']['cli.jsonl']
            command=dict(action='bind-cli',task_id=key,approval=case['approval'],launch=case['inputs']['isolated-launch.json'],log=log,
                thread_id=case['author_thread'],backend_authority=case['backend_authority'],prior_operation_id=case['prior_operation_id'],
                approved_inputs=dict(spec=case['spec']))
            recipient=load_bound(case['approval'])['integration_agent']
            original_operations=deepcopy(original['runtime']['operations'])
            observed=case['process_observation']
            actual=dict(thread_id=case['author_thread'],pid=observed['identity']['pid'],job_name=observed['job_name'],
                worktree=launch['worktree'],backend='codex_exec',origin='independent_codex_process')
            result=bridge.bind_cli(command,recipient,user_origin=dict(role='user',conversation_id=original['coordinator_id'],
                reference=case['backend_authority']),observe_cli=lambda _:deepcopy(actual))
            after=runtime._load();self.assertEqual(result['status'],'bound')
            self.assertEqual({k:v for k,v in after['runtime']['operations'].items() if k in original_operations},original_operations)
            new_keys=set(after['runtime']['operations'])-set(original_operations)
            self.assertEqual(new_keys,{launch['attempt_id']})
            self.assertEqual(after['runtime']['operations'][launch['attempt_id']]['operation'],'codex_exec')
            self.assertEqual(set(after['tasks']),set(original['tasks']))
            self.assertEqual(after['tasks'][key]['owner_agent_id'],case['author_thread'])
            self.assertEqual(after['tasks'][key]['worktree'],original['tasks'][key]['worktree'])
            self.assertEqual(after['tasks'][key]['mr'],original['tasks'][key]['mr'])
            self.assertEqual(after['runtime']['executors'][case['author_thread']]['origin'],'independent_codex_process')
            self.assertEqual(set(after['runtime']['executors'][case['author_thread']]['routes']),{'codex_exec'})
            self.assertTrue(bridge.bind_cli(command,recipient,user_origin=None,observe_cli=None)['duplicate'])
            write_atomic(Path(source).parent/'case-result.json',dict(status='passed',thread_id=case['author_thread'],
                original_task=key,prior_operation=case['prior_operation_id'],original_operations_unchanged=True,
                task_count_unchanged=True,worktree_unchanged=True,PR_unchanged=True,channel='codex_exec',origin='independent_codex_process',
                scope=case['boundary'],capture=ref(source),runtime_ledger=str(runtime.path)))
