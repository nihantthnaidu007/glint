import os
from playwright.sync_api import expect


def test_repro(page):
    page.goto(os.environ["BASE_URL"])

    new_todo = page.get_by_placeholder("What needs to be done?")
    new_todo.click()
    new_todo.fill("buy milk")
    new_todo.press("Enter")

    label = page.locator(".todo-list li label", has_text="buy milk")
    label.dblclick()

    edit_input = page.locator(".todo-list li input.edit")
    edit_input.press("Enter")

    expect(edit_input).to_have_count(0)
    checkbox = page.locator(".todo-list li input.toggle")
    expect(checkbox).to_be_visible()
    expect(checkbox).not_to_be_checked()
    expect(label).to_be_visible()
    expect(label).to_have_text("buy milk")
    expect(page.locator(".todo-count")).to_have_text("1 item left")
