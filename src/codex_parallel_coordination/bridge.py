"""Connect canonical CORE decisions to RESOURCE receipts without a second ledger."""
from pathlib import Path
import uuid

from .resources.support import fingerprint, load, load_bound, ref, write_atomic


class ResourceBridge:
    def __init__(self, runtime, controller, artifact_cleanup, evidence_root):
        self.runtime = runtime
        self.controller = controller
        self.artifacts = artifact_cleanup
        self.evidence = Path(evidence_root)
        if not self.evidence.is_absolute():
            raise ValueError("explicit absolute external evidence root required")
        if self.evidence.resolve().is_relative_to(runtime.project_root.resolve()):
            raise ValueError("evidence must outlive project checkout cleanup")
        _, policy = controller.read()
        for roots in policy.get("cleanup_roots", {}).values():
            if any(self.evidence.resolve().is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError("evidence cannot reside in a managed cleanup subtree")

    def _external(self, resource):
        if resource.get("path") and self.evidence.resolve().is_relative_to(Path(resource["path"]).resolve()):
            raise ValueError("cleanup evidence must outlive the resource")

    def _record(self, result):
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / ("cleanup-" + uuid.uuid4().hex + ".json")
        write_atomic(path, result)
        return ref(path)

    def admit(self, spec_ref):
        """Reserve actual RESOURCE quota and bind a CORE admission artifact."""
        spec = load_bound(spec_ref)
        record = self.controller.capacity_acquire(spec_ref["path"])
        if record["spec"] != spec:
            raise ValueError("capacity source changed during admission; reconcile reserved quota")
        if record["status"] != "granted":
            return {"status": record["status"], "request_id": spec["request_id"], "admission": None}
        value = dict(spec, status="granted", permit_id=record["permit_id"], resource_source=spec_ref)
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / ("admission-" + fingerprint(value) + ".json")
        if path.exists():
            if load(path) != value:
                raise ValueError("immutable admission evidence changed")
        else:
            write_atomic(path, value)
        return {"status": "granted", "request_id": spec["request_id"], "admission": ref(path)}

    def cleanup(self, resource):
        """Pass as Runtime.cleanup_task's trusted callback, never as JSON authority."""
        self._external(resource)
        # A fresh authorized CORE round is the explicit retry request. RESOURCE
        # retries only failed_no_change; unknown outcomes remain observe-only.
        result = self.artifacts.cleanup(resource["id"], resource["cleanup_proof"],
                                        self.runtime.verify_cleanup_receipt, retry=True)
        evidence = self._record(result)
        if result["status"] == "reclaimed":
            status = "cleaned"
        elif result.get("result", {}).get("status") == "failed_no_change":
            status = "failed"
        else:
            status = "unknown"
        return {"status": status, "evidence": evidence}

    def reconcile_cleanup(self, task_id, resource_id, actor_id):
        """Confirm observed absence after an unknown response; never re-remove."""
        state, _ = self.controller.read()
        self._external(state["cleanup_resources"][resource_id]["spec"])
        result = self.artifacts.observe(resource_id)
        evidence = self._record(result)
        if result["status"] != "reclaimed":
            return result
        observation = self._record({"resource_id": resource_id, "status": "cleaned", "evidence": [evidence]})
        return self.runtime.reconcile_cleanup(task_id, resource_id, observation, actor_id)

    def release_agent_quota(self, operation_id, actor_id):
        """Release a quota only after CORE has persisted a confirmed host outcome."""
        receipt = self.runtime.release_receipt(operation_id, actor_id)
        context = load_bound(receipt)
        return self.controller.capacity_release(context["request_id"], context["permit_id"],
                                                receipt, self.runtime.verify_release_receipt)
