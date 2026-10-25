"""Tests for OnlineContract with mocked HTTP."""

from unittest.mock import MagicMock, patch

from onlinecontract.client import OnlineContract


def _mock_response(status: int, json_data=None, json_raises=None):
    r = MagicMock()
    r.status_code = status
    if json_raises:
        r.json.side_effect = json_raises
    else:
        r.json.return_value = json_data if json_data is not None else {}
    return r


@patch("onlinecontract.client.api_get")
def test_get_procedures_non_200_returns_none(mock_get):
    mock_get.return_value = _mock_response(500)
    oc = OnlineContract()
    assert oc.get_procedures(["1811"]) is None


@patch("onlinecontract.client.api_get")
def test_get_procedures_empty_body_returns_none(mock_get):
    mock_get.return_value = _mock_response(200, json_data={})
    oc = OnlineContract()
    assert oc.get_procedures(["1811"]) is None


@patch("onlinecontract.client.api_get")
def test_get_procedures_missing_data_key_returns_none(mock_get):
    mock_get.return_value = _mock_response(200, json_data={"total": 0})
    oc = OnlineContract()
    assert oc.get_procedures(["1811"]) is None


@patch("onlinecontract.client.api_get")
def test_get_procedures_empty_list(mock_get):
    mock_get.return_value = _mock_response(200, json_data={"data": []})
    oc = OnlineContract()
    assert oc.get_procedures(["1811"]) == []


@patch("onlinecontract.client.api_get")
def test_get_procedures_success_normalizes_row(mock_get):
    mock_get.return_value = _mock_response(
        200,
        json_data={
            "data": [
                {
                    "id": 100,
                    "type": 1,
                    "name": "Proc",
                    "date": "d",
                    "status": 1,
                    "useNDS": False,
                    "nds": 0,
                    "reBiddingStart": "a",
                    "reBiddingEnd": "b",
                    "owner": {"name": "ACME"},
                }
            ]
        },
    )
    oc = OnlineContract()
    rows = oc.get_procedures(["1811"])
    assert len(rows) == 1
    r0 = rows[0]
    assert r0["owner"] == "ACME"
    assert r0["link"] == "https://onlinecontract.ru/tenders/100"
    assert r0["name"] == "Proc"


@patch("onlinecontract.client.api_get")
def test_get_positions_non_200_returns_none(mock_get):
    mock_get.return_value = _mock_response(404)
    oc = OnlineContract()
    assert oc.get_positions("123") is None


@patch("onlinecontract.client.api_get")
def test_get_positions_empty_data_array(mock_get):
    mock_get.return_value = _mock_response(200, json_data={"data": []})
    oc = OnlineContract()
    out = oc.get_positions("555")
    assert out["positions"] == []
    assert out["common_message"]["Id"] == "555"


@patch("onlinecontract.client.api_get")
def test_get_positions_with_rows(mock_get):
    mock_get.return_value = _mock_response(
        200,
        json_data={
            "data": [
                {
                    "name": "N1",
                    "totalCount": 1,
                    "unit": "u",
                    "price": 5,
                    "ownerSrok": "pay",
                    "ownerSklad": "ship",
                    "deliveryPlace": "here",
                }
            ]
        },
    )
    oc = OnlineContract()
    out = oc.get_positions("1")
    assert len(out["positions"]) == 1
    assert out["positions"][0]["name"] == "N1"
    assert out["common_message"].get("Id") == "1"


@patch("onlinecontract.client.api_get")
def test_get_procedures_calls_shared_client(mock_get):
    mock_get.return_value = _mock_response(200, json_data={"data": []})
    assert OnlineContract().get_procedures(["1811"]) == []
    assert mock_get.call_args.args[0] == "https://api.onlc.ru/purchases/v1/public/procedures"
    assert mock_get.call_args.kwargs["timeout"] == 60
    assert mock_get.call_args.kwargs["follow_redirects"] is True
    assert mock_get.call_args.kwargs["params"]["total"] is True
    assert mock_get.call_args.kwargs["params"]["filters[categories]"] == ["1811"]


def test_api_get_encodes_bool_like_requests(monkeypatch):
    import httpx

    from onlinecontract.client import PROCEDURES_URL, api_get

    captured = {}

    def fake_get(url, *, params=None, timeout=None, follow_redirects=False, **kwargs):
        captured["url"] = url
        captured["params"] = params
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {}
        return response

    monkeypatch.setattr(httpx, "get", fake_get)
    api_get(
        PROCEDURES_URL,
        params={"total": True, "limit": 100, "filters[categories]": ["1811", "1812"]},
        timeout=60,
        follow_redirects=True,
    )
    assert captured["params"]["total"] == "True"
    request = httpx.Request("GET", captured["url"], params=captured["params"])
    assert str(request.url) == (
        "https://api.onlc.ru/purchases/v1/public/procedures?total=True&limit=100"
        "&filters%5Bcategories%5D=1811&filters%5Bcategories%5D=1812"
    )


def test_active_list_url_keeps_httpx_query_text():
    import httpx

    from onlinecontract.client import PROCEDURES_URL

    url = f"{PROCEDURES_URL}?filters[status]=1&limit=100&offset=0&include=owner&total=true"
    assert str(httpx.Request("GET", url).url) == url
