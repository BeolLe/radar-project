"""Synthetic key only: validate DAG parsing and runtime stdin delivery without a cluster."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


class AirflowKeyTests(unittest.TestCase):
    def load_dag(self, variable=None):
        operator = MagicMock()
        modules = {
            "airflow": SimpleNamespace(DAG=MagicMock()),
            "airflow.sdk": SimpleNamespace(Param=lambda value, **kw: value, Variable=variable),
            "airflow.providers.cncf.kubernetes.operators.pod": SimpleNamespace(KubernetesPodOperator=operator),
            "airflow.providers.cncf.kubernetes.callbacks": SimpleNamespace(KubernetesPodOperatorCallback=object),
        }
        with patch.dict(sys.modules, modules):
            dag = runpy.run_path(str(Path(__file__).resolve().parents[1] / "airflow/radar_tagging.py"))
        return dag, modules, operator

    def test_key_not_read_during_parse_or_embedded_in_manifest(self):
        variable = MagicMock()
        _, _, operator = self.load_dag(variable)
        variable.get.assert_not_called()
        config = operator.call_args.kwargs
        self.assertFalse(config["do_xcom_push"])
        spec = config["pod_template_dict"]["spec"]
        base = spec["containers"][0]
        self.assertTrue(base["stdin"])
        self.assertTrue(base["stdinOnce"])
        self.assertFalse(base["tty"])
        self.assertNotIn("GEMINI_API_KEY", json.dumps(base["env"]))
        self.assertNotIn("gemini_api_key", json.dumps(spec))

    def test_delivery_uses_variable_stream_and_nonsecret_ack_only(self):
        variable = MagicMock()
        variable.get.return_value = "synthetic-private-value"
        dag, modules, _ = self.load_dag(variable)
        kubernetes = MagicMock()
        stream = MagicMock()
        connection = stream.return_value
        connection.is_open.return_value = True
        connection.read_stdout.return_value = "RADAR_KEY_READY\n"
        modules.update({"kubernetes": kubernetes, "kubernetes.stream": SimpleNamespace(stream=stream)})
        pod = SimpleNamespace(metadata=SimpleNamespace(name="radar-test", namespace="radar", annotations={}))
        client = MagicMock()
        with patch.dict(sys.modules, modules):
            dag["AirflowGeminiKey"].on_pod_starting(pod=pod, client=client)
        variable.get.assert_called_once_with("gemini_api_key")
        connection.write_stdin.assert_called_once_with('"synthetic-private-value"\n')
        connection.close.assert_called_once()
        self.assertNotIn("synthetic-private-value", str(stream.call_args))
        self.assertNotIn("synthetic-private-value", str(client.mock_calls))
        body = client.patch_namespaced_pod.call_args.kwargs["body"]
        self.assertEqual(body["metadata"]["annotations"], {"radar.selfronny.com/key-delivered": "true"})
        pod.metadata.annotations = body["metadata"]["annotations"]
        variable.reset_mock()
        with patch.dict(sys.modules, modules):
            dag["AirflowGeminiKey"].on_pod_starting(pod=pod, client=client)
        variable.get.assert_not_called()

    def test_delivery_failure_does_not_expose_key(self):
        variable = MagicMock()
        variable.get.side_effect = RuntimeError("synthetic-private-value")
        dag, modules, _ = self.load_dag(variable)
        pod = SimpleNamespace(metadata=SimpleNamespace(annotations={}))
        modules.update({"kubernetes": MagicMock(), "kubernetes.stream": SimpleNamespace(stream=MagicMock())})
        with patch.dict(sys.modules, modules), self.assertRaises(RuntimeError) as error:
            dag["AirflowGeminiKey"].on_pod_starting(pod=pod, client=MagicMock())
        self.assertNotIn("synthetic-private-value", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_bootstrap_reads_stdin_then_runs_existing_pipeline_without_echo(self):
        dag, _, _ = self.load_dag()
        wrapper = """
import runpy, os, sys
def check(name, run_name):
    assert name == 'radar' and run_name == '__main__'
    assert os.environ['GEMINI_API_KEY'] == 'synthetic-private-value'
    assert sys.argv == ['radar', 'tag-pending', '--max-requests', '1']
    print('RUN_OK')
runpy.run_module = check
""" + dag["BOOTSTRAP"]
        run = subprocess.run([sys.executable, "-c", wrapper, "tag-pending", "--max-requests", "1"],
                             input='"synthetic-private-value"\n', capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "RADAR_KEY_READY\nRUN_OK\n")
        self.assertNotIn("synthetic-private-value", run.stdout + run.stderr)
        for invalid in ('"bad secret"\n', 'null\n', '', '{bad-json}\n'):
            run = subprocess.run([sys.executable, "-c", dag["BOOTSTRAP"]], input=invalid,
                                 capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("RADAR_KEY_READY", run.stdout)
            self.assertIn("Invalid Airflow credential payload", run.stderr)


if __name__ == "__main__":
    unittest.main()
