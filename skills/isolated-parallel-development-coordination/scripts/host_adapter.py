"""Finite calls through host-owned callbacks; no credentials, hooks or subprocesses."""
import json
from evidence import require, fingerprint

APP = {
    'send': 'mcp__codex_app__send_message_to_thread',
    'observe': 'mcp__codex_app__read_thread',
}
NATIVE = {'send': 'send_input', 'observe': 'wait_agent', 'spawn': 'spawn_agent',
          'close': 'close_agent', 'resume': 'resume_agent'}


class HostCallError(RuntimeError):
    def __init__(self, detail):
        self.detail = detail if isinstance(detail, dict) else dict(message=str(detail))
        super().__init__(self.detail.get('message') or str(detail))


def error_detail(error):
    if isinstance(error, HostCallError):
        return error.detail
    return dict(name=type(error).__name__, message=str(error), code=getattr(error, 'code', None))


def capacity_error(detail):
    return detail.get('code') in ('agent_capacity_exceeded', 'max_agents') or \
        'agent thread limit reached' in str(detail.get('message', '')).lower()


def spawn_identity(value):
    """An error cannot erase a structured host creation identity."""
    values = [value]
    if isinstance(value.get('raw'), dict): values.append(value['raw'])
    for candidate in list(values):
        for block in candidate.get('content', []):
            if block.get('type') != 'text': continue
            try: decoded = json.loads(block['text'])
            except (ValueError, KeyError): continue
            if isinstance(decoded, dict): values.append(decoded)
    ids = {row[key] for row in values for key in ('agent_id', 'id') if row.get(key)}
    require(len(ids) <= 1, 'host returned conflicting creation identities')
    return next(iter(ids), None)


def unwrap(result):
    if isinstance(result, dict) and 'content' in result:
        require(not result.get('isError'), 'host tool rejected operation')
        blocks = [b['text'] for b in result['content'] if b.get('type') == 'text']
        require(len(blocks) == 1, 'host response has no unique structured result')
        return json.loads(blocks[0])
    require(isinstance(result, dict), 'host response must be an object')
    return result


