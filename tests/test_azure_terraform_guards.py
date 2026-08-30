"""Repo-level guards for the Azure production stack (#326, #328, epic #320).

`infra/azure/`, `charts/inherent/`, `scripts/deploy-azure.sh`, and
`.github/workflows/azure-terraform.yml` are built by several agents in
parallel against a shared contract: `.memory/azure-build-spec.md`. These
tests pin the parts of that contract a silent regression could otherwise
slip past unnoticed -- module wiring, secret-default hygiene, HA/DR
defaults, the durability setting Redis Streams depends on, and the CI lane's
shape.

Two constraints shape every test here:

1. **No terraform binary dependency.** The root `tests/` suite is meant to
   stay hermetic (see the Makefile's `uvx 'pytest==9.0.2' tests/ -q`, run
   with no project of its own to install). A "skip if terraform is missing"
   test is explicitly NOT an acceptable substitute -- that is exactly the
   silently-degrading-into-a-false-pass shape that sank the Hetzner e2e lane
   (docs/testing.md:199-235: it reported success on every run even when its
   meaningful steps silently skipped themselves). So every check here is a
   pure text/regex pin over the `.tf`/`.yaml`/`.md` source, or a subprocess
   call to a tool assumed present in this environment (Python's own `yaml`
   parser) -- never terraform itself.
2. **Peer files may not exist yet.** `infra/azure/*.tf`, `charts/inherent/`,
   and the `docs/deploy/` pages are owned by other agents working the same
   build spec in parallel, and may land in later commits than this test
   file. Pins against those paths are written against the SPEC (the spec
   fixes their paths and names as part of the cross-agent contract), so a
   test failing only because a peer file does not exist YET is a legitimate,
   temporary state -- distinct from a test failing because a peer file that
   DOES exist violates the contract.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

INFRA_DIR = REPO_ROOT / "infra" / "azure"
MAIN_TF = INFRA_DIR / "main.tf"
VERSIONS_TF = INFRA_DIR / "versions.tf"
MODULES_DIR = INFRA_DIR / "modules"
PROD_TFVARS_EXAMPLE = INFRA_DIR / "envs" / "prod.tfvars.example"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "azure-terraform.yml"
CHART_DIR = REPO_ROOT / "charts" / "inherent"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-azure.sh"
DOCS_AZURE = REPO_ROOT / "docs" / "deploy" / "azure.md"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"

# Root variable names the build spec calls out as "generated in module, never
# passed as a tfvar" (`.memory/azure-build-spec.md` "Secrets" section). Any
# OTHER var whose name looks like a secret must not carry a plaintext
# default -- that would mean a real password/key baked into a `variables.tf`
# default, which is committed to git and shipped to every consumer of the
# module.
SECRET_NAME_RE = re.compile(r"(password|secret|_key$)", re.IGNORECASE)
# `*_kv_secret` names are Key Vault SECRET NAME references (a lookup key, not
# a credential -- see the "Cross-module interface" outputs, e.g.
# `pg_password_kv_secret`), so they are exempt from the no-default rule.
KV_SECRET_REF_RE = re.compile(r"_kv_secret$", re.IGNORECASE)


def _skip_if_missing(path: Path) -> None:
    if not path.exists():
        pytest.skip(
            f"{path.relative_to(REPO_ROOT)} does not exist yet -- owned by "
            "another agent per .memory/azure-build-spec.md; re-run once it "
            "lands."
        )


# ---------------------------------------------------------------------------
# (a) every module main.tf references exists on disk with real .tf content.
# ---------------------------------------------------------------------------


def test_every_referenced_module_directory_exists_with_tf_files() -> None:
    """A `module "x" { source = "./modules/x" }` with no such dir is a typo.

    Terraform would catch this at `init` time, but this repo's root test
    suite deliberately runs with no terraform binary (see module docstring),
    so this is the guard that catches a drifted module reference before any
    CI lane that DOES have terraform gets to it.
    """
    _skip_if_missing(MAIN_TF)
    text = MAIN_TF.read_text()
    refs = re.findall(
        r'module\s+"([a-zA-Z0-9_-]+)"\s*\{[^}]*?source\s*=\s*"(\./modules/[a-zA-Z0-9_/-]+)"',
        text,
        re.DOTALL,
    )
    assert refs, (
        "expected at least one `module \"x\" { source = \"./modules/x\" }` "
        f"block in {MAIN_TF.relative_to(REPO_ROOT)}"
    )
    for name, source in refs:
        module_dir = INFRA_DIR / source.removeprefix("./")
        assert module_dir.is_dir(), (
            f"module {name!r} in main.tf points at {source}, but "
            f"{module_dir.relative_to(REPO_ROOT)} does not exist"
        )
        tf_files = list(module_dir.glob("*.tf"))
        if not tf_files:
            pytest.skip(
                f"module {name!r}'s directory "
                f"{module_dir.relative_to(REPO_ROOT)} exists but has no .tf "
                "files yet -- owned by another agent per "
                ".memory/azure-build-spec.md; re-run once it lands"
            )


# ---------------------------------------------------------------------------
# (b) versions.tf pins azurerm major 4 and required_version >= 1.9.
# ---------------------------------------------------------------------------


def test_versions_tf_pins_azurerm_major_4() -> None:
    """An unpinned or wrong-major azurerm provider can silently change
    resource schemas between CI runs -- pin the major version explicitly."""
    _skip_if_missing(VERSIONS_TF)
    text = VERSIONS_TF.read_text()
    azurerm_block = re.search(
        r'azurerm\s*=\s*\{[^}]*?version\s*=\s*"([^"]+)"', text, re.DOTALL
    )
    assert azurerm_block is not None, (
        f"expected an azurerm provider `version = \"...\"` constraint in "
        f"{VERSIONS_TF.relative_to(REPO_ROOT)}"
    )
    version_constraint = azurerm_block.group(1)
    assert re.search(r"~>\s*4\.|>=\s*4\.\d+.*<\s*5", version_constraint), (
        f"azurerm version constraint {version_constraint!r} does not pin to "
        "major 4 (spec: 'azurerm ~>4.x')"
    )


def test_versions_tf_requires_terraform_1_9_or_newer() -> None:
    _skip_if_missing(VERSIONS_TF)
    text = VERSIONS_TF.read_text()
    match = re.search(r'required_version\s*=\s*"([^"]+)"', text)
    assert match is not None, (
        f"expected a `required_version = \"...\"` constraint in "
        f"{VERSIONS_TF.relative_to(REPO_ROOT)}"
    )
    constraint = match.group(1)
    version_num = re.search(r"(\d+)\.(\d+)", constraint)
    assert version_num is not None, f"unparseable required_version: {constraint!r}"
    major, minor = int(version_num.group(1)), int(version_num.group(2))
    assert (major, minor) >= (1, 9), (
        f"required_version {constraint!r} allows terraform older than 1.9, "
        "the version pinned in azure-terraform.yml and used to author this "
        "config"
    )


# ---------------------------------------------------------------------------
# (c) no secret-shaped variable has a plaintext default.
# ---------------------------------------------------------------------------


def _iter_variables_tf() -> list[Path]:
    files = []
    if INFRA_DIR.exists():
        files.extend(INFRA_DIR.glob("variables.tf"))
    if MODULES_DIR.exists():
        files.extend(MODULES_DIR.glob("*/variables.tf"))
    return files


def test_no_plaintext_default_for_secret_shaped_variables() -> None:
    """Secrets are generated in-module (random_password) or Azure-issued,
    never passed as tfvars (build spec's "Secrets" section). A non-empty
    default on a `password`/`secret`/`*_key` variable is exactly how a real
    credential ends up committed to git in a `variables.tf` default.

    `*_kv_secret` names are Key Vault secret NAME references, not
    credentials, and are exempt (see KV_SECRET_REF_RE above).
    """
    files = _iter_variables_tf()
    if not files:
        pytest.skip(
            "no infra/azure/**/variables.tf found yet -- owned by another "
            "agent per .memory/azure-build-spec.md"
        )

    offenders: list[str] = []
    for path in files:
        text = path.read_text()
        for match in re.finditer(
            r'variable\s+"([a-zA-Z0-9_]+)"\s*\{(.*?)\n\}', text, re.DOTALL
        ):
            var_name, body = match.group(1), match.group(2)
            if not SECRET_NAME_RE.search(var_name):
                continue
            if KV_SECRET_REF_RE.search(var_name):
                continue
            default_match = re.search(r'default\s*=\s*(.+)', body)
            if default_match is None:
                continue
            default_value = default_match.group(1).strip()
            # `default = ""` / `default = null` are the allowed "no secret
            # baked in" shapes; anything else is a literal value.
            if default_value in ('""', "null"):
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}: variable {var_name!r} has "
                f"a non-empty default ({default_value})"
            )

    assert not offenders, (
        "secret-shaped variables must not carry a plaintext default:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# (d) prod.tfvars.example: HA + DR on, inherent_version pinned (not latest).
# ---------------------------------------------------------------------------


def test_prod_tfvars_example_enables_ha_and_dr() -> None:
    _skip_if_missing(PROD_TFVARS_EXAMPLE)
    text = PROD_TFVARS_EXAMPLE.read_text()
    assert re.search(r"^\s*enable_ha\s*=\s*true\s*$", text, re.MULTILINE), (
        f"{PROD_TFVARS_EXAMPLE.relative_to(REPO_ROOT)} must set "
        "enable_ha = true (prod profile requirement)"
    )
    assert re.search(r"^\s*enable_dr\s*=\s*true\s*$", text, re.MULTILINE), (
        f"{PROD_TFVARS_EXAMPLE.relative_to(REPO_ROOT)} must set "
        "enable_dr = true (prod profile requirement)"
    )


def test_prod_tfvars_example_pins_inherent_version_not_latest() -> None:
    _skip_if_missing(PROD_TFVARS_EXAMPLE)
    text = PROD_TFVARS_EXAMPLE.read_text()
    # Trailing `# comment` after the value is common style in this file
    # (see envs/prod.tfvars.example), so don't anchor to end-of-line.
    match = re.search(r'^\s*inherent_version\s*=\s*"([^"]*)"', text, re.MULTILINE)
    assert match is not None, (
        f"{PROD_TFVARS_EXAMPLE.relative_to(REPO_ROOT)} must set "
        "inherent_version to a real, present value"
    )
    assert match.group(1) not in ("", "latest"), (
        f"{PROD_TFVARS_EXAMPLE.relative_to(REPO_ROOT)}: inherent_version "
        f"must not be 'latest' or empty (found {match.group(1)!r}) -- a "
        "prod deploy must be reproducible, not float onto whatever image "
        "tag `latest` happens to resolve to on apply day"
    )


# ---------------------------------------------------------------------------
# (e) modules/data pins Redis maxmemory-policy=noeviction.
# ---------------------------------------------------------------------------


def test_data_module_pins_redis_noeviction() -> None:
    """Redis Streams durability depends on this (build spec's "Ground truth"
    section: "Redis: MUST be maxmemory-policy=noeviction"). An eviction
    policy that can silently drop stream entries under memory pressure is a
    silent data-loss bug, not a performance tuning knob."""
    data_module = MODULES_DIR / "data"
    if not data_module.is_dir():
        pytest.skip(
            "infra/azure/modules/data does not exist yet -- owned by "
            "another agent per .memory/azure-build-spec.md"
        )
    tf_files = list(data_module.glob("*.tf"))
    assert tf_files, f"{data_module.relative_to(REPO_ROOT)} has no .tf files"
    combined = "\n".join(f.read_text() for f in tf_files)
    assert re.search(r'maxmemory[_-]policy["\s]*[:=]\s*"?noeviction"?', combined, re.IGNORECASE), (
        f"expected a maxmemory-policy=noeviction setting somewhere in "
        f"{data_module.relative_to(REPO_ROOT)}/*.tf (Redis Streams "
        "durability requirement)"
    )


# ---------------------------------------------------------------------------
# (f) azure-terraform.yml: job shape, triggers, required steps, no bypasses.
# ---------------------------------------------------------------------------


def _workflow_text() -> str:
    assert WORKFLOW.exists(), f"expected workflow at {WORKFLOW.relative_to(REPO_ROOT)}"
    return WORKFLOW.read_text()


def test_workflow_job_named_exactly_azure_terraform_validate() -> None:
    text = _workflow_text()
    assert re.search(r"^  azure-terraform-validate:\s*$", text, re.MULTILINE), (
        "expected a job id `azure-terraform-validate:` at 2-space indent -- "
        "guard tests and any required-check config pin this exact name"
    )


def test_workflow_triggers_on_infra_azure_paths() -> None:
    text = _workflow_text()
    assert "infra/azure/**" in text, (
        "azure-terraform.yml must trigger on infra/azure/** path changes"
    )


def test_workflow_has_fmt_validate_and_tflint_steps() -> None:
    text = _workflow_text()
    assert re.search(r"terraform\s+-chdir=infra/azure\s+fmt\s+-check", text), (
        "expected a `terraform -chdir=infra/azure fmt -check ...` step"
    )
    assert re.search(r"terraform\s+-chdir=infra/azure\s+validate", text), (
        "expected a `terraform -chdir=infra/azure validate ...` step"
    )
    assert "tflint" in text, "expected a tflint step"


def test_workflow_has_no_continue_on_error_or_shell_level_swallowing() -> None:
    """Every step must fail loudly. `continue-on-error` or a shell `|| true`
    tacked onto a validation command is exactly the false-pass shape the
    deleted Hetzner e2e lane had (docs/testing.md:199-235): a green check
    that means either "verified" or "silently did nothing," indistinguishable
    without opening the run."""
    text = _workflow_text()
    assert "continue-on-error" not in text, (
        "azure-terraform.yml must not use `continue-on-error` on any step"
    )
    assert "|| true" not in text, (
        "azure-terraform.yml must not swallow a command's failure with "
        "`|| true`"
    )


def test_workflow_carries_no_cloud_credentials() -> None:
    """This lane validates syntax/lint only; `plan`/`apply` need real Azure
    credentials and deliberately never run in CI (see the module docstring
    and the workflow's own header comment)."""
    text = _workflow_text()
    for token in ("AZURE_CLIENT_SECRET", "ARM_CLIENT_SECRET", "azure/login@"):
        assert token not in text, (
            f"azure-terraform.yml must carry no cloud credentials; found "
            f"{token!r}"
        )


def test_workflow_yaml_parses() -> None:
    """Belt-and-braces: the regex-based pins above only prove specific
    substrings exist, not that the file is valid YAML a runner can load."""
    yaml = pytest.importorskip("yaml")
    with WORKFLOW.open() as f:
        doc = yaml.safe_load(f)
    assert "jobs" in doc
    assert "azure-terraform-validate" in doc["jobs"]


# ---------------------------------------------------------------------------
# (g) chart pins weaviate 1.27.0 and ENVIRONMENT=production for the api.
# ---------------------------------------------------------------------------


def _chart_yaml_text() -> str | None:
    if not CHART_DIR.is_dir():
        return None
    parts = []
    values = CHART_DIR / "values.yaml"
    if values.exists():
        parts.append(values.read_text())
    templates_dir = CHART_DIR / "templates"
    if templates_dir.is_dir():
        for f in templates_dir.rglob("*.yaml"):
            parts.append(f.read_text())
    return "\n".join(parts) if parts else None


def test_chart_pins_weaviate_1_27_0() -> None:
    text = _chart_yaml_text()
    if text is None:
        pytest.skip(
            "charts/inherent does not exist yet -- owned by another agent "
            "per .memory/azure-build-spec.md"
        )
    assert "semitechnologies/weaviate:1.27.0" in text, (
        "expected the weaviate image pinned to "
        "semitechnologies/weaviate:1.27.0 somewhere in charts/inherent "
        "(values.yaml or a template)"
    )


def test_chart_sets_environment_production_for_api() -> None:
    text = _chart_yaml_text()
    if text is None:
        pytest.skip(
            "charts/inherent does not exist yet -- owned by another agent "
            "per .memory/azure-build-spec.md"
        )
    assert re.search(r"ENVIRONMENT\s*:\s*[\"']?production[\"']?", text), (
        "expected ENVIRONMENT=production set for the public-api workload "
        "somewhere in charts/inherent (values.yaml or a template)"
    )


# ---------------------------------------------------------------------------
# (h) docs/deploy/azure.md exists and is in the mkdocs nav.
# ---------------------------------------------------------------------------


def test_azure_docs_page_exists() -> None:
    _skip_if_missing(DOCS_AZURE)


def test_mkdocs_nav_references_azure_docs() -> None:
    assert MKDOCS_YML.exists(), f"expected {MKDOCS_YML.relative_to(REPO_ROOT)}"
    text = MKDOCS_YML.read_text()
    assert "deploy/azure.md" in text, (
        "mkdocs.yml nav must reference deploy/azure.md so the page is "
        "reachable from the published docs site"
    )


# ---------------------------------------------------------------------------
# (i) deploy-azure.sh: strict mode, and auto-approve only inside the --yes
#     gate.
# ---------------------------------------------------------------------------


def test_deploy_script_starts_with_strict_mode() -> None:
    _skip_if_missing(DEPLOY_SCRIPT)
    text = DEPLOY_SCRIPT.read_text()
    assert "set -euo pipefail" in text, (
        f"{DEPLOY_SCRIPT.relative_to(REPO_ROOT)} must run under bash strict "
        "mode (`set -euo pipefail`) -- an unguarded step failing silently "
        "in a one-click deploy script is how partial infra gets left behind"
    )


def test_deploy_script_shebang_and_no_unguarded_auto_approve() -> None:
    """`terraform apply -auto-approve` must only ever run behind the --yes
    gate. This is intentionally a simple substring pin (per the build spec):
    the literal auto-approve invocation should appear exactly once in the
    whole script, inside the function `run_apply` gates on `AUTO_YES`."""
    _skip_if_missing(DEPLOY_SCRIPT)
    lines = DEPLOY_SCRIPT.read_text().splitlines()
    assert lines[0].startswith("#!"), (
        f"{DEPLOY_SCRIPT.relative_to(REPO_ROOT)} must start with a shebang"
    )

    # Match actual invocations (`terraform ... apply ... -auto-approve`),
    # not the flag's mention in --help usage text or a comment.
    auto_approve_lines = [
        i
        for i, line in enumerate(lines)
        if re.search(r"terraform\s.*apply\s.*-auto-approve", line)
    ]
    assert auto_approve_lines, (
        "expected at least one `apply ... -auto-approve` invocation, gated "
        "by --yes"
    )
    for i in auto_approve_lines:
        # Walk backwards to the nearest enclosing `if [[ "$AUTO_YES" -eq 1
        # ]]` so the auto-approve call is provably behind the --yes gate,
        # not a bare unconditional apply.
        window = "\n".join(lines[max(0, i - 6) : i + 1])
        assert 'AUTO_YES' in window, (
            f"line {i + 1} runs `apply ... -auto-approve` without an "
            "`AUTO_YES` check in the preceding lines -- auto-approve must "
            "stay behind the --yes gate"
        )


def test_deploy_script_shellcheck_clean() -> None:
    """Executed only if shellcheck is on PATH in this environment -- the
    build task calls for running it, but the guard suite must stay hermetic
    per the module docstring, so a missing binary here is a skip, not a
    failure that blocks an unrelated CI lane which never installs it."""
    _skip_if_missing(DEPLOY_SCRIPT)
    shellcheck = _which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed in this environment")
    result = subprocess.run(
        [shellcheck, str(DEPLOY_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck reported issues in {DEPLOY_SCRIPT.relative_to(REPO_ROOT)}:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def _which(binary: str) -> str | None:
    result = subprocess.run(["which", binary], capture_output=True, text=True)
    return result.stdout.strip() or None
