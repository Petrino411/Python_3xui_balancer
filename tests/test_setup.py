"""Первичная настройка: поиск исходящих, выбор, генерация конфига, команда `init`."""

from __future__ import annotations

import json
import sqlite3

import pytest

from mock_panel import TOKEN, make_client

from xray_client_balancer import setup
from xray_client_balancer.config import load_config
from xray_client_balancer.main import main


def outbound(tag: str, protocol: str = "vless", address: str = "", port: int | None = None):
    settings = {"vnext": [{"address": address, "port": port}]} if address else {}
    return {"tag": tag, "protocol": protocol, "settings": settings}


# ------------------------------------------------------------------ поиск исходящих


def test_discover_merges_template_and_running() -> None:
    template = {
        "outbounds": [
            outbound("a", address="a.example", port=443),
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ]
    }
    running = {
        "outbounds": [
            outbound("a", address="a.example", port=443),
            outbound("sub-x", address="x.example", port=8443),
        ]
    }

    items = setup.discover_outbounds(template, running)

    assert [i.tag for i in items] == ["a", "sub-x"]
    assert items[0].origin == "шаблон+ядро"
    assert items[0].endpoint == "a.example:443"
    # тег, которого нет в шаблоне (подписка панели), тоже попадает в список
    assert items[1].origin == "ядро"
    assert items[1].endpoint == "x.example:8443"


def test_internal_outbounds_hidden_by_default() -> None:
    template = {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "dns-out", "protocol": "dns"},
            {"tag": "sub1", "protocol": "vless"},
        ]
    }

    assert [i.tag for i in setup.discover_outbounds(template)] == ["sub1"]
    assert len(setup.discover_outbounds(template, include_internal=True)) == 3


def test_render_outbounds_is_numbered() -> None:
    items = [setup.OutboundInfo("sub1", "vless", "a.example", "443", "шаблон+ядро")]
    text = setup.render_outbounds(items)
    assert "1)" in text and "sub1" in text and "a.example:443" in text


# ------------------------------------------------------------------ разбор выбора


def test_parse_selection_variants() -> None:
    tags = ["a", "b", "c"]
    assert setup.parse_selection("1,3", tags) == ["a", "c"]
    assert setup.parse_selection("1-2", tags) == ["a", "b"]
    assert setup.parse_selection("3-1", tags) == ["a", "b", "c"]  # диапазон всегда по возрастанию
    assert setup.parse_selection("all", tags) == tags
    assert setup.parse_selection("B", tags) == ["b"]
    assert setup.parse_selection("", tags, allow_empty=True) == []


def test_parse_selection_refuses_unknown() -> None:
    tags = ["a", "b"]
    for bad in ("9", "", "нет-такого"):
        with pytest.raises(setup.SetupError):
            setup.parse_selection(bad, tags)


def test_choose_primaries_repeats_on_bad_input() -> None:
    items = [setup.OutboundInfo("a"), setup.OutboundInfo("b"), setup.OutboundInfo("c")]
    answers = iter(["7", "1,2"])
    printed: list[str] = []

    chosen = setup.choose_primaries(items, ask_fn=lambda _p: next(answers), print_fn=printed.append)

    assert chosen == ["a", "b"]
    assert any("Не понял" in line for line in printed)


def test_choose_fallback_excludes_primaries() -> None:
    items = [setup.OutboundInfo(t) for t in ("a", "b", "c", "d")]
    # в списке для выбора остаются только те, кто не занят балансировщиками
    chosen = setup.choose_fallback(
        items, ["a", "b"], ask_fn=lambda _p: "2", print_fn=lambda _line: None
    )
    assert chosen == "d"

    # единственный свободный исходящий берётся без вопроса
    assert (
        setup.choose_fallback(
            items, ["a", "b", "c"], ask_fn=lambda _p: "1", print_fn=lambda _line: None
        )
        == "d"
    )


def test_choose_fallback_without_candidates_is_error() -> None:
    items = [setup.OutboundInfo("a")]
    with pytest.raises(setup.SetupError):
        setup.choose_fallback(items, ["a"], ask_fn=lambda _p: "1", print_fn=lambda _line: None)


# ------------------------------------------------------------------ конфиг


def test_build_config_document_is_valid_and_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XRAY_BALANCER_API_TOKEN", TOKEN)
    document = setup.build_config_document(
        "http://127.0.0.1:7119",
        ["sub1", "sub2"],
        "sub3",
        state_database=str(tmp_path / "state.db"),
        backups_directory=str(tmp_path / "backups"),
    )
    path = setup.write_config(tmp_path / "config.yaml", document)

    config = load_config(path)

    assert config.balancer_tags == ["client-balancer-1", "client-balancer-2"]
    assert [b.primary for b in config.balancers] == ["sub1", "sub2"]
    assert config.fallback.outbound == "sub3"
    assert config.panel.url == "http://127.0.0.1:7119"
    # в файле лежит подстановка, а не сам токен
    assert "${XRAY_BALANCER_API_TOKEN}" in path.read_text(encoding="utf-8")
    assert config.panel.api_token == TOKEN  # подставился из окружения при чтении
    assert config.panel.resolve_token() == TOKEN


