"""Copy into git-sync only after building pipeline:v0.4.0 and creating radar-gemini.

First manual trigger: max_requests=1. Enable scheduling after checking its results.
"""
from datetime import datetime, timedelta, timezone
from airflow import DAG
from airflow.sdk import Param
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator


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
        cmds=["python", "-m", "radar"],
        arguments=["tag-pending", "--run-id",
                   "{{ dag_run.start_date.strftime('%Y%m%dT%H%M%S%f') }}",
                   "--max-requests", "{{ params.max_requests }}", "--daily-limit", "200"],
        pool="radar_collection", pool_slots=1, get_logs=True, log_events_on_failure=True,
        do_xcom_push=False, deferrable=False, reattach_on_restart=True,
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
                    "env": [
                        {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {
                            "name": "radar-pipeline-db", "key": "DATABASE_URL"}}},
                        {"name": "GEMINI_API_KEY", "valueFrom": {"secretKeyRef": {
                            "name": "radar-gemini", "key": "GEMINI_API_KEY"}}},
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
