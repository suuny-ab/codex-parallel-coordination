"""Task-scoped, approved capture boundaries; file contents never grant exemptions."""
from copy import deepcopy
from pathlib import Path
import re

from coordination import writer_lock
from evidence import artifact, load_bound, require

CONTRACTS = {'frozen-test-result/1', 'frozen-baseline/1', 'frozen-host-message/1'}
# A capture can be supplied as evidence or an explicit extra. These slots must
# continue to prove live authority/delivery even if the same bytes are also evidence.
_EVIDENCE_SLOTS = {'evidence', 'artifacts', 'extra', 'probe_evidence', 'raw_journals'}
CAPTURE_SLOTS = {contract: _EVIDENCE_SLOTS | slots for contract, slots in (
    ('frozen-test-result/1', {'result', 'probe'}),
    ('frozen-baseline/1', {'real_task_evidence'}),
    ('frozen-host-message/1', set()))}


def binding(state, task_id):
    task = state['tasks'][task_id]
    return dict(project_id=state['project']['id'], task_id=task_id,
                delivery_revision=task['delivery_revision'], delivery=task['delivery'])


def required_references(state, task_id):
    """Canonical credentials stay required even when their leaf slot is evidence."""
    task = state['tasks'][task_id]
    values = [task.get('delivery'), task.get('resource_authority')]
    for key in ('review','independent_quality','technical_acceptance','merge','development_binding'):
        packet = task.get(key) or {}
        values.append(packet)
        values.extend(packet.get(field) for field in ('evidence','review','report','proof','approval','spec'))
    executor = state['runtime']['executors'][task['owner_agent_id']]
    values.extend(executor.get(key) for key in ('authority','observation'))
    return [dict(path=row['path'],sha256=row['sha256']) for value in values
            for row in (value if isinstance(value,list) else [value])
            if isinstance(row,dict) and 'path' in row and 'sha256' in row]


def approved(state, task_id, record):
    """Recheck the original approval and exact current task identity on every use."""
    from cleanup import canonical_path
    task = state['tasks'][task_id]
    from real_use_closeout import approval
    authority = record['authority']
    require(authority and authority==task.get('resource_authority'), 'original task classification authority changed')
    original_approval = load_bound(authority)
    integration_actor = original_approval.get('integration_agent')
    original_approval = approval(state, task_id, authority, integration_actor)
    require(record.get('actor_id') in (state['coordinator_id'], integration_actor) and
        record.get('actor_id')!=task['owner_agent_id'] and
        {'preserve_and_reclaim_task_owned_resources_after_completion','bounded_new_task_resource_cleanup'} &
        set(original_approval.get('authorized', [])), 'existing task preservation authority required')
    declaration = load_bound(record['declaration'])
    expected = binding(state, task_id)
    require(record.get('binding')==expected and all(declaration.get(k)==v for k,v in expected.items()),
            'preservation classification identity/revision changed')
    require(declaration.get('schema')=='preservation-classification/1' and
        record.get('captures')==declaration.get('captures') and record.get('captures'),
            'preservation classification differs from declaration')
    roots = [state['project']['root'], state['runtime']['cleanup_root'], task['worktree']]
    roots += [r['path'] for r in state['runtime'].get('resources', {}).values() if r.get('path')]
    for reference in (authority, record['declaration']):
        require(all(not canonical_path(reference['path']).is_relative_to(canonical_path(root)) for root in roots),
                'classification authority/declaration must outlive resources')
    seen = set()
    required = required_references(state, task_id)
    for row in record['captures']:
        original = row.get('original', {})
        require(set(original)=={'path','sha256'} and Path(original['path']).is_absolute() and
            re.fullmatch('[0-9a-f]{64}', original['sha256']) and row.get('contract') in CONTRACTS and
            row.get('reason'), 'explicit capture contract/path/SHA/reason required')
        require(original not in required, 'required canonical credential cannot be classified as capture')
        token = (original['path'], original['sha256'])
        require(token not in seen, 'duplicate capture classification')
        seen.add(token)
    previous = record.get('previous')
    if previous:
        old = approved(state, task_id, previous)
        require(declaration.get('previous')==previous['declaration'] and declaration.get('reason') and
            record['authority']==previous['authority'] and record['binding']==previous['binding'] and
            len(record['captures'])>len(old) and all(row in record['captures'] for row in old),
            'classification revision must retain every original capture and binding')
    else:
        require(not declaration.get('previous'), 'classification revision history missing')
    return record['captures']


