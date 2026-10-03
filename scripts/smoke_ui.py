"""Exercise the running local workbench in Edge and save real UI screenshots.

Run with the PatchForge server already started:
    python scripts/smoke_ui.py --url http://127.0.0.1:8770
    python scripts/smoke_ui.py --url http://127.0.0.1:8771 --require-v02

Requires the optional development dependency: pip install playwright
Uses installed Microsoft Edge; no Playwright browser download is needed.
Creates one actual demo run and downloads its patch to a temporary directory.
It never starts a paid model run or prints authentication configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import zipfile

from playwright.sync_api import expect, sync_playwright


def smoke(base_url: str, require_v02: bool = False) -> dict:
    output_dir = Path(__file__).resolve().parents[1] / "docs"
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    checks: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(base_url, wait_until="networkidle")
        assert response is not None and response.status == 200, "Workbench did not load"
        config = context.request.get(base_url + "/api/config").json()
        expect(page.locator(".task-button").first).to_be_visible()
        assert page.locator(".task-button").count() == len(config["tasks"])
        assert page.locator("#connection-error").is_hidden()
        checks.append("initial_load")

        # Task source must be browsable before creating a run; v0.1 gets a
        # readable fallback instead of a broken pane when the API is absent.
        page.locator(".task-button").first.click()
        task_files = context.request.get(base_url + "/api/tasks/" + config["tasks"][0]["id"] + "/files")
        page.locator("#tab-code").click()
        source_supported = task_files.status == 200
        if require_v02:
            assert source_supported, "v0.2 file API is required"
        if source_supported:
            expect(page.locator("#code-lines")).to_be_visible()
            assert page.locator("#file-list .file-button").count() == len(task_files.json()["files"])
            assert page.locator("#source-switch").is_hidden()
            assert page.locator("#code-lines .diff-line").count() > 0
            checks.append("original_task_file_browser")
        else:
            expect(page.locator("#code-message")).to_contain_text("当前服务版本不支持代码浏览")
            checks.append("legacy_file_api_fallback")
        assert page.locator("#issue-tags .source").is_visible()
        checks.append("task_source_distinction")

        live_buttons = page.locator(".run-button").filter(has_text="LIVE").filter(has_text="已完成")
        assert live_buttons.count() > 0, "Create a live run before checking its history"
        live_buttons.first.click()
        page.locator("#tab-diff").click()
        page.locator("#diff-content").wait_for(state="visible")
        assert page.locator("#diff-lines .diff-line").count() > 0
        assert "LIVE MODEL" in page.locator("#rail-foot").inner_text()
        assert page.locator("#mode").input_value() == "live"
        live_run_id = page.locator("#rail-foot").inner_text().split("RUN ", 1)[1]
        live_run = context.request.get(base_url + "/api/runs/" + live_run_id).json()
        if live_run.get("context_stats"):
            expect(page.locator("#context-evidence")).to_be_visible()
            expect(page.locator("#context-value")).to_contain_text(str(live_run["context_stats"]["compacted_groups"]) + " 组")
            checks.append("live_context_compaction_stats")
        page.locator("#tab-tests").click()
        assert page.locator("#test-suites .test-suite").count() == 3
        assert page.locator("#test-suites .test-suite pre").count() >= 2
        checks.append("live_history_diff_and_tests")

        page.locator("#tab-evaluation").click()
        expect(page.locator("#evaluation-content .evaluation-table").first).to_be_visible()
        report = context.request.get(base_url + "/api/evaluation").json()
        assert report.get("available") is not False
        assert page.locator("#evaluation-content .evaluation-table").first.locator("tbody tr").count() == len(report["summary"])
        if report.get("by_split"):
            assert page.locator("#evaluation-content .evaluation-split").count() == len(report["by_split"])
            for group in report["by_split"]:
                section = page.locator(".evaluation-split[data-split='" + group["split"] + "']")
                assert section.locator("tbody tr").count() == len(group["summary"])
            checks.append("evaluation_separate_source_groups")
        checks.append("evaluation_summary")

        page.locator("#tab-diff").click()
        page.screenshot(path=str(output_dir / "workbench-live.png"), full_page=True)
        if source_supported:
            page.locator("#tab-code").click()
            expect(page.locator("#code-lines")).to_be_visible()
            page.screenshot(path=str(output_dir / "workbench-code.png"), full_page=True)

        demo_index = next(index for index, task in enumerate(config["tasks"]) if task.get("demo_available", True))
        page.locator(".task-button").nth(demo_index).click()
        page.locator("#mode").select_option("demo")
        page.locator("#strategy").select_option("repair")
        with page.expect_response(lambda item: item.request.method == "POST" and item.url == base_url + "/api/runs") as created:
            page.locator("#start-button").click()
        assert created.value.status == 201
        run_id = created.value.json()["id"]
        expect(page.locator("#state-label")).to_have_text("已完成", timeout=90000)
        run = context.request.get(base_url + "/api/runs/" + run_id).json()
        assert run["mode"] == "demo" and run["status"] == "completed"
        assert run["visible"]["passed"] and run["hidden"]["passed"]
        assert "不调用模型" in page.locator("#mode-note").inner_text()
        checks.append("demo_run_completed_with_real_tests")

        if source_supported:
            page.locator("#tab-code").click()
            expect(page.locator("#code-lines")).to_be_visible()
            files = context.request.get(base_url + "/api/runs/" + run_id + "/files").json()
            changed = next(item for item in files["files"] if item["changed"])
            page.locator("#file-list .file-button").filter(has=page.locator(".file-name", has_text=changed["path"].split("/")[-1])).first.click()
            expect(page.locator("#code-file-path")).to_have_text(changed["path"])
            expect(page.locator("#code-lines")).to_be_visible()
            current = page.locator("#code-lines .line-text").all_text_contents()
            page.locator("#source-original").click()
            original = page.locator("#code-lines .line-text").all_text_contents()
            assert current != original, "Changed file original/current views should differ"
            page.locator("#source-current").click()
            assert page.locator("#code-lines .line-text").all_text_contents() == current
            checks.append("run_source_original_current_toggle")
            if run.get("workspace_sha256"):
                assert run["visible"]["workspace_sha256"] == run["workspace_sha256"]
                expect(page.locator("#verification-status")).to_have_text("可见测试已验证此版本")
                expect(page.locator("#verification-hash")).to_contain_text(run["workspace_sha256"])
                with tempfile.TemporaryDirectory(prefix="patchforge-bundle-") as temp_dir:
                    with page.expect_download() as bundle:
                        page.locator("#bundle-button").click()
                    bundle_path = Path(temp_dir) / "run.zip"
                    bundle.value.save_as(str(bundle_path))
                    with zipfile.ZipFile(bundle_path) as archive:
                        assert archive.namelist() and archive.testzip() is None
                checks.append("tested_version_fingerprint_and_bundle")
            if run.get("context_stats"):
                expect(page.locator("#context-evidence")).to_be_visible()
                checks.append("context_compaction_stats")

        with tempfile.TemporaryDirectory(prefix="patchforge-ui-") as temp_dir:
            with page.expect_download() as downloaded:
                page.locator("#download-button").click()
            patch_path = Path(temp_dir) / "smoke.patch"
            downloaded.value.save_as(str(patch_path))
            assert patch_path.read_text(encoding="utf-8") == run["diff"]
        checks.append("patch_download")

        page.locator("#tab-events").click()
        assert page.locator("#event-details .event-detail").count() == len(run["events"])
        page.locator("#event-details summary").first.click()
        assert page.locator("#event-details details[open]").count() == 1
        checks.append("event_details")

        # Return to a real model run for the narrow-screen product screenshot.
        page.locator(".run-button").filter(has_text="LIVE").filter(has_text="已完成").first.click()
        expect(page.locator("#rail-foot")).to_contain_text("LIVE MODEL")
        page.locator("#tab-diff").click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
        assert dimensions["document"] <= dimensions["viewport"], f"Horizontal overflow: {dimensions}"
        page.screenshot(path=str(output_dir / "workbench-mobile.png"), full_page=True)
        if source_supported:
            page.locator("#tab-code").click()
            expect(page.locator("#code-lines")).to_be_visible()
            dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
            assert dimensions["document"] <= dimensions["viewport"], f"Code browser overflow: {dimensions}"
        checks.append("mobile_no_horizontal_overflow")
        assert not errors, "JavaScript errors: " + "; ".join(errors)
        checks.append("no_javascript_errors")
        browser.close()
    return {"passed": True, "checks": checks, "demo_run_id": run_id, "live_run_id": live_run_id,
            "screenshots": ["docs/workbench-live.png", "docs/workbench-mobile.png"] + (["docs/workbench-code.png"] if source_supported else [])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8770")
    parser.add_argument("--require-v02", action="store_true", help="Require the v0.2 source browser API")
    args = parser.parse_args()
    print(json.dumps(smoke(args.url.rstrip("/"), args.require_v02), ensure_ascii=True, indent=2))
