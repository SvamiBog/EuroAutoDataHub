"""Согласованность вопросов и дашбордов Metabase (без запуска Metabase)."""
import re

import pytest

from cards import CARDS, DASHBOARDS, VARIABLES
from provision import template_tags

VARIABLE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@pytest.mark.parametrize("key", sorted(CARDS))
def test_card_variables_are_described(key):
    sql = CARDS[key]["sql"]
    used = set(VARIABLE_RE.findall(sql))
    assert used <= set(VARIABLES), f"{key}: неизвестные переменные {used - set(VARIABLES)}"
    assert "{{{{" not in sql and "}}}}" not in sql, f"{key}: лишние фигурные скобки после f-строки"
    # необязательная переменная должна стоять внутри [[ ... ]]
    required = set(CARDS[key].get("required", []))
    outside = set(VARIABLE_RE.findall(re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S)))
    assert outside <= required, f"{key}: переменные вне [[ ]] должны быть в required: {outside - required}"


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=lambda d: d["name"])
def test_dashboard_filters_reach_cards(dashboard):
    for key, width, height in dashboard["cards"]:
        assert key in CARDS and 0 < width <= 24 and height > 0
    for parameter in dashboard["parameters"]:
        assert parameter in VARIABLES
        assert any(parameter in VARIABLE_RE.findall(CARDS[key]["sql"]) for key, _, _ in dashboard["cards"]), \
            f"фильтр {parameter} дашборда «{dashboard['name']}» ни к чему не привязан"


def test_template_tags_have_stable_ids():
    first = template_tags("SELECT 1 WHERE [[x = {{make}}]]", [])
    second = template_tags("SELECT 2 WHERE [[y = {{make}}]]", [])
    assert first["make"]["id"] == second["make"]["id"]
    assert first["make"]["type"] == "text" and first["make"]["required"] is False
