// Finite installed host driver. No queue, daemon, implicit authority or task-selected code.
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {readFile, mkdir, open, link, unlink} from 'node:fs/promises';
import {createHash, randomUUID} from 'node:crypto';
import {DispatchSession, serializeHostError} from './dispatch_host.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const stable = value => JSON.stringify(value,(_,item) => item && typeof item === 'object' && !Array.isArray(item) ?
  Object.fromEntries(Object.keys(item).sort().map(key => [key,item[key]])) : item);
const allowed = new Set(['multi_agent_v1__close_agent','multi_agent_v1__wait_agent','mcp__codex_app__read_thread']);

async function bound(reference) {
  const bytes=await readFile(reference.path);
  if (hash(bytes)!==reference.sha256) throw new Error('bound continuation evidence changed');
  return JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
}

export class ContinuationHost {
  constructor({python,profile,requestMeta,availableTools,scope='host_verified',permissions={},successorOrigin=null}) {
    if (!path.isAbsolute(python) || !requestMeta?.threadId || !['fixture_only','host_verified'].includes(scope) ||
        !['state_root','project_root','resource_state','evidence_root'].every(k => path.isAbsolute(profile?.[k] ?? ''))) {
      throw new Error('fixed absolute host profile and actual nodeRepl.requestMeta required');
    }
    this.python=python;this.profile={...profile};this.requestMeta=requestMeta;
    this.host={actor_id:requestMeta.threadId,scope,available_tools:[...availableTools],
      reclaim_authorized:permissions.reclaim===true,install_authorized:permissions.install===true,
      successor_authorized:permissions.successor===true,successor_origin:successorOrigin};
  }

  async endpoint(action,fields) {
    const endpoint=path.toNamespacedPath(fileURLToPath(new URL('./continuation_endpoint.py',import.meta.url)));
    return await new Promise((resolve,reject) => {
      const child=spawn(this.python,['-X','utf8','-B',endpoint],{cwd:this.profile.project_root,
        windowsHide:true,stdio:['pipe','pipe','pipe']});
      let output='',error='';
      const timer=setTimeout(() => child.kill(),45000);
      child.stdout.on('data',data => {output+=data;});
      child.stderr.on('data',data => {error=(error+data).slice(-2000);});
      child.stdin.on('error',()=>{});
      child.on('error',failure => {clearTimeout(timer);reject(failure);});
      child.on('exit',code => {
        clearTimeout(timer);
        try {resolve(JSON.parse(output));}
        catch {resolve({status:'needs_reconcile',reason:'Finite continuation exited before result',exit_code:code,
          error,automatic_retry:false,next_action:'Preserve and recover only confirmed-dead call locks; continue original request'});}
      });
      child.stdin.end(JSON.stringify({protocol:'delivery-continuation-host/1',action,profile:this.profile,host:this.host,...fields})+'\n');
    });
  }

  async save(key,value) {
    const bytes=JSON.stringify(value,null,2)+'\n';
    await mkdir(this.profile.evidence_root,{recursive:true});
    const target=path.join(this.profile.evidence_root,'continuation-'+hash(key)+'.json');
    const temporary=target+'.'+randomUUID()+'.tmp';
    const file=await open(temporary,'wx');
    try {await file.writeFile(bytes);await file.sync();} finally {await file.close();}
    try {
      try {await link(temporary,target);}
      catch (error) {
        if (error.code!=='EEXIST') throw error;
        if (stable(JSON.parse(await readFile(target,'utf8')))!==stable(value)) throw new Error('conflicting original result; retain both observations');
      }
    } finally {await unlink(temporary);}
    return {path:target,sha256:hash(await readFile(target))};
  }

  async existing(key) {
    const target=path.join(this.profile.evidence_root,'continuation-'+hash(key)+'.json');
    try {const bytes=await readFile(target);return {path:target,sha256:hash(bytes)};}
    catch(error) {if(error.code!=='ENOENT') throw error;return null;}
  }

