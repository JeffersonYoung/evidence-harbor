"""Fail CI when a promised real-service test is absent, skipped or unsuccessful."""

import argparse
from pathlib import Path
from xml.etree import ElementTree

REQUIRED = {
    "durable": {
        "tests.test_postgres_domain::test_postgresql_fts_indexes_and_retrieves_english_and_chinese",
        "tests.test_postgres_domain::test_postgresql_concurrent_publication_has_exactly_one_cas_winner",
        "tests.test_postgres_domain::test_postgresql_worker_redelivery_does_not_duplicate_assets",
        "tests.test_postgres_domain::test_postgresql_cached_model_response_recovers_after_transaction_failure",
        "tests.test_postgres_domain::test_postgresql_vector_http_runner_publishes_generation_and_hybrid_results",
        "tests.test_initial_report_publication::test_postgresql_concurrent_initial_publications_have_exactly_one_winner",
        "tests.test_vector_integration::test_real_vector_generation_scoping_and_version",
        "tests.test_temporal_integration::test_temporal_durable_dispatch_retry_and_idempotency",
        "tests.test_temporal_integration::test_real_temporal_schedule_reconcile_and_pause",
        "tests.test_temporal_integration::test_terminal_workflow_failure_then_explicit_api_retry_new_generation",
        "tests.test_temporal_integration::test_new_workflow_code_replays_real_history_created_without_heartbeat_timeout",
        "tests.test_temporal_integration::test_worker_process_loss_recovers_on_real_heartbeat_timeout_without_clock_changes",
    },
    "s3": {
        "tests.test_s3_integration::test_live_s3_recovery_set_roundtrip_is_not_independent_restore_acceptance",
        "tests.test_s3_integration::test_live_s3_upload_pipeline_evidence_export_and_reprocess",
        "tests.test_s3_integration::test_live_s3_immutable_deduplicated_put",
    },
}


def validate_report(suite: str, report: Path) -> int:
    cases = list(ElementTree.parse(report).iter("testcase"))
    seen = set()
    for case in cases:
        identifier = f"{case.get('classname', '')}::{case.get('name', '')}"
        if identifier in seen:
            raise ValueError(f"Duplicate test result: {identifier}")
        seen.add(identifier)
        if any(case.find(status) is not None for status in ("skipped", "failure", "error")):
            raise ValueError(f"Real-service suite contains a skipped or unsuccessful case: {identifier}")
    missing = REQUIRED[suite] - seen
    if missing:
        raise ValueError("Missing required real-service cases: " + ", ".join(sorted(missing)))
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=REQUIRED)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    count = validate_report(args.suite, args.report)
    print(f"{len(REQUIRED[args.suite])} required {args.suite} real-service tests passed ({count} total)")


if __name__ == "__main__":
    main()
