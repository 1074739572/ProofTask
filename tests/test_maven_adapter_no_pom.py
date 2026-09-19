"""The Maven adapter must collect JUnit tests even before the module pom exists.

A test-first Goal writes ``<module>/src/test/java/...`` before the
implementation worker creates ``<module>/pom.xml``.  ``_test_roots`` used to
depend on ``pom.xml`` discovery, so the catalog came back empty and the bound
selectors never resolved.
"""
from harness.verification.adapters import VerificationContext
from harness.verification.maven_adapter import MavenTestAdapter


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discover_collects_junit_without_pom(tmp_path):
    test_java = (
        tmp_path / "batch-summary-domain" / "src" / "test" / "java"
        / "com" / "nexus" / "batchsummary" / "domain"
    )
    _write(
        test_java / "BatchSummarySchemaDdlTest.java",
        "package com.nexus.batchsummary.domain;\n"
        "import org.junit.jupiter.api.Test;\n"
        "class BatchSummarySchemaDdlTest {\n"
        "  @Test void createsExactlyFiveCoreTables() {}\n"
        "  @Test void usesUtf8mb4UnicodeCiCollation() {}\n"
        "}\n",
    )
    adapter = MavenTestAdapter(tmp_path)
    catalog = adapter.discover(VerificationContext(tmp_path, command="mvn -q test"))

    assert catalog.available, "catalog must collect the test without a pom.xml"
    assert len(catalog.selectors) == 2
    assert any(
        "BatchSummarySchemaDdlTest#createsExactlyFiveCoreTables" in selector
        for selector in catalog.selectors
    )
