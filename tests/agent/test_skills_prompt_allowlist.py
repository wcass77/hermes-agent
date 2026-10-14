"""Explicit skill allowlists survive cold scans and disk-snapshot reuse."""

import pytest
import yaml

from agent import prompt_builder as pb


@pytest.fixture
def skill_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    roots = {"local": tmp_path / "skills", "external": tmp_path / "external"}
    for root in roots.values():
        for directory, name in (("folder-name", "declared-name"), ("other", "unapproved")):
            skill = root / directory / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                f"---\nname: {name}\ndescription: {name} instructions.\n---\nUse this skill.\n",
                encoding="utf-8",
            )
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)
    yield tmp_path, roots
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)


@pytest.mark.parametrize("source", ["local", "external"])
@pytest.mark.parametrize(
    "allowed, expected",
    [
        (["declared-name"], {"declared-name"}),
        (["folder-name"], {"declared-name"}),
        ([], set()),
        (None, {"declared-name", "unapproved"}),
    ],
)
def test_allowlist_matches_declared_or_directory_name(skill_catalog, source, allowed, expected):
    home, roots = skill_catalog
    # Keep the inactive root out of discovery so both source paths are independently exercised.
    active_root = roots[source]
    local = roots["local"] if source == "local" else home / "empty-skills"
    local.mkdir(exist_ok=True)
    config = {"skills": {"allowed": allowed}}
    if source == "external":
        config["skills"]["external_dirs"] = [str(active_root)]
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for reuse_snapshot in (False, True):
        if reuse_snapshot:
            assert pb._load_skills_snapshot(local) is not None
            pb.clear_skills_system_prompt_cache()
        prompt = pb.build_skills_system_prompt(skills_dir_override=local)
        for name in ("declared-name", "unapproved"):
            assert (f"{name}:" in prompt) == (name in expected)


@pytest.mark.parametrize("source", ["local", "external"])
def test_allowlisted_skill_still_obeys_disabled_policy(skill_catalog, source):
    home, roots = skill_catalog
    local = roots["local"] if source == "local" else home / "empty-skills"
    local.mkdir(exist_ok=True)
    config = {"skills": {"allowed": ["declared-name"], "disabled": ["declared-name"]}}
    if source == "external":
        config["skills"]["external_dirs"] = [str(roots[source])]
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    assert "declared-name:" not in pb.build_skills_system_prompt(skills_dir_override=local)
