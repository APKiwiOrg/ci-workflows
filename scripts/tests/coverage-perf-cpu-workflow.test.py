#!/usr/bin/env python3
"""Run the coverage workflow scripts with command stubs, without installs or measurements."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "tools/EquipmentCoverage.Perf.Tests/EquipmentCoverage.Perf.Tests.csproj"
FILTER = ("FullyQualifiedName=EquipmentCoverage.Perf.Tests.CoveragePerfTests."
          "Measure_authorized_unit&Category=CoveragePerformance")
JOB_LIMIT = "${{ inputs.coverage-perf-cpu && (inputs.coverage-perf-preset == 'C0' && 20 || 120) || 90 }}"
RUN_LIMIT = ("${{ inputs.coverage-perf-cpu && (inputs.coverage-perf-preset == 'C0' && 20 || 120) || "
             "(inputs.artifact-env != '' && 60 || 90) }}")
FETCH_DEPTH = "${{ inputs.coverage-perf-cpu && 2 || 1 }}"


class CoverageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load((ROOT / ".github/workflows/stress-test.yml").read_text())
        cls.job = cls.workflow["jobs"]["stress"]
        cls.steps = {step.get("name"): step for step in cls.job["steps"]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.work / PROJECT).parent.mkdir(parents=True)
        (self.work / PROJECT).touch()
        (self.work / "Engine.slnx").touch()
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.work), "RUNNER_TEMP": str(self.work / "temp"),
            "GITHUB_STEP_SUMMARY": str(self.work / "summary"),
            "GITHUB_ENV": str(self.work / "github-env"),
            "PROJECT": PROJECT, "BUILD_TARGET": "", "CONFIGURATION": "Release",
            "LOAD": "0", "ITERATIONS": "1", "SUPPORT_COST": "false", "FILTER": "",
            "ARTIFACT_ENV": "KE_COVERAGE_PERF_REPORTS", "COVERAGE_PERF_CPU": "true",
            "COVERAGE_PERF_UNIT": "coverage-c0", "COVERAGE_PERF_MANIFEST_SHA256": "a" * 64,
            "COVERAGE_PERF_WORKLOAD_SHA": "b" * 40, "COVERAGE_PERF_PRESET": "C0",
            "ImageOS": "ubuntu26", "ImageVersion": "20261021.1",
        }
        self.manifest = self.work / "tools/EquipmentCoverage.Perf.Tests/authorized/coverage-c0.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({"preset": "C0"}))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o755)

    def run_step(self, name, **overrides):
        step = self.steps[name]
        # Match GitHub's Linux shell templates, including the default without pipefail.
        shells = {None: ["bash", "-e"],
                  "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
                  "sh": ["sh", "-e"]}
        shell = step.get("shell")
        self.assertIn(shell, shells, "unmodeled workflow shell")
        script = self.work / "step.sh"
        script.write_text(step["run"])
        return subprocess.run(shells[shell] + [str(script)],
                              cwd=self.work, env={**self.env, **overrides},
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_input_wiring_limits_and_validation_precedes_installs(self):
        inputs = self.workflow[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["coverage-perf-cpu"]["type"], "boolean")
        self.assertIs(inputs["coverage-perf-cpu"]["default"], False)
        for key in ("unit", "manifest-sha256", "workload-sha", "preset"):
            self.assertEqual(inputs["coverage-perf-" + key]["type"], "string")
            self.assertEqual(inputs["coverage-perf-" + key]["default"], "")
        self.assertIs(inputs["filter"]["required"], False)
        self.assertEqual(inputs["filter"]["default"], "")
        self.assertEqual(self.job["timeout-minutes"], JOB_LIMIT)
        self.assertEqual(self.steps["Repeat the selection"]["timeout-minutes"], RUN_LIMIT)
        checkouts = [step for step in self.job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"], {"fetch-depth": FETCH_DEPTH})
        names = [step.get("name") for step in self.job["steps"]]
        check = names.index("Check coverage performance inputs")
        stress_filter = names.index("Check stress filter")
        self.assertLess(stress_filter, names.index("Setup .NET"))
        self.assertEqual(self.steps["Check stress filter"]["if"], "${{ !inputs.coverage-perf-cpu }}")
        self.assertEqual(self.steps["Check stress filter"]["env"], {"FILTER": "${{ inputs.filter }}"})
        self.assertLess(check, names.index("Setup .NET"))
        self.assertLess(check, names.index("Install coverage software Vulkan"))
        self.assertGreater(check, next(i for i, step in enumerate(self.job["steps"])
                                       if step.get("uses", "").startswith("actions/checkout@")))
        gate = self.steps["Check coverage performance inputs"]
        self.assertEqual(gate["if"], "inputs.coverage-perf-cpu || inputs.coverage-perf-unit != '' || "
                         "inputs.coverage-perf-manifest-sha256 != '' || inputs.coverage-perf-workload-sha != '' || "
                         "inputs.coverage-perf-preset != ''")
        for key in ("cpu", "unit", "manifest-sha256", "workload-sha", "preset"):
            variable = "COVERAGE_PERF_" + key.upper().replace("-", "_")
            self.assertEqual(gate["env"][variable], "${{ inputs.coverage-perf-" + key + " }}")
            self.assertEqual(self.steps["Repeat the selection"]["env"].get(variable),
                             "${{ inputs.coverage-perf-" + key + " }}")
        for step in self.job["steps"]:
            self.assertNotIn("${{", step.get("run", ""))
        self.assertEqual(self.steps["Install coverage software Vulkan"]["if"], "inputs.coverage-perf-cpu")
        self.assertEqual(self.steps["Upload the stress artifacts"]["if"], "always() && inputs.artifact-env != ''")

    def test_non_coverage_routes_still_require_a_filter(self):
        empty = self.run_step("Check stress filter", FILTER="")
        self.assertEqual(empty.returncode, 1, empty.stdout)
        self.assertIn("filter is required unless coverage-perf-cpu is true", empty.stdout)
        given = self.run_step("Check stress filter", FILTER="FullyQualifiedName~Stub")
        self.assertEqual(given.returncode, 0, given.stdout)

    def test_valid_inputs_and_unit_boundaries(self):
        for overrides in ({}, {"BUILD_TARGET": PROJECT, "COVERAGE_PERF_PRESET": "C1"},
                          {"COVERAGE_PERF_UNIT": "a"}, {"COVERAGE_PERF_UNIT": "a" * 64}):
            with self.subTest(overrides=overrides):
                manifest = self.manifest.with_name(overrides.get("COVERAGE_PERF_UNIT", "coverage-c0") + ".json")
                manifest.write_text(json.dumps({"preset": overrides.get("COVERAGE_PERF_PRESET", "C0")}))
                result = self.run_step("Check coverage performance inputs", **overrides)
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_manifest_preset_binds_both_timeout_directions_and_matching_cases(self):
        for approved, selected in (("C0", "C1"), ("C1", "C0"), ("C0", "C0"), ("C1", "C1")):
            with self.subTest(approved=approved, selected=selected):
                self.manifest.write_text(json.dumps({"preset": approved}))
                result = self.run_step("Check coverage performance inputs", COVERAGE_PERF_PRESET=selected)
                self.assertEqual(result.returncode, 0 if approved == selected else 1, result.stdout)
                if approved != selected:
                    self.assertIn("::error::coverage-perf-preset", result.stdout)

    def test_missing_malformed_or_unresolved_manifest_refuses_preflight(self):
        for document in (None, "{", "[]", "null", '{}', '{"preset": true}', '{"preset": "G1"}'):
            with self.subTest(document=document):
                if document is None:
                    self.manifest.unlink()
                else:
                    self.manifest.write_text(document)
                result = self.run_step("Check coverage performance inputs")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("::error::coverage-perf-preset", result.stdout)

    def test_each_invalid_input_is_refused_with_its_name(self):
        cases = [("PROJECT", "other.csproj", "project"), ("BUILD_TARGET", "Engine.slnx", "build-target"),
                 ("CONFIGURATION", "Debug", "configuration"), ("LOAD", "1", "load"),
                 ("LOAD", "00", "load"), ("ITERATIONS", "01", "iterations"),
                 ("ITERATIONS", "2", "iterations"), ("SUPPORT_COST", "true", "support-cost"),
                 ("FILTER", "Name~Anything", "filter"), ("ARTIFACT_ENV", "PATH", "artifact-env"),
                 ("COVERAGE_PERF_UNIT", "", "unit"), ("COVERAGE_PERF_UNIT", "a" * 65, "unit"),
                 ("COVERAGE_PERF_UNIT", "A", "unit"), ("COVERAGE_PERF_UNIT", "a_b", "unit"),
                 ("COVERAGE_PERF_UNIT", "a\nb", "unit"), ("COVERAGE_PERF_UNIT", "é", "unit"),
                 ("COVERAGE_PERF_UNIT", "$(touch pwned)", "unit"),
                 ("COVERAGE_PERF_MANIFEST_SHA256", "A" * 64, "manifest-sha256"),
                 ("COVERAGE_PERF_MANIFEST_SHA256", "a" * 63, "manifest-sha256"),
                 ("COVERAGE_PERF_MANIFEST_SHA256", "a" * 65, "manifest-sha256"),
                 ("COVERAGE_PERF_WORKLOAD_SHA", "B" * 40, "workload-sha"),
                 ("COVERAGE_PERF_WORKLOAD_SHA", "b" * 39, "workload-sha"),
                 ("COVERAGE_PERF_WORKLOAD_SHA", "b" * 41, "workload-sha"),
                 ("COVERAGE_PERF_PRESET", "c0", "preset"),
                 ("COVERAGE_PERF_PRESET", "C2", "preset")]
        for variable, value, input_name in cases:
            with self.subTest(variable=variable, value=value):
                result = self.run_step("Check coverage performance inputs", **{variable: value})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("::error::", result.stdout)
                self.assertIn(input_name, result.stdout)
        self.assertFalse((self.work / "pwned").exists())

    def test_disabled_route_refuses_every_new_string(self):
        empty = {key: "" for key in self.env if key.startswith("COVERAGE_PERF_")}
        empty["COVERAGE_PERF_CPU"] = "false"
        self.assertEqual(self.run_step("Check coverage performance inputs", **empty).returncode, 0)
        for key in empty:
            if key == "COVERAGE_PERF_CPU":
                continue
            with self.subTest(key=key):
                result = self.run_step("Check coverage performance inputs", **{**empty, key: "x"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout)
                self.assertIn("coverage-perf-cpu", result.stdout)

    def test_direct_build_even_with_a_root_solution(self):
        self.stub("dotnet", "import sys\nprint(repr(sys.argv[1:]))\n")
        result = self.run_step("Build once")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(repr(["build", PROJECT, "-c", "Release"]), result.stdout)
        result = self.run_step("Build once", COVERAGE_PERF_CPU="false")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(repr(["build", "Engine.slnx", "-c", "Release"]), result.stdout)

    def stub_vulkan_setup(self):
        icd = self.work / "lvp_icd.fixture.json"
        icd.write_text('{"ICD":{"library_path":"fixture.so"}}')
        self.stub("sudo", "import sys\nprint('sudo ' + ' '.join(sys.argv[1:]))\n")
        self.stub("dpkg", "import os\nprint(os.environ['STUB_ICD'])\n")
        self.stub("dpkg-query", "print('libvulkan1 1.4.fixture\\nmesa-vulkan-drivers 26.fixture')\n")
        self.stub("vulkaninfo", "import os\nassert os.environ['VK_DRIVER_FILES'] == os.environ['STUB_ICD']\n"
                  "assert os.environ['VK_ICD_FILENAMES'] == os.environ['STUB_ICD']\nprint('deviceName = llvmpipe fixture')\n")
        return icd

    def test_lavapipe_discovery_exports_and_diagnostics(self):
        icd = self.stub_vulkan_setup()
        result = self.run_step("Install coverage software Vulkan", STUB_ICD=str(icd))
        self.assertEqual(result.returncode, 0, result.stdout)
        exports = (self.work / "github-env").read_text()
        for key in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES"):
            self.assertIn(key + "=" + str(icd), exports)
        summary = (self.work / "summary").read_text()
        for value in (str(icd), "libvulkan1", "mesa-vulkan-drivers", "ubuntu26",
                      "20261021.1", "llvmpipe fixture", "vulkan-native"):
            self.assertIn(value, result.stdout)
            self.assertIn(value, summary)
        self.assertIn("sudo apt-get install -y libvulkan1 mesa-vulkan-drivers vulkan-tools", result.stdout)
        result = self.run_step("Install coverage software Vulkan", STUB_ICD="/missing/no_icd.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::lavapipe ICD", result.stdout)

    def test_failed_vulkan_diagnostic_fails_setup_under_declared_shell(self):
        icd = self.stub_vulkan_setup()
        self.stub("vulkaninfo", "import sys\nprint('fixture Vulkan diagnostic failed')\nsys.exit(23)\n")
        name = "Install coverage software Vulkan"
        legacy = dict(self.steps[name])
        legacy.pop("shell", None)
        with patch.dict(self.steps, {name: legacy}):
            old_result = self.run_step(name, STUB_ICD=str(icd))
        self.assertEqual(old_result.returncode, 0, old_result.stdout)
        self.assertIn("fixture Vulkan diagnostic failed", old_result.stdout)
        print("Old undeclared shell swallowed vulkaninfo exit 23 (step exit 0)")

        result = self.run_step(name, STUB_ICD=str(icd))
        self.assertIn("fixture Vulkan diagnostic failed", result.stdout)
        self.assertIn("fixture Vulkan diagnostic failed", (self.work / "summary").read_text())
        log = Path(self.env["RUNNER_TEMP"]) / "stress/coverage-vulkan-setup.log"
        self.assertIn("fixture Vulkan diagnostic failed", log.read_text())
        self.assertEqual(result.returncode, 23, result.stdout)
        print("Declared shell propagated vulkaninfo exit 23 (step exit 23)")

    def test_setup_pipeline_failures_are_not_masked(self):
        real_tee = shutil.which("tee")
        self.assertIsNotNone(real_tee)
        for command in ("sudo", "dpkg", "grep", "dpkg-query", "cat", "summary-tee", "log-tee"):
            with self.subTest(command=command):
                for stub in self.bin.iterdir():
                    stub.unlink()
                icd = self.stub_vulkan_setup()
                if command.endswith("-tee"):
                    destination = ("coverage-vulkan-setup.log" if command == "log-tee" else "summary")
                    self.stub("tee", "import subprocess, sys\n"
                              "code = subprocess.call([%r] + sys.argv[1:])\n"
                              "sys.exit(29 if sys.argv[-1].endswith(%r) else code)\n" % (real_tee, destination))
                else:
                    self.stub(command, "import sys\nprint('fixture command failed')\nsys.exit(29)\n")
                result = self.run_step("Install coverage software Vulkan", STUB_ICD=str(icd))
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_run_contract_and_unfinalized_evidence(self):
        self.stub("dotnet", """import json, os, pathlib, sys
