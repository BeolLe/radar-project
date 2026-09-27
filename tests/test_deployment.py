"""Local deployment contract checks; these do not claim a live cluster test."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
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