  async run(request,{inputs={},invoke=null,update=null,maxRounds=4}={}) {
    if (!Number.isInteger(maxRounds) || maxRounds<1 || maxRounds>8) throw new Error('bounded host rounds required');
    await bound(request);
    let result;
    const handled=new Set();
    for (let i=0;i<maxRounds;i++) {
      result=await this.endpoint('run',{request,inputs});
      const packet=result.host_package;
      if (!packet || stable(packet.request)!==stable(request)) return result;
      if (packet.required_actor && packet.required_actor!==this.host.actor_id) return result;
      if (handled.has(packet.action)) return result; // one observation per fact, never poll an uncertain outcome
      handled.add(packet.action);
      if (packet.action==='native_close') {
        if (!this.host.reclaim_authorized || !invoke || !['multi_agent_v1__close_agent','multi_agent_v1__wait_agent'].every(t=>this.host.available_tools.includes(t))) return result;
        const claimed=await this.endpoint('claim-native',{request,operation_id:packet.operation_id});
        if (!('dispatch_allowed' in claimed)) return claimed;
        const plan=await bound(packet.native_request);
        if (plan.operation_id!==packet.operation_id || plan.task_id!==packet.task_id ||
            plan.controller_id!==this.host.actor_id || plan.close.tool!=='multi_agent_v1__close_agent' ||
            plan.wait.tool!=='multi_agent_v1__wait_agent') throw new Error('native package identity differs');
        const key=stable([packet.native_request,'close']);
        let closed=await this.existing(key);
        if (!closed && !claimed.dispatch_allowed) return {...result,status:'needs_reconcile',
          next_action:'Original close was claimed; return its raw result without reissuing'};
        if (!closed) {
          let raw;
          try {raw=await invoke(plan.close.tool,plan.close.arguments);} catch(error) {raw={error:serializeHostError(error)};}
          closed=await this.save(key,raw);
        }
        let raw;
        try {raw=await invoke(plan.wait.tool,plan.wait.arguments);} catch(error) {raw={error:serializeHostError(error)};}
        const waited=await this.save(stable([packet.native_request,'wait',raw]),raw);
        inputs={...inputs,native_observation:await this.save(stable([packet.native_request,closed,waited]),{
          operation_id:plan.operation_id,executor_id:plan.executor_id,generation:plan.generation,
          controller_id:this.host.actor_id,request:packet.native_request,
          close:{...plan.close,result:closed},wait:{...plan.wait,result:waited}})};
      } else if (packet.action==='observe') {
        const plan=packet.plan,call=plan.host_call;
        if (!invoke || !allowed.has(call.tool) || !this.host.available_tools.includes(call.tool)) return result;
        const raw=await invoke(call.tool,call.arguments);
        inputs={...inputs,observation_ref:await this.save(stable([plan.binding,call,raw]),{
          schema:'task-handoff-observation/1',binding:plan.binding,tool:call.tool,arguments:call.arguments,result:raw})};
        if (plan.binding.recipient_agent_id!==this.host.actor_id) return {...result,observation:inputs.observation_ref,
          next_owner:plan.binding.recipient_agent_id,next_action:'Approved integration receiver continues the same request with this observation'};
      } else if (packet.action==='installation') {
        if (!this.host.install_authorized || typeof update!=='function') return result;
        const claimed=await this.endpoint('claim-install',{request});
        if (!claimed.dispatch_allowed) return {...result,status:'needs_reconcile',next_action:'Inspect original installation intent; do not reinstall'};
        inputs={...inputs,installation_proof:await update(claimed.plan)};
      } else if (packet.action==='dispatch') {
        if (!invoke || !this.host.successor_authorized || !this.host.successor_origin) return result;
        const session=new DispatchSession({python:this.python,profile:this.profile,request:packet.successor,
          requestMeta:this.requestMeta,approvalOrigin:this.host.successor_origin,
          availableTools:this.host.available_tools,testingScope:this.host.scope});
        let dispatched=await session.next();
        while (dispatched.kind==='host_call') {
          const call=dispatched;
          let raw,error=null;
          try {raw=await invoke(call.tool,call.arguments);} catch(failure) {error=serializeHostError(failure);}
          dispatched=await session.reply(call.tool,raw,error);
        }
        await session.exited;
        if (dispatched.result.status!=='dispatched') return {...result,successor_result:dispatched.result};
      } else return result;
    }
    return {...result,status:'bounded',next_action:'Continue the original request; no background execution is running'};
  }

  async inspectLocks(request) {return this.endpoint('inspect-locks',{request});}
  async recoverLocks(request,locks,destination) {
    if (locks===null) locks=(await this.inspectLocks(request)).locks;
    return this.endpoint('recover-locks',{request,locks,destination});
  }

  async captureNativeResult(request,nativeRequest,{close,wait}) {
    const task=await bound(request),plan=await bound(nativeRequest);
    if (plan.task_id!==task.task_id || plan.project_id!==task.project_id || plan.controller_id!==this.host.actor_id) {
      throw new Error('late result must retain original native request and controller');
    }
    const closed=await this.save(stable([nativeRequest,'late-close',close]),close);
    const waited=await this.save(stable([nativeRequest,'wait',wait]),wait);
    return this.save(stable([nativeRequest,closed,waited]),{operation_id:plan.operation_id,executor_id:plan.executor_id,
      generation:plan.generation,controller_id:this.host.actor_id,request:nativeRequest,
      close:{...plan.close,result:closed},wait:{...plan.wait,result:waited}});
  }
}