def test_build_config_rejects_fallback_used_as_primary() -> None:
    with pytest.raises(setup.SetupError):
        setup.build_config_document("http://127.0.0.1:1", ["a"], "a")
    with pytest.raises(setup.SetupError):
        setup.build_config_document("http://127.0.0.1:1", [], "a")


def test_detect_panel_url_from_panel_db(tmp_path) -> None:
    db = tmp_path / "x-ui.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    conn.executemany(
        "INSERT INTO settings VALUES (?, ?)",
        [
            ("webPort", "21868"),
            ("webBasePath", "/AbCdEf/"),
            ("webCertFile", "/root/cert/ip/fullchain.pem"),
        ],
    )
    conn.commit()
    conn.close()

    detected = setup.detect_panel_url(db)

    assert detected == ("https://127.0.0.1:21868/AbCdEf/", "/root/cert/ip/fullchain.pem")
    assert setup.detect_panel_url(tmp_path / "нет.db") is None


def test_detect_panel_url_without_cert_is_http(tmp_path) -> None:
    db = tmp_path / "x-ui.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('webPort', '7119')")
    conn.commit()
    conn.close()

    assert setup.detect_panel_url(db) == ("http://127.0.0.1:7119/", None)


def test_resolve_panel_config_requires_token(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(setup.ENV_TOKEN_NAME, raising=False)
    with pytest.raises(setup.SetupError) as exc:
        setup.resolve_panel_config(url="http://127.0.0.1:7119")
    assert setup.ENV_TOKEN_NAME in str(exc.value)


# ------------------------------------------------------------------ команда init


def test_init_end_to_end_creates_balancers_and_routes(panel, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)
    panel.add_clients(*[make_client(i, f"user{i}@example") for i in range(1, 10)])
    path = tmp_path / "config.yaml"

    code = main(
        [
            "init",
            "--config",
            str(path),
            "--panel-url",
            panel.base_url,
            "--insecure",
            "--primary",
            "server-1,server-2",
            "--fallback",
            "server-3",
            "--state-db",
            str(tmp_path / "state.db"),
            "--backups-dir",
            str(tmp_path / "backups"),
            "--yes",
        ]
    )

    assert code == 0
    config = load_config(path)
    assert [b.primary for b in config.balancers] == ["server-1", "server-2"]
    assert panel.write_count == 1
    # балансировщики, правила и общий резерв создал сам сервис
    assert panel.distribution() == {"client-balancer-1": 5, "client-balancer-2": 4}
    assert {b["tag"]: b["fallbackTag"] for b in panel.template["routing"]["balancers"]} == {
        "client-balancer-1": "server-3",
        "client-balancer-2": "server-3",
    }
    assert panel.assignment_of("user1@example") in {"client-balancer-1", "client-balancer-2"}


def test_init_refuses_to_overwrite_without_force(panel, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)
    path = tmp_path / "config.yaml"
    path.write_text("panel: {url: 'http://127.0.0.1:1'}\n", encoding="utf-8")

    code = main(
        ["init", "--config", str(path), "--panel-url", panel.base_url, "--insecure",
         "--primary", "server-1", "--fallback", "server-2", "--yes"]
    )

    assert code == 1
    assert "panel: {url" in path.read_text(encoding="utf-8")  # файл не тронут
    assert panel.write_count == 0


def test_init_list_only_does_not_write_config(panel, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)
    path = tmp_path / "config.yaml"

    code = main(
        ["init", "--config", str(path), "--panel-url", panel.base_url, "--insecure", "--list-only"]
    )

    assert code == 0
    assert not path.exists()


def test_init_with_yes_requires_explicit_choice(panel, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)
    path = tmp_path / "config.yaml"

    code = main(
        ["init", "--config", str(path), "--panel-url", panel.base_url, "--insecure", "--yes"]
    )

    assert code == 1
    assert not path.exists()


def test_init_rewrites_config_with_force(panel, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)
    path = tmp_path / "config.yaml"
    path.write_text("panel: {url: 'http://127.0.0.1:1'}\n", encoding="utf-8")
    panel.add_clients(make_client(1))

    code = main(
        [
            "init", "--config", str(path), "--panel-url", panel.base_url, "--insecure",
            "--primary", "server-1", "--fallback", "server-2",
            "--state-db", str(tmp_path / "state.db"),
            "--backups-dir", str(tmp_path / "backups"),
            "--force", "--yes",
        ]
    )

    assert code == 0
    assert path.with_suffix(".yaml.bak").exists()
    assert load_config(path).fallback.outbound == "server-2"


def test_outbounds_command_lists_panel_outbounds(panel, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(setup.ENV_TOKEN_NAME, TOKEN)

    code = main(["outbounds", "--panel-url", panel.base_url, "--insecure", "--json"])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("[") :])
    assert "server-1" in [item["tag"] for item in payload]
    assert "direct" not in [item["tag"] for item in payload]
