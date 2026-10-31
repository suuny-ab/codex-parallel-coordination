"""Owned resource receipts, not task lifecycle; trusted adapters, no arbitrary PID/Git cleanup."""
import copy
from pathlib import Path
import shutil
import uuid

if __package__:
    from .support import artifact, cleanup_verified, fingerprint, load_bound, require, text, project_roots
else:
    from support import artifact, cleanup_verified, fingerprint, load_bound, require, text, project_roots


class TemporaryFilesystem:
    """Only explicitly allowed temporary artifacts; never follows links or Git roots."""
    def observe(self, spec):
        path = Path(spec['path'])
        if not path.exists():
            return {'exists': False, 'identity': None, 'safe': not path.is_symlink()}
        nodes = [path] + list(path.rglob('*')) if path.is_dir() else [path]
        safe = not any(p.is_symlink() or p.is_junction() or p.name.casefold() == '.git' for p in nodes)
        directory = spec['kind'] in ('temporary_directory', 'acceptance_environment')
        safe = safe and path.is_dir() == directory
        stat = path.stat()
        return {'exists': True, 'identity': {'device': stat.st_dev, 'inode': stat.st_ino, 'created_ns': stat.st_ctime_ns},
                'safe': safe, 'reason': None if safe else 'links/Git/type mismatch; no filesystem fallback'}

    def remove(self, spec, expected_identity):
        observation = self.observe(spec)
        require(observation['safe'] and observation['identity'] == expected_identity, 'artifact identity changed')
        path = Path(spec['path'])
        if path.is_dir():
            shutil.rmtree(path)  # A partial directory failure is unknown, never auto-retried.
        else:
            try:
                path.unlink()
            except PermissionError:
                checked = self.observe(spec)
                if checked['safe'] and checked['exists'] and checked['identity'] == expected_identity:
                    return {'status': 'failed_no_change', 'reason': 'atomic unlink denied; original identity still present'}
                raise
        return {'status': 'removed'}