e = os.environ
args = sys.argv[1:]
assert args[:5] == ['test', e['PROJECT'], '-c', 'Release', '--no-build']
assert args[args.index('--filter') + 1] == %r
for key, value in {'KE_COVERAGE_PERF_UNIT': 'coverage-c0', 'KE_COVERAGE_PERF_MANIFEST_SHA256': 'a' * 64,
                   'KE_COVERAGE_PERF_WORKLOAD_SHA': 'b' * 40, 'KE_COVERAGE_PERF_PRESET': e['COVERAGE_PERF_PRESET'],
                   'KE_GRAPHICS_BACKEND': 'vulkan-native',
                   'KE_VULKAN_REQUIRED': '1', 'KE_GPU_TESTS': '1'}.items():
    assert e[key] == value, (key, e.get(key))
assert e['VK_DRIVER_FILES'] == e['VK_ICD_FILENAMES'] == '/fixture/lvp_icd.json'
reports = pathlib.Path(e['KE_COVERAGE_PERF_REPORTS'])
mode = e.get('STUB_EVIDENCE', 'valid')
document = {'schema': 'khaozengine.coverage-perf-report/1', 'report': {'identity': {'unitId': 'coverage-c0'}}}
if mode == 'schema': document['schema'] = 'old'
if mode == 'unit': document['report']['identity']['unitId'] = 'other'
if mode != 'missing-json':
    (reports / 'coverage-c0.json').write_text('' if mode == 'empty-json' else '{' if mode == 'malformed-json' else json.dumps(document))
