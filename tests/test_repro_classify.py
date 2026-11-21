"""junit.xml classification and repro source checks. No network, no browser."""
from __future__ import annotations

from glint import repro

ASSERTION_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" time="2.9">
<testcase classname="repro_test" name="test_repro[chromium]" time="2.8">
<failure message="AssertionError: Locator expected to have count '2'&#10;Actual value: 1">def test_repro(page):
&gt;       expect(page.locator(".todo-list li")).to_have_count(2)
E       AssertionError: Locator expected to have count '2'</failure>
</testcase></testsuite></testsuites>"""

TIMEOUT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" time="3.4">
<testcase classname="repro_test" name="test_repro[chromium]" time="3.3">
<failure message="playwright._impl._errors.TimeoutError: Locator.click: Timeout 3000ms exceeded.">trace</failure>
</testcase></testsuite></testsuites>"""

COLLECTION_ERROR_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1" time="0.1">
<testcase classname="" name="repro_test" time="0.0">
<error message="collection failure">SyntaxError: invalid syntax</error>
</testcase></testsuite></testsuites>"""

PASSED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="1.2">
<testcase classname="repro_test" name="test_repro[chromium]" time="1.1" />
</testsuite></testsuites>"""

EMPTY_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="0" time="0.0" />
</testsuites>"""

GOOD_TEST = '''import os
from playwright.sync_api import expect


def test_repro(page):
    page.goto(os.environ["BASE_URL"])
    expect(page.locator(".todo-list li")).to_have_count(2)
'''


def test_assertion_failure() -> None:
    outcome, detail = repro.classify_junit(ASSERTION_XML)
    assert outcome == "failed_assertion"
    assert detail.startswith("AssertionError: Locator expected to have count")


def test_timeout_is_failed_other() -> None:
    outcome, detail = repro.classify_junit(TIMEOUT_XML)
    assert outcome == "failed_other"
    assert "TimeoutError" in detail


def test_collection_error_is_failed_other() -> None:
    outcome, _ = repro.classify_junit(COLLECTION_ERROR_XML)
    assert outcome == "failed_other"


def test_passed() -> None:
    assert repro.classify_junit(PASSED_XML) == ("passed", "")


def test_no_tests_or_bad_xml_is_failed_other() -> None:
    assert repro.classify_junit(EMPTY_XML)[0] == "failed_other"
    assert repro.classify_junit("not xml")[0] == "failed_other"


def test_detail_is_capped() -> None:
    long_xml = ASSERTION_XML.replace("Actual value: 1", "x" * 2000)
    _, detail = repro.classify_junit(long_xml)
    assert len(detail) == repro.DETAIL_CHARS


def test_strip_fences() -> None:
    fenced = "Here is the test:\n```python\n" + GOOD_TEST + "```\nDone."
    assert repro.strip_fences(fenced) == GOOD_TEST
    assert repro.strip_fences(GOOD_TEST) == GOOD_TEST


def test_validate_repro_source() -> None:
    assert repro.validate_repro_source(GOOD_TEST) is None
    assert "test_repro" in (repro.validate_repro_source("def test_other(page):\n    pass\n") or "")
    assert "time.sleep" in (repro.validate_repro_source(GOOD_TEST + "    time.sleep(1)\n") or "")
    assert "syntax" in (repro.validate_repro_source(GOOD_TEST + "    expect(\n") or "")
    no_goto = GOOD_TEST.replace('page.goto(os.environ["BASE_URL"])', "page.goto('http://x')")
    assert "BASE_URL" in (repro.validate_repro_source(no_goto) or "")
