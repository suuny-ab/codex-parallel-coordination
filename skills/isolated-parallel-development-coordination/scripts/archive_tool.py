"""Explicit original-file archiving. Host context is never deserialized from commands.

Only STATE owns ledger mutations. Filesystem work runs outside its write lock;
recovery observes the persisted exact plan rather than traversing report paths.
"""
from copy import deepcopy
import ctypes
import hashlib
import os
from pathlib import Path
import re
import stat

from coordination import writer_lock
from evidence import fingerprint, git, io_path, load_bound, require
from task_state import _ARCHIVE, archive_binding, archive_manifest, receiver, record_archive


def canonical(path):
    value = Path(str(path).removeprefix('\\\\?\\'))
    require(value.is_absolute(), 'absolute archive paths required')
    # Reject links in the lexical chain before resolve can hide them.
    for parent in (value, *value.parents):
        try:
            info = io_path(parent).lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) &
            getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0), 'links/reparse points are not archive files')
    return Path(str(io_path(value).resolve()).removeprefix('\\\\?\\'))


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def move_original(source, destination):
    """No replacement. Windows supports cross-volume moves through its file API.

    POSIX uses an exclusive hard-link/unlink move; an interruption leaving both
    names is reported for manual observation, never silently deleted or copied.
    """
    source, destination = io_path(source), io_path(destination)
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.MoveFileExW.restype = ctypes.c_int
        if not kernel.MoveFileExW(str(source), str(destination), 2):  # COPY_ALLOWED, never REPLACE_EXISTING
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.link(source, destination)
        source.unlink()


def file_fact(path, expected):
    canonical(path)
    try:
        info = io_path(path).stat()
    except FileNotFoundError:
        return 'missing'
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'regular exclusive file required')
    actual = hashlib.sha256(io_path(path).read_bytes()).hexdigest()
    return 'matching' if actual == expected else 'changed'