def classify(runtime, task_id, declaration_ref, actor_id, *, authorized=False, previous_declaration=None):
    require(authorized is True, 'explicit capture classification authorization required')
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._scope(state, task_id)
        task = state['tasks'][task_id]
        require(task['phase']=='completed' and task.get('merge', {}).get('verified'),
                'classify a verified completed task delivery')
        require(not task.get('preservation') and not any(o.get('task_id')==task_id and
            o.get('operation')=='close' for o in state['runtime']['operations'].values()),
            'classify before preservation/close intent')
        declaration = load_bound(declaration_ref)
        record = dict(binding=binding(state, task_id), authority=task.get('resource_authority'),
                      declaration=declaration_ref, actor_id=actor_id, captures=declaration.get('captures'))
        previous = task.get('preservation_classification')
        if previous and previous['declaration']==declaration_ref:
            require(previous['actor_id']==actor_id, 'classification caller changed')
            for row in approved(state, task_id, previous): artifact(row['original'])
            return deepcopy(previous)
        if previous:
            require(previous_declaration==previous['declaration'] and
                declaration.get('previous')==previous_declaration,
                'original classification cannot be replaced without its exact predecessor')
            record['previous'] = deepcopy(previous)
        else:
            require(previous_declaration is None, 'original classification predecessor missing')
        approved(state, task_id, record)
        for row in record['captures']: artifact(row['original'])
        task['preservation_classification'] = record
        runtime._save(state, dict(op='classify-preservation', task_id=task_id, declaration=declaration_ref))
        return deepcopy(record)


def captures(state, task_id):
    record = state['tasks'][task_id].get('preservation_classification')
    return approved(state, task_id, record) if record else []


def extends(record, previous):
    """The original immutable classification remains an ancestor of each revision."""
    if previous is None: return bool(record)
    while record:
        if record==previous: return True
        record = record.get('previous')
    return False


def verify_inventory(state, task_id, inventory):
    """Shared by preserved reads, eligibility, and native close verification."""
    record = state['tasks'][task_id].get('preservation_classification')
    require(inventory.get('classification')==record, 'preserved classification binding changed')
    expected = captures(state, task_id)
    if inventory.get('classification_intent'):
        addition = load_bound(inventory['classification_intent'])
        prior = load_bound(addition['original_intent'])
        previous_binding = dict(inventory['binding'])
        old_classification = prior['binding'].get('preservation_classification')
        require(extends(record, old_classification), 'original classification history changed')
        if old_classification is None: previous_binding.pop('preservation_classification', None)
        else: previous_binding['preservation_classification'] = old_classification
        require(record and addition.get('binding')==inventory['binding'] and
            prior.get('binding')==previous_binding and all(addition.get(key)==prior.get(key)
            for key in ('project_id','task_id','operation_id','destination','handoff','scope','merge_verification','extra')),
            'preserved original classification intent changed')
    actual = [dict(original=row['original'], **row['capture']) for row in inventory['files'] if row.get('capture')]
    require(sorted(actual, key=lambda r:r['original']['path'])==sorted(expected, key=lambda r:r['original']['path']),
            'preserved capture classification coverage differs')
    for row in inventory['files']:
        if row.get('capture'):
            require(row['sha256']==row['original']['sha256'], 'preserved capture bytes differ')
            artifact(dict(path=row['path'], sha256=row['sha256']))
            from cleanup import canonical_path
            from evidence import io_path
            original = row['original']
            resources = [r for r in state['runtime'].get('resources', {}).values() if
                r.get('task_id')==task_id and r.get('path') and
                canonical_path(original['path']).is_relative_to(canonical_path(r['path']))]
            durable = inventory.get('remote_verification') and state['tasks'][task_id].get('preservation')
            if not (durable and resources):
                if io_path(original['path']).exists(): artifact(original)
                else:
                    cleaned = [r for r in resources if r.get('cleanup_status')=='cleaned']
                    require(cleaned, 'original capture disappeared outside confirmed cleanup')
                    for resource in cleaned: artifact(resource['cleanup_result']['evidence'])