if mode != 'missing-md': (reports / 'coverage-c0.md').write_text('' if mode == 'empty-md' else 'Final report')
executed = '0' if mode == 'zero' else '2' if mode == 'multiple' else '1'
failed = '1' if mode == 'failed' else '0'
skipped = '1' if mode == 'skipped' else '0'
trx = '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results><UnitTestResult outcome="Passed"/></Results><ResultSummary><Counters total="1" executed="' + executed + '" passed="1" failed="' + failed + '" notExecuted="' + skipped + '" /></ResultSummary></TestRun>'
if mode == 'malformed-trx': trx = '<'
if mode != 'missing-trx': (pathlib.Path(e['RUNNER_TEMP']) / 'stress/run-1.trx').write_text(trx)
print('fixture test output')
sys.exit(3 if mode == 'exit' else 0)
""" % FILTER)
        for mode in ("valid", "valid-c1", "zero", "multiple", "skipped", "failed", "missing-trx", "malformed-trx",
                     "missing-json", "empty-json", "malformed-json", "schema", "unit", "missing-md", "empty-md", "exit"):
            with self.subTest(mode=mode):
                # Each fixture gets an empty retained tree so missing files cannot inherit a prior pass.
                self.env["RUNNER_TEMP"] = str(self.work / mode)
                result = self.run_step("Repeat the selection", STUB_EVIDENCE=mode,
                                       COVERAGE_PERF_PRESET="C1" if mode == "valid-c1" else "C0",
                                       VK_DRIVER_FILES="/fixture/lvp_icd.json", VK_ICD_FILENAMES="/fixture/lvp_icd.json")
                self.assertEqual(result.returncode, 0 if mode in ("valid", "valid-c1") else 1, result.stdout)
                stress = Path(self.env["RUNNER_TEMP"]) / "stress"
                log = (stress / "run-1.log").read_text()
                self.assertIn("fixture test output", log)
                self.assertIn("dotnet test exit " + ("3" if mode == "exit" else "0"), log)
                if mode not in ("valid", "valid-c1"):
                    self.assertIn("::error::", result.stdout)
                if mode not in ("missing-json", "empty-json", "malformed-json"):
                    self.assertTrue((stress / "artifacts/run-1/coverage-c0.json").is_file())


if __name__ == "__main__":
    unittest.main()
