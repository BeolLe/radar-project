"""Local deployment contract checks; these do not claim a live cluster test."""
import json
from pathlib import Path
import runpy
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_airflow_namespace_permissions_and_dag_contract(self):
        role, binding = json.loads((ROOT / "k8s/airflow-rbac.json").read_text())["items"]
        self.assertEqual(role["metadata"], {"name": "radar-airflow", "namespace": "radar"})
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "airflow-worker", "namespace": "pipeline"}])
        self.assertEqual(binding["roleRef"]["kind"], "Role")
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        resources = {resource for rule in role["rules"] for resource in rule["resources"]}
        self.assertEqual(resources, {"pods", "pods/log", "events"})
        self.assertIn("  - airflow-rbac.json", (ROOT / "k8s/kustomization.yaml").read_text())
        # Configuration contract only; actual provider import remains a cluster deployment gate.
        airflow = ModuleType("airflow")
        airflow.DAG = MagicMock()
        pod = ModuleType("airflow.providers.cncf.kubernetes.operators.pod")
        pod.KubernetesPodOperator = MagicMock()
        with patch.dict(sys.modules, {"airflow": airflow, pod.__name__: pod}):
            runpy.run_path(str(ROOT / "airflow/radar_collection.py"))
        self.assertEqual(airflow.DAG.call_count, 2)
        for call in airflow.DAG.call_args_list:
            self.assertFalse(call.kwargs["catchup"])
            self.assertTrue(call.kwargs["is_paused_upon_creation"])
            self.assertEqual(call.kwargs["max_active_runs"], 1)
        self.assertEqual([call.kwargs["schedule"] for call in airflow.DAG.call_args_list], ["0 4 * * *", "0 5 * * 2"])
        for call in pod.KubernetesPodOperator.call_args_list:
            config = call.kwargs
            self.assertEqual(config["namespace"], "radar")
            self.assertEqual(config["pool"], "radar_collection")
            self.assertFalse(config["deferrable"])
            self.assertFalse(config["do_xcom_push"])
            self.assertEqual(config["image"], "ghcr.io/beolle/radar-project-pipeline:v0.3.1")
            spec = config["pod_template_dict"]["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertEqual(spec["imagePullSecrets"], [{"name": "radar-ghcr"}])
            self.assertEqual(spec["containers"][0]["resources"]["limits"]["memory"], "4Gi")
            self.assertIn("dag_run.start_date", config["arguments"][-1])

    def test_workload_contract(self):
        web, service = json.loads((ROOT / "k8s/web.json").read_text())["items"]
        job = json.loads((ROOT / "k8s/pipeline-job.json").read_text())
        storage = json.loads((ROOT / "k8s/storage.json").read_text())
        namespace = json.loads((ROOT / "k8s/namespace.json").read_text())
        self.assertEqual(namespace["metadata"]["name"], "radar")
        for resource in (web, service, job, storage):
            self.assertEqual(resource["metadata"]["namespace"], "radar")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["selector"], web["spec"]["template"]["metadata"]["labels"])
        pods = [w["spec"]["template"]["spec"] for w in (web, job)]
        for pod, secret in zip(pods, ("radar-web-db", "radar-pipeline-db")):
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["imagePullSecrets"], [{"name": "radar-ghcr"}])
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            container = pod["containers"][0]
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(container["env"][0]["valueFrom"]["secretKeyRef"]["name"], secret)
            self.assertNotIn("latest", container["image"])
        claim = pods[1]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        self.assertEqual(claim, storage["metadata"]["name"])
        self.assertEqual(pods[1]["containers"][0]["args"], ["init-db"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertNotIn("pipeline-job.json", (ROOT / "k8s/kustomization.yaml").read_text())
        readiness = pods[0]["containers"][0]["readinessProbe"]["httpGet"]["path"]
        self.assertTrue((ROOT / "web/app" / readiness.lstrip("/") / "route.ts").is_file())

    def test_static_storage_is_reserved_and_retained(self):
        pv = json.loads((ROOT / "k8s/pv.json").read_text())
        pvc = json.loads((ROOT / "k8s/storage.json").read_text())
        namespace = json.loads((ROOT / "k8s/namespace.json").read_text())
        self.assertEqual(pv["kind"], "PersistentVolume")
        self.assertNotIn("namespace", pv["metadata"])
        self.assertEqual(pvc["spec"]["volumeName"], pv["metadata"]["name"])
        self.assertEqual(pv["spec"]["claimRef"], {
            "name": pvc["metadata"]["name"], "namespace": pvc["metadata"]["namespace"],
        })
        self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        for resource in (pv, pvc):
            self.assertEqual(resource["spec"]["storageClassName"], "")
            self.assertEqual(resource["spec"]["volumeMode"], "Filesystem")
            self.assertEqual(resource["spec"]["accessModes"], ["ReadWriteOnce"])
        for resource in (pv, pvc, namespace):
            options = resource["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
            self.assertEqual(set(options.split(",")), {"Prune=false", "Delete=false"})
        self.assertEqual(pv["spec"]["capacity"], pvc["spec"]["resources"]["requests"])
        self.assertEqual(pv["spec"]["local"]["path"], "/mnt/data/ronny-project/radar-data")
        self.assertEqual(pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"], [{
            "matchFields": [{"key": "metadata.name", "operator": "In", "values": ["ronny"]}],
        }])
        self.assertIn("  - pv.json", (ROOT / "k8s/kustomization.yaml").read_text())

    def test_argocd_is_manual_and_scoped(self):
        app = json.loads((ROOT / "argocd/application.json").read_text())
        self.assertEqual(app["metadata"], {"name": "radar", "namespace": "argocd"})
        spec = app["spec"]
        self.assertEqual(spec["project"], "default")
        self.assertEqual(spec["source"], {
            "repoURL": "https://github.com/BeolLe/radar-project.git",
            "targetRevision": "main", "path": "k8s",
        })
        self.assertEqual(spec["destination"], {
            "server": "https://kubernetes.default.svc", "namespace": "radar",
        })
        self.assertEqual(spec["syncPolicy"], {})
        self.assertNotIn("operation", app)
        resources = (ROOT / spec["source"]["path"] / "kustomization.yaml").read_text()
        self.assertNotIn("argocd", resources)
        self.assertNotIn("pipeline-job.json", resources)

    def test_dedicated_tunnel_contract(self):
        tunnel = json.loads((ROOT / "k8s/cloudflared.json").read_text())
        self.assertEqual(tunnel["metadata"], {"name": "radar-cloudflared", "namespace": "radar"})
        self.assertEqual(tunnel["spec"]["replicas"], 1)
        self.assertEqual(tunnel["spec"]["selector"]["matchLabels"],
                         tunnel["spec"]["template"]["metadata"]["labels"])
        pod = tunnel["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertNotIn("volumes", pod)
        container = pod["containers"][0]
        self.assertEqual(container["image"], "cloudflare/cloudflared:2026.9.3")
        self.assertEqual(container["args"], ["tunnel", "--no-autoupdate", "--loglevel", "info",
                                             "--metrics", "0.0.0.0:2000", "run"])
        self.assertEqual(container["env"], [{"name": "TUNNEL_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "radar-tunnel", "key": "token"},
        }}])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/ready", "port": "metrics"})
        self.assertEqual(container["livenessProbe"]["tcpSocket"], {"port": "metrics"})
        self.assertIn("  - cloudflared.json", (ROOT / "k8s/kustomization.yaml").read_text())

    def test_collection_job_is_manual_and_reuses_pipeline_security(self):
        collect = json.loads((ROOT / "k8s/collect-job.json").read_text())
        init = json.loads((ROOT / "k8s/pipeline-job.json").read_text())
        self.assertEqual(collect["metadata"], {"generateName": "radar-collect-daily-", "namespace": "radar"})
        self.assertNotIn("collect-job.json", (ROOT / "k8s/kustomization.yaml").read_text())
        self.assertEqual(collect["spec"]["backoffLimit"], 0)
        pod = collect["spec"]["template"]["spec"]
        base = init["spec"]["template"]["spec"]
        for key in ("securityContext", "volumes", "imagePullSecrets", "restartPolicy", "automountServiceAccountToken"):
            self.assertEqual(pod[key], base[key])
        container = pod["containers"][0]
        self.assertEqual(container["args"], ["collect-daily", "--locations", "WORLD", "KR"])
        self.assertEqual(container["image"], "ghcr.io/beolle/radar-project-pipeline:v0.2.0")
        self.assertEqual(container["env"][-1], {"name": "CLOUDFLARE_API_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "radar-cloudflare-api", "key": "token"},
        }})
        for key in ("securityContext", "resources", "volumeMounts"):
            self.assertEqual(container[key], base["containers"][0][key])

    def test_build_context_excludes_secrets(self):
        excluded = (ROOT / ".dockerignore").read_text().splitlines()
        for entry in (".git", "**/.env*", "secrets", "data", "**/node_modules", "k8s/local"):
            self.assertIn(entry, excluded)
        dockerfile = (ROOT / "Dockerfile.web").read_text()
        self.assertIn('"0.0.0.0"', dockerfile)
        self.assertIn("next.config.mjs", dockerfile)
        self.assertFalse((ROOT / "web/next.config.ts").exists())


if __name__ == "__main__":
    unittest.main()