class HostAdapter:
    """invoke(tool_name, args) and actor_id come from the trusted host, not a report.

    The callback may return normalized operation_succeeded only when the actual
    host API documents that success. Mere completed/idle status never means close.
    """
    def __init__(self, channel, available_tools, invoke, actor_id, *, scope='fixture_only'):
        require(channel in ('app_thread', 'native_agent'), 'unknown host channel')
        self.channel, self.tools, self.invoke, self.actor_id = channel, set(available_tools), invoke, actor_id
        self.names = APP if channel == 'app_thread' else NATIVE
        require(scope in ('fixture_only','host_verified'), 'explicit host testing boundary required')
        self.scope = scope

    def capabilities(self):
        return dict(channel=self.channel, existing_message=self.names['send'] in self.tools,
                    observation=self.names['observe'] in self.tools,
                    spawn=self.names.get('spawn') in self.tools,
                    close=self.names.get('close') in self.tools,
                    resume=self.names.get('resume') in self.tools, idle_wakeup=False)

    def call(self, operation, *, target=None, message=None, authorized=False):
        require(authorized is True, 'host action requires current scoped authorization')
        tool = self.names.get(operation)
        require(tool and tool in self.tools, 'host capability unavailable: '+operation)
        if operation == 'spawn':
            require(self.channel == 'native_agent', 'App channel cannot create a chat')
            args = {'message': message}
        elif self.channel == 'app_thread':
            require(target, 'existing thread target required')
            args = {'threadId': target, 'prompt': message} if operation == 'send' else {
                'threadId': target, 'turnLimit': 1, 'includeOutputs': False, 'maxOutputCharsPerItem': 1000}
        else:
            require(target, 'registered native target required')
            args = {'id': target}
            if operation == 'send':
                args.update(message=message, interrupt=False)
            if operation == 'observe':
                args = {'ids': [target], 'timeout_ms': 0}
        try:
            invoked = self.invoke(tool, args)
            if isinstance(invoked, dict) and invoked.get('isError'):
                texts = [b['text'] for b in invoked.get('content', []) if b.get('type') == 'text']
                raise HostCallError(dict(message='\n'.join(texts), raw=invoked))
            raw = unwrap(invoked)
        except Exception as error:
            detail = error_detail(error)
            returned = spawn_identity(detail) if operation == 'spawn' else None
            if returned:
                return dict(status='accepted', target_id=returned, tool=tool, error=detail,
                            channel=self.channel, response_digest=fingerprint(detail))
            if operation == 'spawn' and capacity_error(detail):
                return dict(status='capacity', code='agent_capacity_exceeded', tool=tool, error=detail,
                            reason=detail.get('message'), response_digest=fingerprint(detail))
            raise
        # Only structural host facts are retained; never persist session text.
        returned = raw.get('threadId',raw.get('thread',{}).get('id')) if self.channel == 'app_thread' else spawn_identity(raw)
        if raw.get('error'):
            error = raw['error']
            detail = error if isinstance(error, dict) else dict(message=str(error))
            if operation == 'spawn' and returned:
                return dict(status='accepted', target_id=returned, tool=tool, error=detail,
                            channel=self.channel, response_digest=fingerprint(raw))
            capacity = operation == 'spawn' and capacity_error(detail)
            return dict(status='capacity' if capacity else 'failed',
                        code='agent_capacity_exceeded' if capacity else detail.get('code') or 'host_error',
                        error=detail, reason=detail.get('message') or detail.get('code') or 'host_error',
                        tool=tool, response_digest=fingerprint(raw))
        if returned is not None and target is not None:
            require(returned == target, 'host returned a different execution identity')
        result = dict(status='accepted', target_id=returned or target, tool=tool,
                      response_digest=fingerprint(raw), channel=self.channel)
        if operation == 'observe':
            if self.channel == 'app_thread':
                turn = raw.get('latestTurn') or raw.get('latest_turn') or next(iter(raw.get('turns',[])),None)
                status = turn.get('status') if turn else None
                result['execution'] = 'running' if status in ('inProgress', 'in_progress') else (
                    'completed' if status == 'completed' else 'unknown')
            else:
                status = raw.get('status', {})
                status = status.get(target, status) if isinstance(status, dict) else status
                result['execution'] = 'running' if status == 'running' else (
                    'completed' if isinstance(status, dict) and 'completed' in status or status == 'completed' else 'unknown')
        elif operation == 'close':
            result['status'] = 'closed' if raw.get('closed') is True or raw.get('operation_succeeded') is True else 'unknown'
        elif operation == 'resume':
            result['status'] = 'resumed' if raw.get('resumed') is True or raw.get('operation_succeeded') is True else 'unknown'
        elif operation == 'spawn':
            require(returned, 'spawn returned no actual executor identity')
        return result

    def final_receipt(self, state, task_id, payload, sender_target, recipient, proof_ref):
        """Called with authenticated host sideband, never sender claims in payload."""
        from coordination import receipt_context, receipt_report
        task = state['tasks'][task_id]
        receiver = state.get('delivery', {}).get('agent_id', state['coordinator_id'])
        require(recipient == receiver == self.actor_id, 'final receiver is not current authorized recipient')
        owner = task['owner_agent_id']
        binding = state['runtime']['executors'][owner]['routes'].get(self.channel)
        require(binding and binding['target_id'] == sender_target, 'final sender differs from registered host target')
        require(payload.get('task_id') == task_id and payload.get('owner_agent_id') == owner and
                payload.get('recipient_agent_id', receiver) == receiver, 'final payload identity mismatch')
        result = dict(payload, channel=self.channel, recipient_agent_id=receiver, transport_evidence=proof_ref)
        receipt_context(state, result)
        receipt_report(state, result)
        return result
