"""Use existing Airflow Variable gemini_api_key; never copy it into a Secret or Pod spec.

First manual trigger: max_requests=1. Enable scheduling after checking its results.
"""
from datetime import datetime, timedelta, timezone
from airflow import DAG
from airflow.sdk import Param
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.callbacks import KubernetesPodOperatorCallback


# The key travels only over an authenticated attach stream into process memory.
BOOTSTRAP = """
import json, os, runpy, select, sys
if not select.select([sys.stdin], [], [], 300)[0]:
    raise SystemExit('Timed out waiting for Airflow credential delivery')
try:
    key = json.loads(sys.stdin.readline(8192))
    if not isinstance(key, str) or not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError()
except Exception:
    raise SystemExit('Invalid Airflow credential payload') from None
os.environ['GEMINI_API_KEY'] = key
del key
print('RADAR_KEY_READY', flush=True)
sys.argv = ['radar', *sys.argv[1:]]
runpy.run_module('radar', run_name='__main__')
"""


class AirflowGeminiKey(KubernetesPodOperatorCallback):
    @staticmethod
    def on_pod_starting(*, pod, client, **kwargs):
        import json
        import time
        from airflow.sdk import Variable
        from kubernetes import client as k8s
        from kubernetes.stream import stream

        marker = "radar.selfronny.com/key-delivered"
        if (pod.metadata.annotations or {}).get(marker) == "true":
            return  # Reattach to an already-running task without sending its key again.
        try:
            key = Variable.get("gemini_api_key")
            if not isinstance(key, str) or not key or len(key) > 4096 or any(c.isspace() for c in key):
                raise ValueError("Invalid credential")
            # stream changes its API client's transport; never mutate KPO's shared client.
            with k8s.ApiClient(configuration=client.api_client.configuration) as api:
                connection = stream(
                    k8s.CoreV1Api(api).connect_get_namespaced_pod_attach,
                    name=pod.metadata.name, namespace=pod.metadata.namespace, container="base",
                    stdin=True, stdout=True, stderr=False, tty=False, _preload_content=False,
                )
                try:
                    connection.write_stdin(json.dumps(key) + "\n")
                    del key
                    deadline = time.monotonic() + 30
                    output = ""
                    while connection.is_open() and time.monotonic() < deadline:
                        connection.update(timeout=1)
                        output = (output + connection.read_stdout())[-8192:]
                        if "RADAR_KEY_READY" in output:
                            client.patch_namespaced_pod(
                                name=pod.metadata.name, namespace=pod.metadata.namespace,
                                body={"metadata": {"annotations": {marker: "true"}}},
                            )
                            return
                    raise RuntimeError("Credential delivery not acknowledged")
                finally:
                    connection.close()
        except Exception:
            # Do not expose API payloads, Variable values or websocket exception details.
            raise RuntimeError("Gemini credential delivery failed; check Variable and pods/attach permission") from None


with DAG(
    dag_id="radar_tagging", schedule="0 9 * * *",  # 18:00 KST, after daily collection.
    start_date=datetime(2026, 9, 28, tzinfo=timezone.utc), catchup=False,
    max_active_runs=1, max_active_tasks=1, is_paused_upon_creation=True,
    tags=["radar"], default_args={"retries": 0},
    params={"max_requests": Param(200, type="integer", minimum=1, maximum=200)},
) as radar_tagging:
    KubernetesPodOperator(
        task_id="tag", name="radar-tagging", namespace="radar", in_cluster=True,
        image="ghcr.io/beolle/radar-project-pipeline:v0.4.0",
        cmds=["python", "-u", "-c", BOOTSTRAP],
        arguments=["tag-pending", "--run-id",
                   "{{ dag_run.start_date.strftime('%Y%m%dT%H%M%S%f') }}",
                   "--max-requests", "{{ params.max_requests }}", "--daily-limit", "200"],
        pool="radar_collection", pool_slots=1, get_logs=True, log_events_on_failure=True,
        do_xcom_push=False, deferrable=False, reattach_on_restart=True,
        callbacks=[AirflowGeminiKey],
        on_finish_action="delete_pod", startup_timeout_seconds=300,
        execution_timeout=timedelta(hours=14),
        pod_template_dict={
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"labels": {"app": "radar-tagger"}},
            "spec": {
                "restartPolicy": "Never", "activeDeadlineSeconds": 50400,
                "automountServiceAccountToken": False,
                "imagePullSecrets": [{"name": "radar-ghcr"}],
                "securityContext": {
                    "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                    "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [{
                    "name": "base",
                    "stdin": True, "stdinOnce": True, "tty": False,
                    "env": [
                        {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {
                            "name": "radar-pipeline-db", "key": "DATABASE_URL"}}},
                        {"name": "RADAR_DATA_DIR", "value": "/data"},
                    ],
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                  "limits": {"cpu": "1", "memory": "1Gi"}},
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "raw", "mountPath": "/data"},
                                     {"name": "tmp", "mountPath": "/tmp"}],
                }],
                "volumes": [{"name": "raw", "persistentVolumeClaim": {"claimName": "radar-raw"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}],
            },
        },
    )
