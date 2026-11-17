"""v12.2: Matrix como perfil padrão e personalidades distintas para Byte, Bit e Kronus."""
from byte_agent import agents, engine, stream


def teardown_function(_fn):
    engine.apply_profile("pessoal")


def test_default_profile_is_matrix():
    engine.apply_profile("matrix")
    assert engine.ACTIVE_PROFILE == "pessoal" and engine.AGENT_NAME == "Matrix" and engine.ACTIVE_AGENT is None
    assert engine.PROFILE_LABELS["pessoal"] == "Matrix (padrão)"
    assert engine.SYSTEM_PROMPT_V100.startswith("Você é Matrix") and "Byte (v10.1)" not in engine.SYSTEM_PROMPT_V100
    assert engine.AGENT_PERSONA == engine.MATRIX_PERSONA
    engine.apply_profile("vtuber")                                    # nome antigo continua aceito
    assert engine.ACTIVE_PROFILE == "pessoal"


def test_work_profile_keeps_matrix_personality():
    engine.apply_profile("trabalho")
    assert engine.AGENT_NAME == "Matrix" and engine.MATRIX_PERSONA in engine.SYSTEM_PROMPT_V100


def test_each_builtin_agent_has_its_own_personality():
    seen = {}
    for agent_id, gender, marks in [("byte", "feminino", ["energética", "amiga", "competitiva", "acolha com carinho", "justa e firme"]),
                                    ("bit", "masculino", ["mordomo", "estatística", "racional"]),
                                    ("kronus", "masculino", ["brother", "sarcasmo", "respeito"])]:
        m = agents.resolve_agent(agent_id)
        persona = agents.persona_of(m)
        low = persona.lower()
        assert f"fale sempre no {gender}" in low, agent_id
        for word in marks:  # noqa
            assert word in low, (agent_id, word)
        seen[agent_id] = persona
    assert len(set(seen.values())) == 3


def test_agent_introduction_reads_naturally():
    for agent_id in ("byte", "bit", "kronus"):
        m = agents.resolve_agent(agent_id)
        prompt = agents.build_agent_prompt(m)
        first = prompt.splitlines()[0]
        assert first == f"Você é {m['nome']}. {m['descricao'][0].upper() + m['descricao'][1:].rstrip('.')}."
        assert "Seu Nome" not in prompt and "de Seu" not in first
        agents.apply_identity(m)
        assert engine.AGENT_NAME == m["nome"]
        assert engine.AGENT_PERSONA.count(f"Você é {m['nome']}") == 1      # nome não repete na personalidade


def test_live_uses_the_byte_personality():
    assert stream.DEFAULT_PERSONA == agents.resolve_agent("byte")["persona"]
    env = agents.stream_env(agents.resolve_agent("byte"), lambda names: "")
    assert env.get("BYTE_STREAM_PERSONA", "").startswith("Você é Byte") and env["BYTE_STREAM_PERSONA"].count("Você é Byte") == 1


def test_panel_shows_the_agent_name():
    html = engine.build_v90_web_ui({"version": engine.BYTE_VERSION})
    assert "b90AgentLabel()" in html and "BYTE [" not in html and ">Matrix</button>" in html


def test_owner_placeholders_are_ignored_and_dados_perfil_fills_them():
    m = agents.resolve_agent("bit")
    assert "usuario" not in m["dono"] and "assinatura" not in m["dono"]
    again = agents.validate_manifest({**m, "dono": {**m["dono"], "usuario": "Ana Souza"}})
    prompt = agents.build_agent_prompt(again)
    assert "Você atende Ana Souza." in prompt.splitlines()[1]
