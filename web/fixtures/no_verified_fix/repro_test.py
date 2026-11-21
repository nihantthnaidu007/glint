import os
from playwright.sync_api import expect


def test_repro(page):
    page.goto(os.environ["BASE_URL"])
    input_box = page.locator(".new-todo")
    input_box.fill("Buy milk")
    input_box.press("Enter")
    input_box.fill("Write tests")
    input_box.press("Enter")

    first_toggle = page.locator(".todo-list li").first.locator(".toggle")
    first_toggle.check()

    expect(page.locator(".todo-count")).to_contain_text("1 item left")
