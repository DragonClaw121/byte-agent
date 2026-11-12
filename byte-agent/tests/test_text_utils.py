from byte_agent import engine

R = engine.TrueLLMAutonomousRouter


def test_tag_is_never_aliased():
    for tag in ["nemotron-3.5-lightning", "deepseek-r1:14b", "qwen2.5-coder:14b", "llama3.3:70b"]:
        assert R.normalize_ollama_model_name(tag)[0] == tag


def test_tag_hygiene_and_validation():
    assert R.normalize_ollama_model_name("  ollama:qwen2.5-coder : 14b ")[0] == "qwen2.5-coder:14b"
    assert not R.is_valid_ollama_tag(R.normalize_ollama_model_name("modelo;rm -rf")[0])


def test_reasoning_is_stripped():
    assert engine.strip_reasoning_markup("<think>the user wants X</think>\nOlá!") == "Olá!"
    assert engine.strip_reasoning_markup("plano...</think>Resposta") == "Resposta"


def test_language_detector():
    assert engine.looks_english("Okay, the user is asking what you would do with this and that. Here is how you can have it.")
    assert not engine.looks_english("Claro! Você pode começar pela revisão do diagrama, que é a parte mais importante para o projeto.")


def test_vram_estimate():
    assert R.estimate_vram_gb_from_label("14b") == 9.1
    assert R.estimate_vram_gb_from_label("lightning") is None
