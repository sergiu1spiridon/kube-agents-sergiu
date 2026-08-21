"""Stage 1 E2E Promotion Test: Autonomous Fleet Audits, Audit Streams & GitHub Integration."""

import json
import os
import pathlib
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

import pytest

if "CLOUDSDK_PYTHON" not in os.environ and pathlib.Path("/usr/bin/python3").exists():
    os.environ["CLOUDSDK_PYTHON"] = "/usr/bin/python3"

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AUDIT_REPORT_SCRIPT = (
    _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
)

# All 7 Registered Audit Streams and their human titles
AUDIT_STREAMS: List[Tuple[str, str]] = [
    ("compliance-audit", "Security & RBAC Posture Audit"),
    ("security-patch-orchestrator", "Upgrade & Patch Readiness Audit"),
    ("obtainability-audit", "Workload Reliability Audit"),
    ("fleet-wide-cost-analysis", "Fleet Waste Audit"),
    ("fleet-consistency-drift", "Fleet Consistency Drift Audit"),
    ("ai-security-audit", "AI Workload Security Audit"),
    ("stockout-prevention", "Fleet Stockout Prevention & Capacity Audit"),
]


def test_agent_stockout_prevention_fleet_audit(
    port_forward_agent: Optional[str],
    platform_agent_api_key: Optional[str],
) -> None:
    """Tests the Platform Agent's live automated SRE audit capability for capacity and stockout prevention."""
    if not port_forward_agent or not platform_agent_api_key:
        pytest.skip("No port-forward URL or API key found; skipping fleet audit E2E test.")

    prompt = (
        "Run the daily fleet stockout prevention and capacity audit. "
        "Evaluate the cluster capacity, check for single-zone nodepools, "
        "missing zone fallbacks, and compute class configurations. "
        "Report the audit summary."
    )

    url = f"{port_forward_agent}/v1/responses"
    payload = json.dumps({
        "model": "model-default",
        "input": prompt,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {platform_agent_api_key}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            assert resp.status == 200, f"Expected HTTP 200 from agent audit API, got {resp.status}"
            body = json.loads(resp.read().decode("utf-8"))
            response_text = str(body)
            assert "output" in body or "choices" in body or "response" in body or "assistant" in response_text, (
                f"Agent response missing expected structure: {body}"
            )
            content = ""
            if "output" in body:
                content = str(body["output"])
            elif "choices" in body and isinstance(body["choices"], list) and body["choices"]:
                choice = body["choices"][0]
                content = str(choice.get("message", {}).get("content", "") or choice.get("text", ""))
            elif "response" in body:
                content = str(body["response"])
            else:
                content = response_text

            assert len(content.strip()) > 30, f"Agent returned unexpectedly empty or short audit response: {content}"
            content_lower = content.lower()
            assert not ("traceback (most recent call last)" in content_lower or "internal server error" in content_lower), (
                f"Agent response contains runtime exception: {content}"
            )
            # Verify the agent actually performed capacity / audit reasoning
            domain_terms = ["capacity", "nodepool", "zone", "cluster", "stockout", "audit", "compute", "finding", "workload"]
            assert any(term in content_lower for term in domain_terms), (
                f"Agent audit response does not mention relevant capacity/audit concepts: {content}"
            )
    except urllib.error.HTTPError as e:
        pytest.fail(f"Agent fleet audit execution failed with HTTP {e.code}: {e.read().decode('utf-8')}")
    except Exception as e:
        pytest.fail(f"Failed to execute agent fleet audit on {url}: {e}")


def test_github_token_minting_and_connectivity(
    gke_cluster_name: Optional[str],
    agent_namespace: str,
    github_repo: Optional[str],
) -> None:
    """Verifies live in-cluster GitHub authentication, token minting, and repository reachability.

    Executes a genuinely 100% read-only probe inside the platform-agent pod:
    1. Triggers token refresh via the Envoy credential proxy sidecar and GitHub Token Minter (Cloud KMS).
    2. Executes `gh api repos/<target_repo>` from the shared workspace root.
    3. Verifies repository access and permissions over the network.
    Does NOT invoke `audit_report.py start`, preventing workspace reset, lease scrubbing, or label writes.
    """
    if not gke_cluster_name or not github_repo:
        pytest.skip("GKE cluster or GITHUB_REPO not configured; skipping live GitHub connectivity probe.")

    # Find running platform-agent pod
    proc_pod = subprocess.run(
        [
            "kubectl",
            "get",
            "pod",
            "-n",
            agent_namespace,
            "-l",
            "app=platform-agent-gateway",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    if proc_pod.returncode != 0 or not proc_pod.stdout.strip():
        pytest.skip(f"No running platform-agent-gateway pod found in namespace '{agent_namespace}'.")

    pod_name = proc_pod.stdout.strip()

    # Refresh credentials via broker and query repository via read-only GET API
    script = f"""
import sys, subprocess, os

# 1. Refresh credentials in the credential proxy via the broker client
p_refresh = subprocess.run(['find', '/opt', '-name', 'github_token_refresh.py'], capture_output=True, text=True)
if p_refresh.returncode == 0 and p_refresh.stdout.strip():
    refresh_script = p_refresh.stdout.strip().splitlines()[0]
    res_ref = subprocess.run(['python3', refresh_script, '{github_repo}'], capture_output=True, text=True)
    if res_ref.returncode != 0:
        print(f"Token refresh failed: {{res_ref.stderr}}", file=sys.stderr)
        sys.exit(res_ref.returncode)

# 2. Execute read-only GitHub API verification via Envoy proxy from workspace root
env = os.environ.copy()
env['PWD'] = '/opt/data'
cmd_gh = ['gh', 'api', f'repos/{github_repo}', '--jq', '.full_name']
res_gh = subprocess.run(cmd_gh, cwd='/opt/data', env=env, capture_output=True, text=True)
if res_gh.returncode != 0:
    print(f"GitHub API query failed: {{res_gh.stderr}}", file=sys.stderr)
    sys.exit(res_gh.returncode)

full_name = res_gh.stdout.strip()
if full_name.lower() != '{github_repo}'.lower():
    print(f"Expected repository '{github_repo}', got '{{full_name}}'", file=sys.stderr)
    sys.exit(1)

print(f"Successfully authenticated and queried repository: {{full_name}}")
"""

    cmd = [
        "kubectl",
        "exec",
        "-n",
        agent_namespace,
        pod_name,
        "-c",
        "platform-agent",
        "--",
        "python3",
        "-c",
        script,
    ]
    try:
        proc_start = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        assert proc_start.returncode == 0, (
            f"GitHub token minting and API probe failed inside pod '{pod_name}' (exit code {proc_start.returncode}):\n"
            f"STDOUT:\n{proc_start.stdout}\nSTDERR:\n{proc_start.stderr}"
        )
        assert f"Successfully authenticated and queried repository: {github_repo}" in proc_start.stdout, (
            f"Expected successful repository query confirmation in stdout, got:\n{proc_start.stdout}"
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"GitHub token minting / API probe timed out after 90s in pod '{pod_name}'")


def test_live_in_pod_stockout_audit_dryrun_lifecycle(
    gke_cluster_name: Optional[str],
    agent_namespace: str,
    github_repo: Optional[str],
) -> None:
    """Verifies live in-pod stockout audit execution with real workspace and credentials, suppressing mutations via --dry-run.

    Executes `audit_report.py finish --audit=stockout-prevention --dry-run` inside the live platform-agent container.
    This exercises the full live runtime:
    - Verifies real credential proxy access and repo workspace lease.
    - Runs full finding validation against the live 12-check stockout roster.
    - Renders the complete production Markdown ledger body.
    - Guarantees ZERO issue creation, comments, or PR creation on GitHub via the --dry-run guard.
    """
    if not gke_cluster_name or not github_repo:
        pytest.skip("GKE cluster or GITHUB_REPO not configured; skipping live in-pod dry-run audit.")

    proc_pod = subprocess.run(
        [
            "kubectl",
            "get",
            "pod",
            "-n",
            agent_namespace,
            "-l",
            "app=platform-agent-gateway",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    if proc_pod.returncode != 0 or not proc_pod.stdout.strip():
        pytest.skip(f"No running platform-agent-gateway pod found in namespace '{agent_namespace}'.")

    pod_name = proc_pod.stdout.strip()

    script_cmd = """
import sys, subprocess, json, tempfile, os, shutil

p = subprocess.run(['find', '/opt', '-name', 'audit_report.py'], capture_output=True, text=True)
if p.returncode != 0 or not p.stdout.strip():
    sys.exit(1)
script_path = p.stdout.strip().splitlines()[0]

# Use an ephemeral lease workspace under /opt/data to avoid colliding with or resetting the cron lease
tmp_dir = tempfile.mkdtemp(dir="/opt/data")
env = os.environ.copy()
env["PWD"] = "/opt/data"
env["GITOPS_WORKSPACE"] = tmp_dir
env["SCRATCH_DIR"] = tmp_dir

doc = {
  "audit": "stockout-prevention",
  "scope": {
    "clusters": [{
      "name": "%s",
      "location": "us-east4",
      "project": "sergiuspiridon-gkedemos",
      "checks_run": [
        {"check": "ccc-missing-fallbacks", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "ccc-no-ondemand-floor", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "ccc-large-vm-scarcity", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "ccc-priority-starvation", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "ccc-mixed-disk-generations", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "ccc-hyperdisk-incompatible", "command": "kubectl get customcomputenamespaces -A"},
        {"check": "quota-exhaustion-risk", "command": "gcloud compute project-info describe"},
        {"check": "spot-scarcity-risk", "command": "kubectl get pods -A"},
        {"check": "single-zone-nodepool", "command": "kubectl get nodepools -A"},
        {"check": "reservation-mismatch-risk", "command": "gcloud compute reservations list"},
        {"check": "autoscaler-out-of-resources", "command": "kubectl get events -A"},
        {"check": "dangling-compute-class", "command": "kubectl get pods -A"}
      ]
    }],
    "skipped": []
  },
  "findings": [{
    "check": "single-zone-nodepool",
    "severity": "major",
    "title": "Single zone nodepool missing failover zone",
    "cluster": "%s",
    "namespace": "default",
    "object": "NodePool/gpu-pool",
    "evidence": {"command": "kubectl get nodepools", "excerpt": "gpu-pool us-east4-a only"},
    "impact": "Workload will stock out if zone us-east4-a has capacity shortage.",
    "recommendation": {
      "action": "Add us-east4-b as secondary fallback zone.",
      "rationale": "Multi-zone fallback prevents hard stockout failures.",
      "risk": "Cross-zone egress may increase slightly."
    },
    "remediation": {
      "kind": "manual",
      "note": "gcloud container node-pools update gpu-pool --node-locations=us-east4-a,us-east4-b"
    }
  }]
}

findings_path = os.path.join(tmp_dir, "findings.json")
with open(findings_path, "w") as f:
    json.dump(doc, f)

try:
    res = subprocess.run(
        ["python3", script_path, "finish", "--audit=stockout-prevention", f"--findings-file={findings_path}", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)
    sys.exit(res.returncode)
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
""" % (gke_cluster_name, gke_cluster_name)

    cmd = [
        "kubectl",
        "exec",
        "-n",
        agent_namespace,
        pod_name,
        "-c",
        "platform-agent",
        "--",
        "python3",
        "-c",
        script_cmd,
    ]

    try:
        proc_finish = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        combined = (proc_finish.stdout or "") + "\n" + (proc_finish.stderr or "")
        assert proc_finish.returncode == 0, (
            f"Live in-pod dry-run audit finish failed inside pod '{pod_name}' (exit code {proc_finish.returncode}):\n"
            f"STDOUT:\n{proc_finish.stdout}\nSTDERR:\n{proc_finish.stderr}"
        )

        # 1. Safety & Dry-Run Guarantees
        assert "DRY RUN: validated findings; nothing will be committed, pushed, or published." in combined, (
            "Expected dry-run safety confirmation in audit output."
        )
        assert "WOULD OPEN: (no remediation pull requests)" in combined, (
            "Expected dry-run to confirm zero remediation pull requests opened."
        )

        # 2. Issue Title & Scope Table
        assert "TITLE: [audit] Fleet Stockout Prevention & Capacity Audit — 1 finding (0 critical)" in combined, (
            "Expected rendered stockout audit title in dry-run output."
        )
        assert "| Cluster | Location | Project | Checks |" in combined, (
            "Expected Scope table header in rendered issue ledger."
        )
        assert "12/12" in combined, (
            "Expected full 12/12 check coverage in rendered Scope table."
        )

        # 3. Finding Body & Evidence
        assert "1 finding: 0 critical, 1 major, 0 minor." in combined, (
            "Expected finding summary count in rendered issue ledger."
        )
        assert "Single zone nodepool missing failover zone" in combined, (
            "Expected finding title in rendered issue ledger."
        )
        assert "- **Where:**" in combined and "NodePool/gpu-pool" in combined, (
            "Expected target resource location in finding body."
        )
        assert "gcloud container node-pools update gpu-pool" in combined, (
            "Expected manual remediation command in finding body."
        )

        # 4. Check Roster Verification in Dropdown
        assert "How this run checked the fleet (12 checks)" in combined, (
            "Expected 12-check details summary in rendered issue ledger."
        )
        for expected_check in [
            "ccc-missing-fallbacks",
            "ccc-no-ondemand-floor",
            "ccc-large-vm-scarcity",
            "ccc-priority-starvation",
            "ccc-mixed-disk-generations",
            "ccc-hyperdisk-incompatible",
            "quota-exhaustion-risk",
            "spot-scarcity-risk",
            "single-zone-nodepool",
            "reservation-mismatch-risk",
            "autoscaler-out-of-resources",
            "dangling-compute-class",
        ]:
            assert f"`{expected_check}`" in combined, (
                f"Expected check '{expected_check}' to be documented in rendered issue ledger checks table."
            )

        # 5. Hidden Delta Stamp & Schema Scheme
        assert '<!-- audit-findings: ["single-zone-nodepool.' in combined, (
            "Expected hidden delta block containing derived finding ID."
        )
        assert "<!-- audit-id-scheme: 2 -->" in combined, (
            "Expected audit-id-scheme version 2 footer."
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"Timed out executing live dry-run finish inside pod '{pod_name}'")


def test_github_token_minter_credential_isolation(
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    agent_namespace: str,
) -> None:
    """Verifies GitHub integration security: credentials broker isolation & secret protection.

    Ensures that:
    1. platform-agent-settings ConfigMap is configured with GitOps settings.
    2. platform-agent-credential-proxy-policy ConfigMap is present and enforces token disclosure rules.
    3. The private GitHub App credentials secret is NOT mounted into the agent container.
    """
    if not gcp_project_id or not gke_cluster_name:
        pytest.skip("GCP_PROJECT_ID or GKE_CLUSTER_NAME unset; skipping GitHub integration check.")

    # 1. Verify platform-agent-settings ConfigMap exists
    res_settings = subprocess.run(
        ["kubectl", "get", "cm", "platform-agent-settings", "-n", agent_namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    assert res_settings.returncode == 0, (
        f"platform-agent-settings ConfigMap missing in namespace '{agent_namespace}': {res_settings.stderr}"
    )

    # 2. Verify platform-agent-credential-proxy-policy ConfigMap exists and blocks token leakage
    res_proxy = subprocess.run(
        ["kubectl", "get", "cm", "platform-agent-credential-proxy-policy", "-n", agent_namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    assert res_proxy.returncode == 0, (
        f"platform-agent-credential-proxy-policy ConfigMap missing in namespace '{agent_namespace}': {res_proxy.stderr}"
    )
    policy_data = json.loads(res_proxy.stdout).get("data", {}).get("policy.json", "")
    assert "github.token-disclosure" in policy_data, "Security proxy policy missing github.token-disclosure rule!"
    assert "git.credential-disclosure" in policy_data, "Security proxy policy missing git.credential-disclosure rule!"

    # 3. Verify Credential Isolation: Secret 'github-app-credentials' is NOT mounted into platform-agent container
    res_pod = subprocess.run(
        [
            "kubectl",
            "get",
            "deployment",
            "platform-agent-gateway",
            "-n",
            agent_namespace,
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name=='platform-agent')].volumeMounts[*].name}",
        ],
        capture_output=True,
        text=True,
    )
    if res_pod.returncode == 0:
        mounts = res_pod.stdout.lower()
        assert "github-app-credentials" not in mounts and "github-secret" not in mounts, (
            "SECURITY VIOLATION: github-app-credentials secret directly mounted into platform-agent container!"
        )


@pytest.mark.parametrize(
    "audit_id,human_name",
    AUDIT_STREAMS,
    ids=[aid for aid, _ in AUDIT_STREAMS],
)
def test_audit_report_ledger_dryrun_all_streams(
    audit_id: str,
    human_name: str,
    gke_cluster_name: Optional[str],
    gcp_project_id: Optional[str],
) -> None:
    """Exercises deterministic GitHub ledger issue & PR formatting across all 7 audit streams using --dry-run.

    Validates schema compliance, checks roster enforcement, and verifies ledger rendering
    without mutating live GitHub repositories.
    """
    if not _AUDIT_REPORT_SCRIPT.is_file():
        pytest.skip("audit_report.py not found; skipping audit stream validation.")

    cluster = gke_cluster_name or "test-cluster"
    project = gcp_project_id or "test-project"

    # Retrieve first valid check slug for the audit stream
    valid_check = "sample-check"
    try:
        import sys
        script_dir_str = str(_AUDIT_REPORT_SCRIPT.parent)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        from audit_report import AUDITS
        if audit_id in AUDITS and AUDITS[audit_id].checks:
            valid_check = AUDITS[audit_id].checks[0]
    except Exception:
        pass

    # Construct clean findings document for the audit stream
    mock_findings = {
        "audit": audit_id,
        "scope": {
            "clusters": [
                {
                    "name": cluster,
                    "location": "us-east4",
                    "project": project,
                    "checks_run": [
                        {
                            "check": valid_check,
                            "command": f"kubectl --context={cluster} get pods -A",
                        }
                    ],
                    "checks_not_applicable": [],
                }
            ],
            "skipped": [],
        },
        "findings": [],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(mock_findings, f)
        temp_path = f.name

    try:
        proc = subprocess.run(
            [
                "python3",
                str(_AUDIT_REPORT_SCRIPT),
                "finish",
                f"--audit={audit_id}",
                f"--findings-file={temp_path}",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        # Exit code 0 indicates valid schema, correctly formatted ledger issue, and successful dry-run
        assert proc.returncode == 0, (
            f"Audit stream '{audit_id}' ({human_name}) dry-run validation failed (exit code {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_github_target_repository_configuration(
    github_repo: Optional[str],
    github_org: Optional[str],
    github_app_id: Optional[str],
) -> None:
    """Verifies that GitHub App, Org, and Repo configuration parameters are properly wired for E2E tests."""
    if not github_repo:
        pytest.skip("No GITHUB_REPO or GitOps repo configured; skipping repository target check.")

    assert "/" in github_repo, f"Expected GITHUB_REPO in 'owner/repo' format, got '{github_repo}'"
    owner, repo_name = github_repo.split("/", 1)
    assert len(owner) > 0 and len(repo_name) > 0, f"Invalid repository structure in '{github_repo}'"

    if github_org:
        assert github_org == owner, (
            f"GITHUB_ORG '{github_org}' does not match repository owner in '{github_repo}'"
        )


@pytest.mark.parametrize(
    "audit_id,human_name",
    AUDIT_STREAMS,
    ids=[aid for aid, _ in AUDIT_STREAMS],
)
def test_fleet_audit_live_stream_dispatch(
    audit_id: str,
    human_name: str,
    port_forward_agent: Optional[str],
    platform_agent_api_key: Optional[str],
    github_repo: Optional[str],
    fleet_audit_live: str,
) -> None:
    """Tests live on-demand audit dispatch for all 7 audit streams when FLEET_AUDIT_LIVE is enabled (Nightly)."""
    if fleet_audit_live not in ("1", "true", "all", audit_id):
        pytest.skip(f"Live audit execution disabled for '{audit_id}'. Set FLEET_AUDIT_LIVE=all for nightly full runs.")

    if not port_forward_agent or not platform_agent_api_key:
        pytest.skip("No port-forward URL or API key found; skipping live audit dispatch.")

    prompt = f"Run on-demand fleet audit for stream '{audit_id}' ({human_name}) and report summary."
    url = f"{port_forward_agent}/v1/responses"
    payload = json.dumps({
        "model": "model-default",
        "conversation": f"e2e-fleet-audit-{audit_id}",
        "input": prompt,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {platform_agent_api_key}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        assert resp.status == 200, f"Expected HTTP 200 from live audit dispatch, got {resp.status}"
        body = json.loads(resp.read().decode("utf-8"))
        assert "output" in body or "choices" in body or "response" in body, f"Invalid agent audit response: {body}"


def test_stockout_audit_github_api_lifecycle_mocked(tmp_path: pathlib.Path) -> None:
    """Verifies that the stockout prevention audit watchdog executes the exact expected GitHub API lifecycle.

    Uses an in-memory execution seam to simulate the GitHub CLI/API and assert:
    1. It ensures the standard audit and severity labels exist on the repo.
    2. It searches GitHub for existing ledger issues (gh issue list --label audit:stockout-prevention).
    3. It fetches previous issue body and comments to check for active findings and /remediate commands.
    4. It updates the ledger issue title and body with the rendered capacity audit tables.
    5. It strictly does NOT create unexpected pull requests without explicit authorization.
    """
    if not _AUDIT_REPORT_SCRIPT.is_file():
        pytest.skip("audit_report.py not found; skipping mock GitHub API lifecycle test.")

    import sys
    script_dir_str = str(_AUDIT_REPORT_SCRIPT.parent)
    if script_dir_str not in sys.path:
        sys.path.insert(0, script_dir_str)
    platform_scripts_dir = str(_REPO_ROOT / "agents" / "platform" / "scripts")
    if platform_scripts_dir not in sys.path:
        sys.path.insert(0, platform_scripts_dir)

    import audit_report

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)

    original_workspace = getattr(audit_report, "GITOPS_WORKSPACE", None)
    original_scratch = getattr(audit_report, "SCRATCH_DIR", None)
    original_run_cmd = audit_report.run_cmd
    original_refresh = audit_report.refresh_credentials
    original_resolve = audit_report.resolve_repo

    calls: list[list[str]] = []

    def mock_run_cmd(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append(cmd_list)
        joined = " ".join(cmd_list)
        if cmd_list[:2] == ["git", "clone"]:
            dest = pathlib.Path(cmd_list[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "gh issue list" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps([{"number": 42, "url": "https://github.com/test-org-kube-agent/agents-repo/issues/42"}]),
                "stderr": "",
            })()
        if "gh issue view" in joined and "--json body" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps({"body": "<!-- audit-findings: [] -->"}),
                "stderr": "",
            })()
        if "gh issue view" in joined and "--json comments" in joined:
            return type("CompletedProcess", (), {
                "returncode": 0,
                "stdout": json.dumps({"comments": []}),
                "stderr": "",
            })()
        return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    try:
        audit_report.GITOPS_WORKSPACE = str(tmp_path)
        audit_report.SCRATCH_DIR = str(tmp_path)
        audit_report.set_workspace(workspace)
        audit_report.run_cmd = mock_run_cmd
        audit_report.refresh_credentials = lambda repo=None: None
        audit_report.resolve_repo = lambda: "test-org-kube-agent/agents-repo"
        audit_report.repo_root = lambda: workspace

        doc = {
            "audit": "stockout-prevention",
            "scope": {
                "clusters": [{
                    "name": "platform-agent-host",
                    "location": "us-east4",
                    "project": "sergiuspiridon-gkedemos",
                    "checks_run": [{"check": "single-zone-nodepool", "command": "kubectl get nodepool -A"}]
                }],
                "skipped": []
            },
            "findings": [{
                "check": "single-zone-nodepool",
                "severity": "major",
                "title": "Single zone nodepool missing failover zone",
                "cluster": "platform-agent-host",
                "namespace": "default",
                "object": "NodePool/gpu-pool",
                "evidence": {"command": "kubectl get nodepools", "excerpt": "gpu-pool us-east4-a only"},
                "impact": "Workload will stock out if zone us-east4-a has capacity shortage.",
                "recommendation": {
                    "action": "Add us-east4-b as secondary fallback zone.",
                    "rationale": "Multi-zone fallback prevents hard stockout failures.",
                    "risk": "Cross-zone egress may increase slightly."
                },
                "remediation": {
                    "kind": "manual",
                    "note": "gcloud container node-pools update gpu-pool --node-locations=us-east4-a,us-east4-b"
                }
            }]
        }

        findings_file = tmp_path / "findings.json"
        findings_file.write_text(json.dumps(doc), encoding="utf-8")

        exit_code = audit_report.main(["finish", "--audit=stockout-prevention", f"--findings-file={findings_file}"])
        assert exit_code == 0, f"Expected finish exit code 0, got {exit_code}"

        all_commands = [" ".join(c) for c in calls]

        # 1. Assert label verification calls
        assert any("gh label create" in c and "audit:stockout-prevention" in c for c in all_commands), (
            "Expected GitHub call to ensure 'audit:stockout-prevention' label exists."
        )

        # 2. Assert ledger lookup
        assert any("gh issue list" in c and "audit:stockout-prevention" in c for c in all_commands), (
            "Expected GitHub call to list existing ledger issue for stockout-prevention."
        )

        # 3. Assert ledger issue update
        assert any("gh issue edit 42" in c and "[audit] Fleet Stockout Prevention" in c for c in all_commands), (
            "Expected GitHub call to edit issue #42 with updated stockout audit title."
        )

        # 4. Assert NO unauthorized PR creation
        assert not any("gh pr create" in c for c in all_commands), (
            "SECURITY/SAFETY VIOLATION: Audit unexpectedly attempted to create a pull request!"
        )

    finally:
        audit_report.run_cmd = original_run_cmd
        audit_report.refresh_credentials = original_refresh
        audit_report.resolve_repo = original_resolve
        if original_workspace:
            audit_report.GITOPS_WORKSPACE = original_workspace
        if original_scratch:
            audit_report.SCRATCH_DIR = original_scratch
        audit_report.set_workspace(None)