class ArtifactCleanup:
    def __init__(self, controller, adapters=None):
        self.controller = controller
        filesystem = TemporaryFilesystem()
        self.adapters = {k: filesystem for k in ('temporary_file', 'temporary_directory', 'temporary_database', 'acceptance_environment')}
        self.adapters.update(adapters or {})  # Trusted host code, never JSON imports.

    def _scope(self, spec, policy):
        require(spec['project_id'] in policy['projects'], 'unknown cleanup project')
        authority = load_bound(policy['authority'])
        require(authority.get('cleanup_artifacts') is True, 'artifact cleanup not authorized')
        path = Path(spec['path'])
        roots = policy.get('cleanup_roots', {}).get(spec['project_id'], [])
        require(path.is_absolute() and roots and all(Path(r).is_absolute() for r in roots), 'explicit cleanup roots required')
        path = path.resolve()
        require(any(path != Path(r).resolve() and path.is_relative_to(Path(r).resolve()) for r in roots), 'outside exact cleanup subtree/root itself protected')
        for owner in policy['projects']:
            if owner == spec['project_id']:
                continue
            foreign = policy.get('cleanup_roots', {}).get(owner, [])
            require(isinstance(foreign, list) and all(Path(r).is_absolute() for r in foreign), 'invalid other-project cleanup scope')
            for root in project_roots(policy, owner) + [Path(r).resolve() for r in foreign]:
                require(not path.is_relative_to(root) and not root.is_relative_to(path), 'cleanup overlaps another project scope')
        state, _ = self.controller.read()
        for protected in (self.controller.path.parent, Path(state['policy']['path']), Path(policy['authority']['path'])):
            require(not protected.resolve().is_relative_to(path) and not path.is_relative_to(protected.resolve()),
                    'resource state/authority subtree protected')
        require(spec['kind'] in self.adapters, 'cleanup kind has no trusted adapter; pending_cleanup')
        return self.adapters[spec['kind']]

    @staticmethod
    def _no_nested(state, spec):
        path = Path(spec['path']).resolve()
        for key, other in state['cleanup_resources'].items():
            if key == spec['resource_id'] or other['status'] == 'reclaimed':
                continue
            registered = Path(other['spec']['path']).resolve()
            require(not path.is_relative_to(registered) and not registered.is_relative_to(path),
                    'overlap with another registered resource; reconcile ownership before cleanup')

    def register(self, spec):
        required = {'resource_id', 'project_id', 'task_id', 'attempt_id', 'kind', 'path', 'references', 'protected'}
        require(isinstance(spec, dict) and required.issubset(spec) and set(spec).issubset(required | {'metadata'}), 'cleanup resource fields invalid')
        require(all(text(spec[k]) for k in required - {'references', 'protected'}) and type(spec['protected']) is bool,
                'resource identities and explicit protection required')
        require(isinstance(spec['references'], list) and all(text(x) for x in spec['references'])
                and len(spec['references']) == len(set(spec['references'])), 'explicit shared resource references required')
        require(isinstance(spec.get('metadata', {}), dict) and not {'task_stage', 'stage', 'agent_lease'}.intersection(spec.get('metadata', {})),
                'resource metadata must not duplicate canonical task/agent stages')
        with self.controller.transaction():
            state, policy = self.controller.read()
            adapter = self._scope(spec, policy)
            resources = state['cleanup_resources']
            key = spec['resource_id']
            if key in resources:
                require(resources[key]['spec_digest'] == fingerprint(spec), 'conflicting resource registration')
                return copy.deepcopy(resources[key])
            self._no_nested(state, spec)
            observed = adapter.observe(spec)
            require(observed.get('safe') is True and observed.get('exists') is True and isinstance(observed.get('identity'), dict),
                    'resource identity/safety not established')
            resources[key] = {'spec': copy.deepcopy(spec), 'spec_digest': fingerprint(spec), 'identity': observed['identity'], 'status': 'registered'}
            self.controller.save(state, 'cleanup_resource_registered', resource_id=key)
            return copy.deepcopy(resources[key])

    def update_usage(self, resource_id, project, task, attempt, references, protected):
        require(isinstance(references, list) and all(text(x) for x in references) and type(protected) is bool, 'explicit usage required')
        with self.controller.transaction():
            state, _ = self.controller.read()
            record = state['cleanup_resources'][resource_id]
            spec = record['spec']
            require(tuple(spec[k] for k in ('project_id', 'task_id', 'attempt_id')) == (project, task, attempt), 'another resource owner')
            require(record['status'] == 'registered', 'cleanup intent outstanding; cannot reuse without reconcile')
            spec.update(references=references, protected=protected)
            record['spec_digest'] = fingerprint(spec)
            self.controller.save(state, 'cleanup_usage_updated', resource_id=resource_id)

    def cleanup(self, resource_id, proof_ref, verifier=None, retry=False):
        with self.controller.transaction():
            state, policy = self.controller.read()
            record = state['cleanup_resources'][resource_id]
            if record['status'] == 'reclaimed':
                return {'resource_id': resource_id, 'status': 'reclaimed', 'duplicate': True}
            if record['status'] != 'registered' and not (retry and record.get('last_result', {}).get('status') == 'failed_no_change'):
                return self._observe_locked(state, policy, resource_id)
            spec = record['spec']
            adapter = self._scope(spec, policy)
            self._no_nested(state, spec)
            require(not spec['protected'] and not spec['references'], 'shared/protected resource cannot cleanup')
            require(not Path(proof_ref['path']).resolve().is_relative_to(Path(spec['path']).resolve()), 'preservation proof must be external')
            proof = load_bound(proof_ref)
            require(callable(verifier) and all(proof.get(k) == spec[k] for k in ('resource_id', 'project_id', 'task_id', 'attempt_id')),
                    'canonical resource cleanup proof required')
            cleanup_verified(verifier(copy.deepcopy(spec), copy.deepcopy(proof)))
            observation = adapter.observe(spec)
            require(observation.get('safe') is True and observation.get('identity') == record['identity'], 'resource identity changed; pending_cleanup')
            operation = str(uuid.uuid4())
            record.update(status='cleaning', operation_id=operation, proof_ref=proof_ref)
            self.controller.save(state, 'cleanup_intent_reserved', resource_id=resource_id, operation_id=operation)
            expected, request = copy.deepcopy(record['identity']), copy.deepcopy(spec)
        try:
            result = adapter.remove(request, expected)
            require(isinstance(result, dict) and result.get('status') in ('removed', 'failed_no_change', 'unknown'), 'invalid cleanup adapter acknowledgment')
            observed = adapter.observe(request)
        except Exception as error:
            result, observed = {'status': 'unknown', 'error_type': type(error).__name__}, None
        with self.controller.transaction():
            state, _ = self.controller.read()
            record = state['cleanup_resources'][resource_id]
            require(record['operation_id'] == operation, 'another cleanup operation owns this receipt')
            done = result['status'] == 'removed' and observed is not None and observed.get('exists') is False and observed.get('safe') is True
            record.update(status='reclaimed' if done else 'pending_cleanup', last_result=result)
            self.controller.save(state, 'cleanup_receipt_recorded', resource_id=resource_id, status=record['status'])
            return {'resource_id': resource_id, 'status': record['status'], 'operation_id': operation,
                    'task_completed_changed': False, 'result': result}

    def _observe_locked(self, state, policy, resource_id):
        record = state['cleanup_resources'][resource_id]
        self._no_nested(state, record['spec'])
        observed = self._scope(record['spec'], policy).observe(record['spec'])
        if record['status'] in ('cleaning', 'pending_cleanup') and observed.get('exists') is False and observed.get('safe') is True:
            record.update(status='reclaimed', last_result={'status': 'removed', 'basis': 'actual absence reconciled; no retry'})
            self.controller.save(state, 'cleanup_absence_reconciled', resource_id=resource_id)
        return {'resource_id': resource_id, 'status': record['status'], 'observation': observed, 'side_effect_retried': False}

    def observe(self, resource_id):
        """Reconcile an outstanding operation only; never executes remove."""
        with self.controller.transaction():
            state, policy = self.controller.read()
            return self._observe_locked(state, policy, resource_id)
