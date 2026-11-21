import os
from playwright.sync_api import expect


def test_repro(page):
    page.goto(os.environ["BASE_URL"])

    new_todo = page.get_by_placeholder("What needs to be done?")
    new_todo.click()
    new_todo.fill("Buy milk")
    new_todo.press("Enter")

    expect(new_todo).to_have_value("")
    expect(page.locator(".todo-list li")).to_have_count(1)
    expect(page.locator(".todo-list li")).to_contain_text("Buy milk")
    expect(page.locator(".footer")).to_be_visible()
    expect(page.locator(".todo-count")).to_have_text("1 item left")
    expect(page.locator(".filters a", has_text="All")).to_be_visible()
    expect(page.locator(".filters a", has_text="Active")).to_be_visible()
    expect(page.locator(".filters a", has_text="Completed")).to_be_visible()
