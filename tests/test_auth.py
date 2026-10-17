from tests.conftest import create_property, login


def test_google_login_and_me(client):
    headers = login(client, "ada")
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "ada@example.com"
    assert body["name"] == "ada"


def test_invalid_google_token(client):
    response = client.post("/auth/google", json={"id_token": "bad-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_missing_session_is_rejected(client):
    response = client.get("/properties")
    assert response.status_code == 401


def test_cross_host_records_are_hidden(client):
    owner = login(client, "owner")
    other = login(client, "other")
    prop = create_property(client, owner)
    response = client.get(f"/properties/{prop['id']}", headers=other)
    assert response.status_code == 404
    listed = client.get("/properties", headers=other)
    assert listed.json()["total"] == 0
