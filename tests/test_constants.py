# -*- coding: utf-8 -*-
"""Unit tests for core/constants.py helper functions."""
from geomind_ai.core.constants import (
    MODELS,
    find_class_ids_by_keywords,
    get_model_key_by_mode,
)


def test_find_class_ids_by_keywords():
    assert find_class_ids_by_keywords(["耕", "农"]) == "1"
    assert find_class_ids_by_keywords(["建筑"]) == "5"


def test_find_class_ids_multiple_matches():
    ids = find_class_ids_by_keywords(["耕", "水"])
    assert set(ids.split(",")) == {"1", "10"}


def test_find_class_ids_fallback():
    assert find_class_ids_by_keywords(["不存在的词"], fallback_id="9") == "9"
    assert find_class_ids_by_keywords(["不存在的词"]) == ""


def test_get_model_key_by_mode():
    assert get_model_key_by_mode("sam3") == "SAM3_MODEL"
    assert get_model_key_by_mode("landuse") == "LANDUSE"
    assert get_model_key_by_mode("change_detection") == "CHANGE_DETECTION"


def test_get_model_key_unknown_mode_uses_first_model():
    assert get_model_key_by_mode("not_a_mode") == MODELS[0][1]
