"""Copy this standalone DAG file into the existing airflow-practice git-sync repo.

Prerequisites: radar namespace RBAC/Secrets/PVC, pipeline:v0.3.0 image,
and the radar_collection pool with one slot. Enable only after an import check.
"""
from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator


# Pin dates to the DAG run, not task attempt time. All schedules below are UTC.
RUN_TIME = "(data_interval_end if dag_run.run_type == 'scheduled' else dag_run.start_date)"
DAILY_DATE = "{{ (" + RUN_TIME + " - macros.timedelta(days=1)).strftime('%Y-%m-%d') }}"
WEEKLY_AS_OF = "{{ " + RUN_TIME + ".strftime('%Y-%m-%d') }}"

for dag_id, schedule, arguments, memory in (
    ("radar_daily", "0 4 * * *", ["collect-all-daily", "--date", DAILY_DATE], "256Mi"),
    ("radar_weekly", "0 5 * * 2", ["collect-weekly", "--as-of", WEEKLY_AS_OF], "2Gi"),
):
    with DAG(
        dag_id=dag_id,
        schedule=schedule,  # Daily 13:00 KST; Tuesday 14:00 KST.
        start_date=datetime(2026, 9, 21, tzinfo=timezone.utc),
        catchup=False,
        max_active_runs=1,
        max_active_tasks=1,
        is_paused_upon_creation=True,
        tags=["radar"],
        default_args={"retries": 2, "retry_delay": timedelta(minutes=30)},
    ) as dag:
        KubernetesPodOperator(
            task_id="collect",
            name=dag_id.replace("_", "-"),
            namespace="radar",
            in_cluster=True,
            image="ghcr.io/beolle/radar-project-pipeline:v0.3.0",
            cmds=["python", "-m", "radar"],
            arguments=arguments,
            pool="radar_collection",
            pool_slots=1,
            get_logs=True,
            log_events_on_failure=True,
            do_xcom_push=False,
            deferrable=False,
            reattach_on_restart=True,
            on_finish_action="delete_pod",
            startup_timeout_seconds=300,
            execution_timeout=timedelta(hours=2),
            pod_template_dict={
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"labels": {"app": "radar-collector"}},
                "spec": {
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": 7200,
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
                            {"name": "CLOUDFLARE_API_TOKEN", "valueFrom": {"secretKeyRef": {
                                "name": "radar-cloudflare-api", "key": "token"}}},
                            {"name": "RADAR_DATA_DIR", "value": "/data"},
                            {"name": "PYTHONUNBUFFERED", "value": "1"},
                        ],
                        "resources": {"requests": {"cpu": "250m", "memory": memory},
                                      "limits": {"cpu": "2", "memory": "4Gi"}},
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
    globals()[dag_id] = dag