class ArchiveTool:
    """A trusted host binds the existing project, actual caller and fixed policy.

    authorize(task, actor, plan) returns True only for the actual granted scope.
    observe_stop(task) must observe cancellation/stop and merge outcome, not read
    a self-signed report as authority. This in-process seam is not an OS sandbox.
    """
    def __init__(self, runtime, actor_id, *, archive_root, spec_root, authorize, observe_stop=None):
        runtime._load()
        require(isinstance(actor_id, str) and actor_id and callable(authorize), 'actual host identity and archive authorization required')
        self.runtime, self.actor = runtime, actor_id
        self.policy = dict(archive_root=str(canonical(archive_root)), spec_root=str(canonical(spec_root)))
        self.authorize, self.observe_stop = authorize, observe_stop

    def _context(self, state, key):
        task = state['tasks'][key]
        require(self.actor == receiver(state, key) and self.actor != task['owner_agent_id'],
                'designated independent integrator required for archive')
        archive_manifest(state, key)  # STATE or the approved canonical routing record, never a report.
        require(state['project'].get('archive_policy', self.policy) == self.policy, 'fixed project archive policy differs')
        archive, active = (canonical(self.policy[k]) for k in ('archive_root', 'spec_root'))
        require(not overlaps(archive, active), 'active spec and archive roots overlap')
        roots = [canonical(self.runtime.project_root), canonical(state['runtime']['cleanup_root'])]
        roots += [canonical(t['worktree']) for t in state['tasks'].values() if t.get('worktree')]
        for root in roots:
            require(not overlaps(archive, root), 'archive root must be outside project and managed cleanup trees')
            require(not overlaps(active, root), 'active specs must be outside project and managed cleanup trees')
        for resource in state['runtime'].get('resources', {}).values():
            if resource.get('path'):
                require(not overlaps(archive, canonical(resource['path'])), 'archive overlaps a registered resource')
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', key) is not None, 'safe task directory id required')
        return task

    def _end(self, state, key, task):
        merge = task.get('merge') or {}
        if task['phase']=='completed':
            version = task.get('source_version') or task.get('mr') or {}
            require(merge.get('verified') is True and merge.get('main_sha') and
                merge.get('head_sha') == version.get('head_sha') and
                merge.get('target_sha') == version.get('target_sha'), 'verified merged main/source required')
            observation = merge.get('observation')
            if observation is None:
                # Existing record_main_inclusion / real_use completion: one exact
                # stored proof, never its evidence/report dependency graph.
                require(merge.get('proof'), 'registered actual merge observation required')
                observation = load_bound(merge['proof'])
            require(observation.get('project_id')==self.runtime.project_id and observation.get('task_id')==key and
                observation.get('source_head')==merge['head_sha'] and observation.get('previous_main')==merge['target_sha'] and
                observation.get('main_head')==merge['main_sha'] and observation.get('contains_source') is True and
                observation.get('scope') in ('host_verified','fixture_only'), 'registered actual merge observation binding differs')
            return 'completed', deepcopy(observation)
        require(task.get('cancellation_request') and not merge and not state.get('merge_reservation'),
                'normal archive requires completed merge; cancellation requires actual decision and resolved merge')
        require(callable(self.observe_stop), 'trusted actual stop observer required')
        stopped = self.observe_stop(deepcopy(task))  # outside STATE writer lock
        require(stopped.get('project_id')==self.runtime.project_id and stopped.get('task_id')==key and
            stopped.get('owner_agent_id')==task['owner_agent_id'] and stopped.get('stopped') is True and
            stopped.get('merge_status')=='not_merged' and stopped.get('scope') in ('host_verified','fixture_only'),
                'actual task stop and not-merged observation required')
        return 'cancelled', deepcopy(stopped)

    def _protect(self, state, key, path, kind):
        require(not any(part.casefold()=='.git' for part in path.parts), 'Git administrative files cannot be archived')
        for protected in state['project'].get('protected', []):
            require(not path.is_relative_to(canonical(protected['path'])), 'protected project file')
        for other_key, other in state['tasks'].items():
            if other_key == key:
                continue
            requirement_id = other.get('routing', {}).get('requirement_id')
            contract = other.get('state_contract') or state['runtime'].get('intake', {}).get('requirements', {}).get(requirement_id, {})
            for item in [contract.get('current_spec', contract.get('spec')), *contract.get('deliverables', [])]:
                require(not item or path != canonical(item['path']), 'file belongs to another task')
            require(not other.get('worktree') or not path.is_relative_to(canonical(other['worktree'])), 'other task worktree')
        for resource in state['runtime'].get('resources', {}).values():
            if resource.get('path') and path.is_relative_to(canonical(resource['path'])):
                require(resource['task_id']==key and not resource.get('protected') and
                    not resource.get('shared') and set(resource.get('references', [])) <= {key} and
                    not resource.get('customer_in_use') and not resource.get('in_use'), 'shared/protected/in-use resource')
        if kind=='spec':
            require(path.is_relative_to(canonical(self.policy['spec_root'])), 'spec outside fixed project materials root')
        else:
            task = state['tasks'][key]
            owned = [canonical(task['worktree'])] if task.get('worktree') else []
            owned += [canonical(r['path']) for r in state['runtime'].get('resources', {}).values()
                      if r.get('path') and r['task_id']==key]
            require(any(path.is_relative_to(root) for root in owned), 'deliverable outside task-owned resources')
        # Git-tracked material stays in Git even if a manifest accidentally lists it.
        for parent in (path.parent, *path.parent.parents):
            if io_path(parent/'.git').exists():
                relative = path.relative_to(parent).as_posix()
                require(not git(parent, '-c', 'core.longpaths=true', 'ls-files', '--', relative), 'Git-tracked file cannot be archived')
                break

    def _plan(self, state, key, operation_id, task, mode, end):
        contract = archive_manifest(state, key); source = contract['spec']
        extras = contract.get('deliverables', []) if mode=='completed' else []
        files = []
        directory = canonical(self.policy['archive_root'])/('task-'+key)
        for index, item in enumerate([source, *extras]):
            require(set(item)=={'path','sha256'} and re.fullmatch('[0-9a-fA-F]{64}', item['sha256']), 'exact confirmed file reference required')
            path = canonical(item['path']); kind = 'spec' if index==0 else 'deliverable'
            require(':' not in path.name and path != directory, 'safe file name required')
            self._protect(state, key, path, kind)
            require(file_fact(path, item['sha256'])=='matching', 'confirmed original missing or changed')
            destination = directory/('spec' if index==0 else 'deliverables')/((f'{index-1:03d}-' if index else '')+path.name)
            canonical(destination)
            require(not io_path(destination).exists(), 'archive destination already exists; no overwrite or adoption without intent')
            require(not any(row['original']['path']==str(path) for row in files), 'duplicate manifest file')
            files.append(dict(kind=kind, index=index-1, original=dict(path=str(path),sha256=item['sha256']),
                destination=dict(path=str(destination),sha256=item['sha256'])))
        return dict(project_id=self.runtime.project_id, task_id=key, operation_id=operation_id,
            policy=deepcopy(self.policy), mode=mode, end_observation=end, binding=archive_binding(task), files=files)

    def _admit(self, state, key, operation_id):
        require(isinstance(operation_id, str) and operation_id.strip(), 'stable original operation id required')
        task = self._context(state, key)
        old = state['runtime']['operations'].get(operation_id)
        archive = task.get('archive')
        require(not archive or archive['operation_id']==operation_id, 'resume original archive operation; no replacement id')
        if old:
            require(old['operation']=='archive' and old['task_id']==key and old['owner']==self.actor, 'operation identity differs')
            plan = deepcopy(old['plan'])
            require(plan['binding']==archive_binding(task) and plan['policy']==self.policy, 'archive task/source/policy binding changed')
            contract = archive_manifest(state, key)
            expected = 1+(len(contract.get('deliverables', [])) if plan['mode']=='completed' else 0)
            require(len(plan['files'])==expected, 'confirmed archive manifest changed')
            for item in plan['files']:
                current = contract['spec'] if item['kind']=='spec' else contract['deliverables'][item['index']]
                require(current in (item['original'], item['destination']), 'confirmed archive file changed')
        else:
            mode, end = self._end(state, key, task)
            plan = self._plan(state, key, operation_id, task, mode, end)
        require(self.authorize(deepcopy(task), self.actor, deepcopy(plan)) is True, 'actual host archive authorization denied')
        if old and plan['mode']=='cancelled':
            self._end(state, key, task)  # cancellation is not a permanent claim that all writers stopped
        for item in plan['files']:
            self._protect(state, key, canonical(item['original']['path']), item['kind'])
            canonical(item['destination']['path'])
        return task, plan, old

    def prepare(self, task_id, operation_id, expected_revision):
        """Persist exact intent without moving; also the manual takeover plan."""
        state = self.runtime._load(); task, plan, old = self._admit(state, task_id, operation_id)
        if old:
            return dict(plan=plan, state_registration=dict(status='recorded',revision=state['revision']), duplicate=True)
        registration = record_archive(self.runtime, _ARCHIVE, self.actor, task_id, operation_id,
            expected_revision, plan=plan)
        return dict(plan=plan, state_registration=registration)

    def _observe(self, plan):
        files = []
        for item in plan['files']:
            row = deepcopy(item)
            try:
                source = file_fact(item['original']['path'], item['original']['sha256'])
                destination = file_fact(item['destination']['path'], item['destination']['sha256'])
                row.update(source_fact=source, destination_fact=destination,
                    status=('archived' if source=='missing' and destination=='matching' else
                            'pending' if source=='matching' and destination=='missing' else 'needs_reconcile'))
            except (OSError, ValueError) as error:
                row.update(status='needs_reconcile', reason=str(error))
            row['current'] = (deepcopy(item['destination']) if row['status']=='archived' else
                              deepcopy(item['original']) if row['status']=='pending' else None)
            files.append(row)
        done = [row['original']['path'] for row in files if row['status']=='archived']
        pending = [row['original']['path'] for row in files if row['status']!='archived']
        protected = {str(Path(row['original']['path']).parent) for row in files if row['status']!='archived'}
        if any(row.get('destination_fact') not in ('missing','matching') or row['status']=='needs_reconcile' for row in files):
            protected.add(str(Path(plan['policy']['archive_root'])/('task-'+plan['task_id'])))
        status = 'completed' if not pending else 'needs_reconcile' if any(row['status']=='needs_reconcile' for row in files) else 'partial' if done else 'pending'
        return dict(schema='task-archive/1',project_id=plan['project_id'],task_id=plan['task_id'],
            operation_id=plan['operation_id'],mode=plan['mode'],status=status,files=files,
            admission=dict(status='admitted',basis='merged_main_verified' if plan['mode']=='completed' else 'cancel_and_stop_verified',
                           scope=plan['end_observation']['scope']),
            completed=done,pending=pending,protected_directories=sorted(protected))

    def _run(self, task_id, operation_id, expected_revision, move):
        # A finite per-operation file lock serializes our own moves, never the project ledger.
        lock = self.runtime.directory/'archive-operations'/fingerprint([task_id, operation_id])
        with writer_lock(io_path(lock)):
            state = self.runtime._load(); _, plan, old = self._admit(state, task_id, operation_id)
            if state['revision'] != expected_revision:
                return dict(status='conflict',revision=state['revision'],next_action='read same task and resume original operation')
            if not old:
                require(move, 'prepare intent before observing manual results')
                registration = record_archive(self.runtime, _ARCHIVE, self.actor, task_id, operation_id, expected_revision, plan=plan)
                if registration['status']!='recorded':
                    return dict(status='not_started',plan=plan,state_registration=registration)
                expected_revision = registration['revision']
            observed = self._observe(plan)
            if move:
                for row in observed['files']:
                    if row['status']!='pending':
                        continue
                    # Recheck authorization/protection/revision before each external move.
                    current = self.runtime._load(); self._admit(current, task_id, operation_id)
                    if self.runtime._load()['revision'] != expected_revision:
                        break
                    try:
                        source, target = row['original'], row['destination']
                        canonical(target['path'])
                        io_path(target['path']).parent.mkdir(parents=True, exist_ok=True)
                        require(file_fact(source['path'],source['sha256'])=='matching', 'source changed before move')
                        canonical(target['path'])
                        move_original(source['path'], target['path'])
                    except (OSError, ValueError) as error:
                        row['move_error'] = type(error).__name__+': '+str(error)
                        break
                actual = self._observe(plan)
                for row, previous in zip(actual['files'], observed['files']):
                    if previous.get('move_error'): row['move_error']=previous['move_error']
                observed = actual
            try:
                registration = record_archive(self.runtime, _ARCHIVE, self.actor, task_id, operation_id,
                    expected_revision, result=observed)
            except (OSError, ValueError) as error:
                registration = dict(status='registration_pending',reason=str(error),next_action='observe original operation')
            return dict(observed,state_registration=registration,next_owner=self.actor,
                next_action='none' if observed['status']=='completed' and registration['status']=='recorded' else 'observe/resume original operation or manual takeover')

    def execute(self, task_id, operation_id, expected_revision):
        return self._run(task_id, operation_id, expected_revision, True)

    def observe(self, task_id, operation_id, expected_revision):
        """Observe and register normal-file-tool takeover; never executes a move."""
        return self._run(task_id, operation_id, expected_revision, False)

    def query(self, task_id):
        """Read the last registered minimal result; no reports, tests or file probes."""
        state = self.runtime._load()
        require(self.actor == receiver(state, task_id) and self.actor != state['tasks'][task_id]['owner_agent_id'],
                'designated independent integrator required for archive')
        return dict(project_id=self.runtime.project_id,task_id=task_id,revision=state['revision'],
            archive=deepcopy(state['tasks'][task_id].get('archive',dict(status='not_registered'))),
            freshness='last_registered',host_verified_now=False)
