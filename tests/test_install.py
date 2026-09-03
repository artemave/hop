from pathlib import Path

import hop
from hop.commands.install import install_agent_skill

SKILL_SOURCE = Path(hop.__file__).parent / "agents" / "hop-run" / "SKILL.md"


def test_install_agent_skill_writes_both_agent_dirs(tmp_path: Path) -> None:
    written = install_agent_skill(home=tmp_path)

    claude = tmp_path / ".claude" / "skills" / "hop-run" / "SKILL.md"
    codex = tmp_path / ".agents" / "skills" / "hop-run" / "SKILL.md"

    assert written == [claude, codex]
    source_bytes = SKILL_SOURCE.read_bytes()
    assert claude.read_bytes() == source_bytes
    assert codex.read_bytes() == source_bytes


def test_install_agent_skill_is_idempotent(tmp_path: Path) -> None:
    first = install_agent_skill(home=tmp_path)
    second = install_agent_skill(home=tmp_path)

    assert first == second
    for path in second:
        assert path.read_bytes() == SKILL_SOURCE.read_bytes()


def test_bundled_skill_has_name_and_description_frontmatter() -> None:
    text = SKILL_SOURCE.read_text()
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---\n", 2)

    assert "name: hop-run" in frontmatter
    assert "description:" in frontmatter
    # The description carries real trigger text, not just the key.
    assert "one-shot" in frontmatter
    # The skill gates on HOP_SESSION, both in the trigger and the body.
    assert "HOP_SESSION" in frontmatter
    assert "HOP_SESSION" in body
    # The body tells the agent which role to use.
    assert "hop run --wait --role test" in body
