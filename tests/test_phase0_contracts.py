"""Phase 0 baseline tests validating schemas, preserved rules, and workflow contracts."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

import jsonschema
import yaml

from engine.contracts import (
    AUTHORITY_MODE,
    CHANGE_TYPE_TO_IMPACT_MATRIX,
    COMPANY_MASTER_ID,
    MASTER_RECORD_SCOPE,
    PROJECT_MASTER_ID,
    load_json,
    validate_against_schema,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SCHEMAS_DIR = ROOT / "schemas"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


class Phase0ContractTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self) -> None:
        schema_files = sorted(SCHEMAS_DIR.glob("*.json"))
        expected = {
            "approval_record.schema.json",
            "document_section_output.schema.json",
            "document_update.schema.json",
            "feature_report.schema.json",
            "master_aggregation.schema.json",
            "project_config_local.schema.json",
            "project_registry.schema.json",
            "project_registry_collection.schema.json",
        }
        self.assertTrue(expected.issubset({p.name for p in schema_files}))
        for schema_path in schema_files:
            schema = load_json(schema_path)
            jsonschema.Draft202012Validator.check_schema(schema)

    def test_document_registry_preserves_documents_and_both_master_ids(self) -> None:
        doc_reg = load_json(CONFIG_DIR / "document_registry.json")
        doc_ids = [d["id"] for d in doc_reg["documents"]]
        self.assertEqual(doc_ids, [f"D{i:02d}" for i in range(1, 16)])
        self.assertEqual(doc_reg["master_record"]["id"], PROJECT_MASTER_ID)
        self.assertEqual(doc_reg["master_record"]["scope"], "project")
        self.assertEqual(
            doc_reg["master_record"]["sensitive_sections"],
            [
                "3. Team Structure",
                "4. Team Change Log (Swaps / Exits)",
                "6.3 IT Asset Requirements",
                "7. Financial Overview",
            ],
        )
        self.assertEqual(doc_reg["company_master_record"]["id"], COMPANY_MASTER_ID)
        self.assertEqual(doc_reg["company_master_record"]["scope"], "company")
        self.assertEqual(
            doc_reg["company_master_record"]["repository"],
            "unai-company-master-record",
        )

    def test_impact_matrix_preserves_rules_and_human_locks(self) -> None:
        matrix = load_json(CONFIG_DIR / "impact_matrix.json")
        self.assertGreaterEqual(len(matrix["rules"]), 12)
        for doc_id in ("D01", "D12", "D13", "D14", "MASTER", "COMPANY_MASTER"):
            self.assertIn(doc_id, matrix["never_auto_approve"])
        for field in (
            "Prepared By",
            "Reviewed By",
            "Approved By",
            "Name",
            "Signature",
            "Date of approval",
            "GO / NO-GO DECISION",
            "SIGN-OFF DECLARATION",
            "UAT acceptance",
        ):
            self.assertIn(field, matrix["always_human_fields"])

    def test_change_type_mapping_covers_all_feature_report_enums(self) -> None:
        feature_schema = load_json(SCHEMAS_DIR / "feature_report.schema.json")
        matrix = load_json(CONFIG_DIR / "impact_matrix.json")
        report_enums = set(feature_schema["properties"]["change_type"]["enum"])
        matrix_types = {rule["change_type"] for rule in matrix["rules"]}

        self.assertEqual(set(CHANGE_TYPE_TO_IMPACT_MATRIX.keys()), report_enums)
        for mapped_targets in CHANGE_TYPE_TO_IMPACT_MATRIX.values():
            for target in mapped_targets:
                self.assertIn(target, matrix_types)

    def test_project_registry_validates_against_collection_schema(self) -> None:
        registry_data = load_json(CONFIG_DIR / "project_registry.json")
        self.assertEqual(registry_data["authority_mode"], AUTHORITY_MODE)
        self.assertEqual(registry_data["master_record_scope"], MASTER_RECORD_SCOPE)
        validate_against_schema(
            registry_data, "project_registry_collection.schema.json", SCHEMAS_DIR
        )

    def test_sample_payloads_validate_against_new_schemas(self) -> None:
        sample_report = {
            "project_id": "UTDA_PILOT",
            "feature_id": "FEAT-001",
            "feature_name": "Canonical JSON Report Support",
            "change_type": "api_change",
            "summary": "Added structured endpoint validation.",
            "implementation_status": "implemented",
            "evidence": [
                {"kind": "commit", "reference": "abc1234", "description": "Merged PR #1"}
            ],
        }
        validate_against_schema(sample_report, "feature_report.schema.json", SCHEMAS_DIR)

        sample_local_cfg = {
            "schema_version": "1.0",
            "project_id": "UTDA_PILOT",
            "project_name": "Pilot App",
            "authority_mode": "GITHUB_AUTHORITY",
            "master_record_id": "MASTER",
            "docs_dir": "documentation/Project Documentation",
            "template_source": {
                "repository": "UNAI-TECH/unai-project-documentation",
                "version": "templates-v1.0.0",
                "manifest_path": "documentation/template-manifest.json",
            },
            "reports": {
                "final_dir": "documentation/reports/final",
                "canonical_format": "json",
                "companion_markdown": True,
            },
        }
        validate_against_schema(
            sample_local_cfg, "project_config_local.schema.json", SCHEMAS_DIR
        )

        sample_section = {
            "document_id": "D06",
            "section": "API SPECIFICATIONS",
            "content": "Added /v1/reports validation.",
            "evidence_refs": ["commit:abc1234"],
            "unknowns": [],
            "proposed_change_summary": "Documented /v1/reports schema validation.",
        }
        validate_against_schema(
            sample_section, "document_section_output.schema.json", SCHEMAS_DIR
        )

        sample_approval = {
            "project_id": "UTDA_PILOT",
            "document_id": "MASTER",
            "section_id": "5. Project Progress",
            "change_id": "CHG-001",
            "approver_identity": "lead@unai.tech",
            "approver_role": "tech_lead",
            "decision": "approved",
            "timestamp": "2026-11-26T12:00:00Z",
            "evidence_reference": "pr:12",
        }
        validate_against_schema(
            sample_approval, "approval_record.schema.json", SCHEMAS_DIR
        )

        sample_aggregation = {
            "aggregation_id": "AGG-001",
            "idempotency_key": "UTDA_PILOT:MASTER:abc1234",
            "target_document_id": "COMPANY_MASTER",
            "source_project_id": "UTDA_PILOT",
            "source_document_id": "MASTER",
            "source_commit_sha": "abc1234",
            "source_version": "1",
            "aggregation_timestamp": "2026-11-26T12:05:00Z",
            "evidence_refs": ["commit:abc1234", "pr:12"],
            "updates": [
                {
                    "section": "2. Project Status & Milestones",
                    "action": "append_entry",
                    "content": "UTDA_PILOT milestone updated.",
                    "evidence_refs": ["commit:abc1234"],
                }
            ],
        }
        validate_against_schema(
            sample_aggregation, "master_aggregation.schema.json", SCHEMAS_DIR
        )

    def test_workflows_and_caller_contracts(self) -> None:
        trigger_wf = yaml.safe_load(
            (WORKFLOWS_DIR / "documentation-trigger.yml").read_text(encoding="utf-8")
        )
        reusable_wf = yaml.safe_load(
            (WORKFLOWS_DIR / "reusable-docs.yml").read_text(encoding="utf-8")
        )
        caller_wf = yaml.safe_load(
            (ROOT / "examples" / "project-repository-caller.yml").read_text(
                encoding="utf-8"
            )
        )
        baseline_wf = yaml.safe_load(
            (WORKFLOWS_DIR / "ci-baseline.yml").read_text(encoding="utf-8")
        )

        # Ensure broken requirements-utda.txt reference is gone
        trigger_text = (WORKFLOWS_DIR / "documentation-trigger.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("requirements-utda.txt", trigger_text)
        self.assertIn("documentation/reports/final/*.json", trigger_text)

        # Ensure reusable workflow checks out Repo C, Repo B, and Repo A separately
        doc_steps = reusable_wf["jobs"]["documentation"]["steps"]
        checkout_paths = [
            s.get("with", {}).get("path")
            for s in doc_steps
            if str(s.get("uses", "")).startswith("actions/checkout")
        ]
        self.assertEqual(
            checkout_paths,
            ["workspace/repo", "workspace/utda", "workspace/templates"],
        )
        self.assertTrue(reusable_wf[True]["workflow_call"]["inputs"]["dry_run"]["default"])

        # Ensure caller uses UNAI-TECH org and JSON path
        self.assertIn(
            "UNAI-TECH/unai-project-documentation-automation/.github/workflows/reusable-docs.yml@v1",
            caller_wf["jobs"]["utda"]["uses"],
        )
        self.assertIn(
            "documentation/reports/final/*.json",
            caller_wf[True]["push"]["paths"],
        )
        self.assertIn("validate-contracts", baseline_wf["jobs"])


if __name__ == "__main__":
    unittest.main()
