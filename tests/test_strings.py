"""Every translation key the code uses exists, and en.json mirrors strings.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

COMPONENT = Path(__file__).parent.parent / "custom_components" / "mentat"
STRINGS = json.loads((COMPONENT / "strings.json").read_text())


def _source() -> str:
    return "\n".join(path.read_text() for path in COMPONENT.glob("*.py"))


def test_english_translation_is_strings_json() -> None:
    assert json.loads((COMPONENT / "translations" / "en.json").read_text()) == STRINGS


def test_every_flow_error_has_text() -> None:
    source = (COMPONENT / "config_flow.py").read_text()
    keys = set(re.findall(r'(?:errors\[[^\]]+\]\s*=\s*|return\s+)"([a-z_]+)"', source))
    assert keys
    assert keys <= set(STRINGS["config"]["error"])


def test_every_probe_error_has_text() -> None:
    from custom_components.mentat.config_flow import _PROBE_ERRORS, _STATUS_ERRORS

    keys = {key for _, key in _PROBE_ERRORS} | set(_STATUS_ERRORS.values()) | {"server_error"}
    assert keys <= set(STRINGS["config"]["error"])


def test_every_abort_reason_has_text() -> None:
    source = (COMPONENT / "config_flow.py").read_text()
    reasons = set(re.findall(r'reason="([a-z_]+)"', source))
    reasons |= {"reauth_successful", "reconfigure_successful"}
    assert reasons <= set(STRINGS["config"]["abort"])


def test_every_exception_key_has_text() -> None:
    keys = set(re.findall(r'translation_key="([a-z_]+)"', _source()))
    assert keys
    assert keys <= set(STRINGS["exceptions"])


def test_no_urls_in_strings() -> None:
    assert "://" not in json.dumps(STRINGS)


def test_reconfigure_mirrors_user_step() -> None:
    steps = STRINGS["config"]["step"]
    assert steps["reconfigure"]["sections"] == steps["user"]["sections"]


def test_every_question_editor_error_has_text() -> None:
    source = (COMPONENT / "editor.py").read_text()
    # A rule names its field and then its error key; option_problem returns one.
    keys = set(re.findall(r'(?:(?:BASE|CONF_[A-Z_]+),\s*|return\s+)"([a-z_]+)"', source))
    assert {"nothing_to_read", "window_without_target"} <= keys
    assert keys <= set(STRINGS["config_subentries"]["question"]["error"])


def test_every_trial_sentence_has_text() -> None:
    source = (COMPONENT / "preview.py").read_text()
    keys = set(re.findall(r'say\(\s*"([a-z_]+)"', source))
    keys |= set(re.findall(r'"(trial_[a-z_]+)"', source))
    assert keys
    assert keys <= set(STRINGS["common"])


def test_every_entity_translation_key_has_an_icon() -> None:
    icons = json.loads((COMPONENT / "icons.json").read_text())
    for platform in ("sensor", "binary_sensor"):
        assert set(STRINGS["entity"][platform]) == set(icons["entity"][platform])
    assert set(icons["services"]) == set(STRINGS["services"])
    # The AI Task entity takes the device's name, so it has an icon and no string.
    source = (COMPONENT / "ai_task.py").read_text()
    (key,) = re.findall(r'_attr_translation_key = "([a-z_]+)"', source)
    assert key in icons["entity"]["ai_task"]


def test_the_question_editor_forms_match() -> None:
    steps = STRINGS["config_subentries"]["question"]["step"]
    for step in ("noul", "choice", "score"):
        assert steps[step]["sections"] == steps["reconfigure"]["sections"]
        assert set(steps[step]["data"]) <= set(steps["reconfigure"]["data"])


def test_every_action_field_has_text_and_every_text_a_field() -> None:
    actions = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert set(actions) == set(STRINGS["services"])
    for action, spec in actions.items():
        assert set(spec["fields"]) == set(STRINGS["services"][action]["fields"]), action
