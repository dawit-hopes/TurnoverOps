import json

from turnoverops.integrations.scheduling_agent import AgentCompletion
from tests.conftest import STAYS, create_cleaner, create_property, list_tasks, login, sync


def _conflict(client, headers):
    prop = create_property(client, headers)
    sync(client, headers, prop["id"], STAYS)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["status"] == "at_risk")
    return prop, task


def _mock_intent(monkeypatch, action, cleaner_name=None):
    payload = {"action": action, "cleaner_name": cleaner_name}

    def complete(_self, *, system, user, settings):
        return AgentCompletion(text=json.dumps(payload))

    monkeypatch.setattr("turnoverops.integrations.scheduling_agent.GeminiSchedulingClient.complete", complete)


def _chat(client, headers, task_id, message):
    return client.post(f"/tasks/{task_id}/conflict-chat", json={"message": message}, headers=headers)


def test_conflict_chat_reassigns_a_valid_cleaner(client, monkeypatch):
    headers = login(client)
    _prop, task = _conflict(client, headers)
    sara = create_cleaner(client, headers, "Sara", work_start="09:00:00", work_end="17:00:00")
    client.app.state.settings.gemini_api_key = "test-key"
    _mock_intent(monkeypatch, "assign_cleaner", "Sara")

    response = _chat(client, headers, task["id"], "Assign this to Sara.")

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "assigned"
    assert response.json()["cleaner_id"] == sara["id"]
    updated = next(item for item in list_tasks(client, headers, task["property_id"]) if item["id"] == task["id"])
    assert updated["status"] == "confirmed"
    assert updated["assignment"]["cleaner_id"] == sara["id"]


def test_conflict_chat_rejects_a_cleaner_outside_working_hours(client, monkeypatch):
    headers = login(client)
    _prop, task = _conflict(client, headers)
    create_cleaner(client, headers, "Sara", work_start="01:00:00", work_end="02:00:00")
    client.app.state.settings.gemini_api_key = "test-key"
    _mock_intent(monkeypatch, "assign_cleaner", "Sara")

    response = _chat(client, headers, task["id"], "Assign this to Sara.")

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "message"
    assert "outside working hours" in response.json()["reply"]
    unchanged = next(item for item in list_tasks(client, headers, task["property_id"]) if item["id"] == task["id"])
    assert unchanged["status"] == "at_risk"
    assert unchanged["assignment"] is None


def test_conflict_chat_explains_when_no_cleaner_is_available(client, monkeypatch):
    headers = login(client)
    _prop, task = _conflict(client, headers)
    client.app.state.settings.gemini_api_key = "test-key"
    _mock_intent(monkeypatch, "find_available")

    response = _chat(client, headers, task["id"], "Find another available cleaner.")

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "message"
    assert "No active cleaners" in response.json()["reply"]
    unchanged = next(item for item in list_tasks(client, headers, task["property_id"]) if item["id"] == task["id"])
    assert unchanged["assignment"] is None
