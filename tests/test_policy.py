import json
import unittest
from pathlib import Path

from server_autoops.policy import validate_policy

ROOT = Path(__file__).resolve().parent.parent


def ok_policy():
    return {"services": {"web": {"container": "web", "mutable": True,
                                 "probe": {"kind": "http", "url": "http://127.0.0.1:8080/health"}}},
            "faults": {"web:UNHEALTHY": {"targets": ["web"]}}}


class PolicyValidationTest(unittest.TestCase):
    def errors(self, policy):
        return validate_policy(policy)[1]

    def test_minimal_policy_gets_safe_defaults(self):
        policy, errors, _ = validate_policy(ok_policy())
        self.assertEqual(errors, [])
        self.assertEqual(policy["docker"]["daemons"], {"main": "unix:///var/run/docker.sock"})
        self.assertIn("autoops", policy["protected_name_patterns"])
        self.assertFalse(policy["services"]["web"]["login_sensitive"])
        self.assertEqual(policy["services"]["web"]["probe_timeout_seconds"], 45)

    def test_shipped_examples_validate(self):
        for name in ("policy.example.json", "policy.minimal.json"):
            data = json.loads((ROOT / "examples" / name).read_text())
            self.assertEqual(self.errors(data), [], name)

    def test_unknown_keys_and_targets_are_errors(self):
        p = ok_policy(); p["services"]["web"]["shell"] = "sh -c reboot"
        self.assertTrue(any("unknown key 'shell'" in e for e in self.errors(p)))
        p = ok_policy(); p["faults"]["web:UNHEALTHY"]["targets"] = ["db"]
        self.assertTrue(any("not a defined service" in e for e in self.errors(p)))
        p = ok_policy(); p["sudo"] = True
        self.assertTrue(any("unknown top-level key" in e for e in self.errors(p)))

    def test_remote_docker_and_unsafe_paths_are_rejected(self):
        p = ok_policy(); p["docker"] = {"daemons": {"main": "tcp://10.0.0.1:2375"}}
        self.assertTrue(any("unix:///" in e for e in self.errors(p)))
        p = ok_policy(); p["services"]["web"]["configs"] = {"c": {"path": "/etc/../etc/shadow", "format": "json", "editable_keys": ["a"]}}
        self.assertTrue(any(".path" in e for e in self.errors(p)))
        p = ok_policy(); p["services"]["web"]["probe"] = {"kind": "http", "url": "http://user:pw@127.0.0.1/"}
        self.assertTrue(any("without credentials" in e for e in self.errors(p)))

    def test_bounds_must_cover_editable_keys_and_be_ordered(self):
        p = ok_policy()
        p["services"]["web"]["configs"] = {"c": {"path": "/srv/web/c.json", "format": "json", "editable_keys": ["workers"],
                                                 "bounds": {"workers": [8, 1], "other": [0, 1]}}}
        errs = self.errors(p)
        self.assertTrue(any("min <= max" in e for e in errs))
        self.assertTrue(any("not in editable_keys" in e for e in errs))

    def test_fault_keys_and_aliases(self):
        p = ok_policy(); p["faults"]["web UNHEALTHY"] = {"targets": ["web"]}
        self.assertTrue(any("without spaces" in e for e in self.errors(p)))
        p = ok_policy(); p["code_aliases"] = {"web:ALL": ["A B"]}
        self.assertTrue(any("code_aliases" in e for e in self.errors(p)))

    def test_simulated_driver_needs_explicit_opt_in(self):
        p = ok_policy(); p["services"]["web"] = {"driver": "simulated", "state_file": "/tmp/x.json"}
        self.assertTrue(any("allow_simulated" in e for e in self.errors(p)))
        p["allow_simulated"] = True
        self.assertEqual(self.errors(p), [])

    def test_warnings_explain_weak_probes_and_protected_names(self):
        p = ok_policy(); p["services"]["autoops-web"] = {"container": "autoops-web", "mutable": True}
        p["faults"]["autoops-web:DOWN"] = {"targets": ["autoops-web"]}
        _, errors, warnings = validate_policy(p)
        self.assertEqual(errors, [])
        self.assertTrue(any("protected pattern" in w for w in warnings))
        self.assertTrue(any("business unverified" in w for w in warnings))

    def test_json_schema_file_is_valid_json_and_lists_same_top_keys(self):
        from server_autoops.policy import TOP_KEYS, SERVICE_KEYS
        schema = json.loads((ROOT / "schema" / "policy.schema.json").read_text())
        self.assertEqual(set(schema["properties"]), TOP_KEYS)
        self.assertEqual(set(schema["$defs"]["service"]["properties"]), SERVICE_KEYS)
